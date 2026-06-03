from __future__ import annotations

import datetime as dt
import html
import json
import re
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests
from flask import Flask, jsonify, render_template, request, send_from_directory

from stock_quant_app import (
    TARGET_NAME,
    add_indicators,
    fetch_baostock_daily,
    fetch_baostock_fundamentals,
    fetch_eastmoney_daily,
    fetch_eastmoney_news,
    merge_eastmoney_turnover,
    operation_suggestion,
    run_models,
)


BASE_DIR = Path(__file__).resolve().parent
app = Flask(__name__, template_folder=str(BASE_DIR / "templates"), static_folder=str(BASE_DIR / "static"))


def clean_number(value: Any, digits: int = 2) -> float | None:
    try:
        if value is None or not np.isfinite(float(value)):
            return None
        return round(float(value), digits)
    except Exception:
        return None


def signal_label(signal: int) -> str:
    if signal > 0:
        return "买入/持有"
    if signal < 0:
        return "卖出/回避"
    return "观望"


def resolve_stock(raw_input: str) -> tuple[str, str, str]:
    text = raw_input.strip()
    m = re.search(r"(\d{6})", text)
    if m:
        code = m.group(1)
        name = lookup_stock_name(code) or ("城地香江" if code == "603887" else code)
    else:
        found = search_stock_by_name(text)
        if not found:
            raise ValueError(f"未能识别股票名称或代码：{raw_input}")
        code, name = found
    market = "sh" if code.startswith(("5", "6", "9")) else "sz"
    return code, f"{market}.{code}", name


def search_stock_by_name(name: str) -> tuple[str, str] | None:
    url = "https://searchapi.eastmoney.com/api/suggest/get"
    try:
        r = requests.get(url, params={"input": name, "type": "14"}, timeout=(3, 5), headers={"User-Agent": "Mozilla/5.0"})
        text = r.content.decode("utf-8", errors="ignore")
        data = json.loads(text)
        rows = data.get("QuotationCodeTable", {}).get("Data", [])
    except Exception:
        rows = []
    for row in rows:
        code = str(row.get("Code") or row.get("UnifiedCode") or "")
        stock_name = str(row.get("Name") or "").strip()
        if re.fullmatch(r"\d{6}", code) and row.get("Classify") == "AStock":
            return code, stock_name or name
    return None


def lookup_stock_name(code: str) -> str | None:
    found = search_stock_by_name(code)
    return found[1] if found else None


def _clean_title(text: str) -> str:
    text = html.unescape(re.sub(r"<.*?>", "", text))
    return re.sub(r"\s+", " ", text).strip()


def _extract_news_from_html(source: str, url: str, text: str, limit: int = 6) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for m in re.finditer(r"<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", text, re.I | re.S):
        href, title = m.groups()
        title = _clean_title(title)
        if not (8 <= len(title) <= 90):
            continue
        if any(skip in title.lower() for skip in ["登录", "注册", "首页", "行情", "更多", "广告", "历史资金", "立即查看"]):
            continue
        if re.search(r"[ÃÂ�]{1,}", title):
            continue
        if "http" not in href:
            href = requests.compat.urljoin(url, href)
        key = title[:36]
        if key in seen:
            continue
        seen.add(key)
        items.append({"source": source, "title": title, "date": "", "url": href})
        if len(items) >= limit:
            break
    return items


def fetch_more_news(code: str, name: str) -> list[dict[str, str]]:
    headers = {"User-Agent": "Mozilla/5.0"}
    sources = [
        ("腾讯财经", f"https://gu.qq.com/sh{code}" if code.startswith(("5", "6", "9")) else f"https://gu.qq.com/sz{code}"),
        ("同花顺", f"https://stockpage.10jqka.com.cn/{code}/news/"),
        ("新浪财经", f"https://finance.sina.com.cn/realstock/company/{'sh' if code.startswith(('5','6','9')) else 'sz'}{code}/nc.shtml"),
    ]
    merged: list[dict[str, str]] = []
    for source, url in sources:
        try:
            r = requests.get(url, headers=headers, timeout=(3, 4))
            if r.status_code != 200:
                continue
            r.encoding = r.apparent_encoding or r.encoding
            merged.extend(_extract_news_from_html(source, url, r.text))
        except Exception:
            continue
    if not merged:
        return []
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for item in merged:
        key = item["title"][:36]
        if key in seen:
            continue
        title = item["title"]
        if title.replace("(", "").replace(")", "").replace("SH", "").replace("sh", "") in {name, code, f"{name}{code}"}:
            continue
        important = any(k in title for k in [name, code, "公告", "业绩", "利润", "披露", "减持", "增持", "中标", "合同", "风险", "问询", "处罚", "涨停", "跌停"])
        if not important:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= 8:
            break
    return out


def fetch_eastmoney_news_short(code: str, name: str) -> list[dict[str, str]]:
    try:
        news = fetch_eastmoney_news(code, name)
    except Exception:
        return []
    for item in news:
        item.setdefault("source", "东方财富")
    return news


def fetch_sse_announcements(code: str) -> list[dict[str, str]]:
    if not code.startswith(("5", "6", "9")):
        return []
    today = dt.date.today()
    url = "https://query.sse.com.cn/security/stock/queryCompanyBulletin.do"
    params = {
        "jsonCallBack": "jsonpCallback",
        "isPagination": "true",
        "productId": code,
        "securityType": "0101,120100,020100,020200,120200",
        "reportType": "ALL",
        "beginDate": (today - dt.timedelta(days=365)).isoformat(),
        "endDate": today.isoformat(),
        "pageHelp.pageSize": "8",
        "pageHelp.pageNo": "1",
        "pageHelp.beginPage": "1",
        "pageHelp.cacheSize": "1",
        "pageHelp.endPage": "1",
    }
    try:
        r = requests.get(
            url,
            params=params,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.sse.com.cn/"},
            timeout=(3, 5),
        )
        r.encoding = "utf-8"
        m = re.search(r"jsonpCallback\((.*)\)$", r.text)
        payload = json.loads(m.group(1) if m else r.text)
        rows = payload.get("pageHelp", {}).get("data", [])
    except Exception:
        return []
    out = []
    for row in rows[:8]:
        title = str(row.get("TITLE") or "").strip()
        link = str(row.get("URL") or "").strip()
        if not title:
            continue
        out.append(
            {
                "source": "上交所公告",
                "title": title,
                "date": str(row.get("SSEDATE") or row.get("ADDDATE") or ""),
                "url": requests.compat.urljoin("https://www.sse.com.cn", link),
            }
        )
    return out


def build_trade_plan(action: str, results: list[Any], features, last) -> dict[str, Any]:
    hist = features.tail(45)
    close = float(last["close"])
    lower_candidates = [
        float(v)
        for v in [
            hist["low"].tail(20).min(),
            last.get("boll_low"),
            last.get("ma5"),
            last.get("ma10"),
            last.get("ma20"),
        ]
        if pd_notna(v) and float(v) < close
    ]
    upper_candidates = [
        float(v)
        for v in [
            hist["high"].tail(20).max(),
            last.get("boll_mid"),
            last.get("boll_up"),
            last.get("ma5"),
            last.get("ma10"),
            last.get("ma20"),
        ]
        if pd_notna(v) and float(v) > close
    ]
    support = max(lower_candidates) if lower_candidates else float(hist["low"].tail(20).min())
    resistance = min(upper_candidates) if upper_candidates else float(hist["high"].tail(20).max())
    stop_loss = support * 0.985

    avg_prob = np.average([r.prediction_win_probability for r in results], weights=[max(r.sample_size, 1) for r in results])
    avg_ret = np.average([r.prediction_return for r in results], weights=[max(r.win_rate, 1) for r in results])
    score = 3 + (avg_prob - 50) / 18 + avg_ret / 3
    if "卖出" in action:
        score -= 1.2
    elif "买入" in action:
        score += 0.5
    stars = int(max(1, min(5, round(score))))

    if "卖出" in action:
        advice = (
            f"明日以防守为主，不追高。盘中若反抽至{resistance:.2f}附近但量能不足，优先减仓或回避；"
            f"若跌破{support:.2f}且不能快速收回，短线风险加大，止损参考{stop_loss:.2f}。"
        )
    elif "买入" in action:
        advice = (
            f"明日可轻仓试探，优先等回踩{support:.2f}附近企稳后介入；"
            f"若放量突破{resistance:.2f}，可提高仓位，跌破{stop_loss:.2f}应及时止损。"
        )
    else:
        advice = (
            f"明日先观察价格在{support:.2f}-{resistance:.2f}区间的方向选择；"
            f"放量突破压力再考虑跟随，跌破支撑则回避，止损参考{stop_loss:.2f}。"
        )

    return {
        "stars": stars,
        "support": clean_number(support),
        "resistance": clean_number(resistance),
        "stopLoss": clean_number(stop_loss),
        "weightedWin": clean_number(avg_prob),
        "weightedReturn": clean_number(avg_ret),
        "advice": advice,
    }


def pd_notna(value: Any) -> bool:
    try:
        return value is not None and np.isfinite(float(value))
    except Exception:
        return False


def period_label(value: Any) -> str:
    text = str(value)[:10]
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    if not m:
        return text
    year, month, day = m.groups()
    if month == "12" and day == "31":
        return f"{year}年报"
    if month == "03":
        return f"{year}一季报"
    if month == "06":
        return f"{year}半年报"
    if month == "09":
        return f"{year}三季报"
    return text


def compact_money(value: Any) -> str:
    try:
        num = float(value)
    except Exception:
        return "--"
    sign = "-" if num < 0 else ""
    num = abs(num)
    if num >= 100000000:
        return f"{sign}{num / 100000000:.2f}亿"
    if num >= 10000:
        return f"{sign}{num / 10000:.0f}万"
    return f"{sign}{num:.0f}"


def compact_percent(value: Any) -> str:
    try:
        return f"{float(value):.2f}%"
    except Exception:
        return "--"


def build_financial_metric(fundamentals, key: str, label: str, unit: str, is_ratio: bool = False) -> dict[str, Any]:
    if fundamentals.empty or "statDate" not in fundamentals.columns or key not in fundamentals.columns:
        return {"key": key, "label": label, "unit": unit, "labels": [], "values": [], "valueText": [], "yoy": [], "rows": []}
    df = fundamentals[["statDate", key]].copy()
    df["statDate"] = df["statDate"].astype(str)
    df[key] = df[key].astype(float)
    df = df.dropna().drop_duplicates("statDate").sort_values("statDate").tail(6)
    labels = [period_label(x) for x in df["statDate"]]
    values = [float(x) * 100 if is_ratio else float(x) for x in df[key]]
    yoy: list[float | None] = []
    for idx, value in enumerate(values):
        if idx == 0 or values[idx - 1] == 0:
            yoy.append(None)
        else:
            yoy.append(round((value - values[idx - 1]) / abs(values[idx - 1]) * 100, 2))
    value_text = [compact_percent(x) if is_ratio else compact_money(x) for x in values]
    rows = [
        {"period": labels[i], "value": value_text[i], "yoy": None if yoy[i] is None else yoy[i]}
        for i in range(len(labels) - 1, -1, -1)
    ]
    return {
        "key": key,
        "label": label,
        "unit": unit,
        "labels": labels,
        "values": [clean_number(x, 2) for x in values],
        "valueText": value_text,
        "yoy": yoy,
        "rows": rows,
    }


def build_financial_chart(fundamentals) -> dict[str, Any]:
    metric_defs = [
        ("profit_netProfit", "归母净利润", "元", False),
        ("profit_MBRevenue", "营业收入", "元", False),
        ("growth_YOYPNI", "扣非净利润估算", "%", True),
        ("balance_liabilityToAsset", "资产负债率", "%", True),
        ("profit_gpMargin", "销售毛利率", "%", True),
    ]
    metrics = [build_financial_metric(fundamentals, *item) for item in metric_defs]
    metrics = [m for m in metrics if m["labels"]]
    active = metrics[0]["key"] if metrics else ""
    return {"active": active, "metrics": metrics}


def fetch_quote_metrics(code: str, latest, fundamentals) -> list[dict[str, str]]:
    close = float(latest.get("close", 0) or 0)
    total_share = None
    float_share = None
    if not fundamentals.empty:
        tail = fundamentals.tail(1).iloc[0]
        total_share = tail.get("profit_totalShare")
        float_share = tail.get("profit_liqaShare")
    total_market_cap = close * float(total_share) if pd_notna(total_share) else None
    pe_dynamic = estimate_dynamic_pe(total_market_cap, fundamentals)
    pe_static = estimate_static_pe(total_market_cap, fundamentals)
    metrics = [
        {"label": "市盈TTM", "value": fmt_metric(latest.get("peTTM"))},
        {"label": "PB", "value": fmt_metric(latest.get("pbMRQ"))},
        {"label": "市盈(动)", "value": fmt_metric(pe_dynamic)},
        {"label": "市盈(静)", "value": fmt_metric(pe_static)},
        {"label": "总市值", "value": compact_money(total_market_cap) if total_market_cap is not None else "--"},
        {"label": "流通市值", "value": compact_money(close * float(float_share)) if pd_notna(float_share) else "--"},
    ]
    market = "1" if code.startswith(("5", "6", "9")) else "0"
    try:
        r = requests.get(
            "https://push2.eastmoney.com/api/qt/stock/get",
            params={"secid": f"{market}.{code}", "fields": "f116,f117,f162,f163,f164,f167"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=(2, 4),
        )
        data = r.json().get("data") or {}
        em = {
            "市盈(动)": scaled_number(data.get("f162"), 2),
            "市盈(静)": scaled_number(data.get("f163"), 2),
            "市盈TTM": scaled_number(data.get("f164"), 2),
            "PB": scaled_number(data.get("f167"), 2),
            "总市值": compact_money(float(data["f116"])) if pd_notna(data.get("f116")) else "--",
            "流通市值": compact_money(float(data["f117"])) if pd_notna(data.get("f117")) else "--",
        }
        for item in metrics:
            if em.get(item["label"]) not in {None, "--"}:
                item["value"] = em[item["label"]]
    except Exception:
        pass
    return metrics


def estimate_static_pe(market_cap: float | None, fundamentals) -> float | None:
    if market_cap is None or fundamentals.empty or "profit_netProfit" not in fundamentals.columns:
        return None
    annual = fundamentals[fundamentals["statDate"].astype(str).str.endswith("-12-31")]
    if annual.empty:
        return None
    profit = annual.tail(1).iloc[0].get("profit_netProfit")
    if not pd_notna(profit) or float(profit) == 0:
        return None
    return market_cap / float(profit)


def estimate_dynamic_pe(market_cap: float | None, fundamentals) -> float | None:
    if market_cap is None or fundamentals.empty or "profit_netProfit" not in fundamentals.columns:
        return None
    row = fundamentals.dropna(subset=["profit_netProfit"]).tail(1)
    if row.empty:
        return None
    item = row.iloc[0]
    stat = str(item.get("statDate"))
    profit = float(item.get("profit_netProfit"))
    if profit == 0:
        return None
    factor = 1.0
    if stat.endswith("-03-31"):
        factor = 4.0
    elif stat.endswith("-06-30"):
        factor = 2.0
    elif stat.endswith("-09-30"):
        factor = 4.0 / 3.0
    return market_cap / (profit * factor)


def scaled_number(value: Any, digits: int = 2) -> str:
    if not pd_notna(value):
        return "--"
    num = float(value)
    if abs(num) > 1000:
        num = num / 100
    return f"{num:.{digits}f}"


def fmt_metric(value: Any) -> str:
    if not pd_notna(value):
        return "--"
    return f"{float(value):.2f}"


def analyze_stock(raw_input: str) -> dict[str, Any]:
    code, bs_code, name = resolve_stock(raw_input)
    today = dt.date.today()
    start = (today - dt.timedelta(days=560)).isoformat()
    end = today.isoformat()

    used_cached_features = False
    try:
        daily = fetch_baostock_daily(bs_code, start, end)
        daily = merge_eastmoney_turnover(daily, code, start, end)
        data_source = "BaoStock日线 + 东方财富换手率"
    except Exception:
        try:
            daily = fetch_eastmoney_daily(code, start, end)
            if daily.empty:
                raise RuntimeError("东方财富日线为空")
            data_source = "东方财富日线"
        except Exception:
            cached = load_cached_features(code)
            if cached is None:
                raise
            features = cached
            fundamentals = fetch_baostock_fundamentals(bs_code)
            news = fetch_eastmoney_news_short(code, name)
            if not news:
                news = fetch_sse_announcements(code) + fetch_more_news(code, name)
            used_cached_features = True
            data_source = "本地缓存特征数据"

    if not used_cached_features:
        fundamentals = fetch_baostock_fundamentals(bs_code)
        news = fetch_eastmoney_news_short(code, name)
        if not news:
            news = fetch_sse_announcements(code) + fetch_more_news(code, name)
        features = add_indicators(daily, fundamentals)
    results, latest = run_models(features)
    action, reason = operation_suggestion(results)

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_name = f"{code}_web_features_{stamp}.csv"
    features.to_csv(BASE_DIR / csv_name, index=False, encoding="utf-8-sig")

    last = latest.iloc[0]
    chart_df = features.tail(90).copy()
    chart = {
        "dates": [d.strftime("%Y-%m-%d") for d in chart_df["date"]],
        "close": [clean_number(x) for x in chart_df["close"]],
        "ma20": [clean_number(x) for x in chart_df["ma20"]],
        "bollUp": [clean_number(x) for x in chart_df["boll_up"]],
        "bollLow": [clean_number(x) for x in chart_df["boll_low"]],
    }
    model_rows = [
        {
            "name": r.name,
            "winRate": clean_number(r.win_rate),
            "avgReturn": clean_number(r.avg_return),
            "predWin": clean_number(r.prediction_win_probability),
            "predReturn": clean_number(r.prediction_return),
            "signal": signal_label(r.signal),
            "sampleSize": r.sample_size,
        }
        for r in results
    ]
    financial_chart = build_financial_chart(fundamentals)
    trade_plan = build_trade_plan(action, results, features, last)
    quote_metrics = fetch_quote_metrics(code, last, fundamentals)

    return {
        "code": code,
        "name": name,
        "dataSource": data_source,
        "generatedAt": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "latestDate": last["date"].strftime("%Y-%m-%d"),
        "action": action,
        "reason": reason,
        "tradePlan": trade_plan,
        "weighted": {
            "close": clean_number(last["close"]),
            "turnover": clean_number(last.get("turnover")),
            "macdHist": clean_number(last.get("macd_hist"), 4),
            "kdjJ": clean_number(last.get("kdj_j")),
            "bollPct": clean_number(last.get("boll_pct")),
            "chipProfit": clean_number(last.get("profit_ratio60", 0) * 100),
            "volumeRatio": clean_number(last.get("volume_ratio")),
            "targetDefinition": f"({TARGET_NAME}) = (t+2开盘价 / t+1开盘价 - 1) * 100%",
        },
        "quoteMetrics": quote_metrics,
        "models": model_rows,
        "news": news,
        "fundamentals": financial_chart,
        "chart": chart,
        "featureCsv": csv_name,
    }


def load_cached_features(code: str):
    files = sorted(BASE_DIR.glob(f"{code}*_features_*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
    files += sorted(BASE_DIR.glob(f"{code}_web_features_*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return None
    df = pd.read_csv(files[0])
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
    return df


@app.get("/")
def index() -> str:
    return render_template("index.html")


@app.post("/api/analyze")
def api_analyze():
    payload = request.get_json(silent=True) or {}
    stock = str(payload.get("stock") or "城地香江").strip()
    try:
        return jsonify({"ok": True, "data": analyze_stock(stock)})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/<path:filename>")
def output_file(filename: str):
    if filename.endswith(".csv"):
        return send_from_directory(BASE_DIR, filename, as_attachment=True)
    return ("Not found", 404)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
