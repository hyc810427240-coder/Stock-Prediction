from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import sys
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests

warnings.filterwarnings("ignore")


TARGET_NAME = "open_to_open_return"


@dataclass
class ModelResult:
    name: str
    win_rate: float
    avg_return: float
    prediction_win_probability: float
    prediction_return: float
    signal: int
    sample_size: int


def _import_optional(name: str) -> Any | None:
    try:
        return __import__(name)
    except Exception:
        return None


def normalize_symbol(raw: str) -> tuple[str, str]:
    text = raw.strip()
    known = {
        "城地香江": "603887",
        "城地香江股份": "603887",
        "603887": "603887",
    }
    code = known.get(text, text)
    m = re.search(r"(\d{6})", code)
    if not m:
        raise ValueError(f"Cannot parse stock code from: {raw}")
    six = m.group(1)
    market = "sh" if six.startswith(("5", "6", "9")) else "sz"
    return six, f"{market}.{six}"


def fetch_baostock_daily(bs_code: str, start: str, end: str) -> pd.DataFrame:
    import baostock as bs

    lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError(f"BaoStock login failed: {lg.error_msg}")
    try:
        fields = (
            "date,code,open,high,low,close,preclose,volume,amount,"
            "adjustflag,tradestatus,pctChg,peTTM,pbMRQ,psTTM,pcfNcfTTM,isST"
        )
        rs = bs.query_history_k_data_plus(
            bs_code,
            fields,
            start_date=start,
            end_date=end,
            frequency="d",
            adjustflag="2",
        )
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
        df = pd.DataFrame(rows, columns=rs.fields)
    finally:
        bs.logout()

    if df.empty:
        raise RuntimeError(f"No BaoStock daily data for {bs_code}")
    numeric = [c for c in df.columns if c not in {"date", "code"}]
    for c in numeric:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["date"] = pd.to_datetime(df["date"])
    df = df[df["tradestatus"] == 1].sort_values("date").reset_index(drop=True)
    # BaoStock's current daily endpoint does not expose turnover reliably.
    # Use a stable liquidity proxy so the model can still learn volume rotation.
    df["turnover"] = df["volume"] / df["volume"].rolling(60, min_periods=5).max() * 10
    return df


def fetch_eastmoney_daily(code: str, start: str, end: str) -> pd.DataFrame:
    market = "1" if code.startswith(("5", "6", "9")) else "0"
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {
        "secid": f"{market}.{code}",
        "klt": "101",
        "fqt": "1",
        "beg": start.replace("-", ""),
        "end": end.replace("-", ""),
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
    }
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    data = r.json().get("data") or {}
    klines = data.get("klines") or []
    if not klines:
        return pd.DataFrame()
    rows = []
    for line in klines:
        parts = line.split(",")
        if len(parts) < 11:
            continue
        rows.append(
            {
                "date": parts[0],
                "code": f"{'sh' if market == '1' else 'sz'}.{code}",
                "open": parts[1],
                "close": parts[2],
                "high": parts[3],
                "low": parts[4],
                "volume": parts[5],
                "amount": parts[6],
                "pctChg": parts[8],
                "turnover": parts[10],
                "preclose": np.nan,
                "adjustflag": 1,
                "tradestatus": 1,
                "peTTM": np.nan,
                "pbMRQ": np.nan,
                "psTTM": np.nan,
                "pcfNcfTTM": np.nan,
                "isST": 0,
            }
        )
    df = pd.DataFrame(rows)
    for c in [c for c in df.columns if c not in {"date", "code"}]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def merge_eastmoney_turnover(daily: pd.DataFrame, code: str, start: str, end: str) -> pd.DataFrame:
    try:
        em = fetch_eastmoney_daily(code, start, end)
    except Exception:
        return daily
    if em.empty or "turnover" not in em.columns:
        return daily
    out = daily.drop(columns=["turnover"], errors="ignore").merge(
        em[["date", "turnover"]], on="date", how="left"
    )
    if out["turnover"].notna().sum() < 20:
        out["turnover"] = daily.get("turnover")
    else:
        fallback = daily.get("turnover")
        if fallback is not None:
            out["turnover"] = out["turnover"].fillna(fallback)
    return out


def fetch_baostock_fundamentals(bs_code: str, years: int = 2) -> pd.DataFrame:
    import baostock as bs

    lg = bs.login()
    if lg.error_code != "0":
        return pd.DataFrame()

    frames = []
    try:
        today = dt.date.today()
        for y in range(today.year - years, today.year + 1):
            for q in range(1, 5):
                funcs = [
                    ("profit", bs.query_profit_data),
                    ("growth", bs.query_growth_data),
                    ("operation", bs.query_operation_data),
                    ("balance", bs.query_balance_data),
                ]
                merged: pd.DataFrame | None = None
                for prefix, fn in funcs:
                    try:
                        rs = fn(code=bs_code, year=y, quarter=q)
                        rows = []
                        while rs.next():
                            rows.append(rs.get_row_data())
                        tmp = pd.DataFrame(rows, columns=rs.fields)
                        if tmp.empty:
                            continue
                        rename = {
                            c: f"{prefix}_{c}"
                            for c in tmp.columns
                            if c not in {"code", "pubDate", "statDate"}
                        }
                        tmp = tmp.rename(columns=rename)
                        key_cols = [c for c in ["code", "pubDate", "statDate"] if c in tmp.columns]
                        tmp = tmp[key_cols + [c for c in tmp.columns if c not in key_cols]]
                        merged = tmp if merged is None else pd.merge(
                            merged, tmp, on=key_cols, how="outer"
                        )
                    except Exception:
                        continue
                if merged is not None and not merged.empty:
                    frames.append(merged)
    finally:
        bs.logout()
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    for c in df.columns:
        if c not in {"code", "pubDate", "statDate"}:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    if "pubDate" in df.columns:
        df["pubDate"] = pd.to_datetime(df["pubDate"], errors="coerce")
        df = df.sort_values("pubDate")
    return df


def fetch_eastmoney_news(code: str, name: str | None = None) -> list[dict[str, str]]:
    query = name or code
    url = "https://search-api-web.eastmoney.com/search/jsonp"
    params = {
        "cb": "jQuery",
        "param": json.dumps(
            {
                "uid": "",
                "keyword": query,
                "type": ["cmsArticleWebOld"],
                "client": "web",
                "clientType": "web",
                "clientVersion": "curr",
                "param": {"cmsArticleWebOld": {"searchScope": "default", "pageIndex": 1, "pageSize": 8}},
            },
            ensure_ascii=False,
        ),
    }
    try:
        r = requests.get(url, params=params, timeout=10)
        text = r.text
        m = re.search(r"jQuery\((.*)\)$", text)
        data = json.loads(m.group(1) if m else text)
        items = data.get("result", {}).get("cmsArticleWebOld", [])
        news = []
        for item in items[:8]:
            news.append(
                {
                    "title": str(item.get("title", "")).strip(),
                    "date": str(item.get("date", "") or item.get("showTime", "")).strip(),
                    "url": str(item.get("url", "")).strip(),
                }
            )
        return [n for n in news if n["title"]]
    except Exception:
        return []


def add_indicators(df: pd.DataFrame, fundamentals: pd.DataFrame | None = None) -> pd.DataFrame:
    out = df.copy()
    close = out["close"]
    high = out["high"]
    low = out["low"]
    volume = out["volume"].replace(0, np.nan)

    for n in [5, 10, 20, 60, 120]:
        out[f"ma{n}"] = close.rolling(n).mean()
        out[f"vol_ma{n}"] = volume.rolling(n).mean()

    out["ret1"] = close.pct_change()
    out["ret3"] = close.pct_change(3)
    out["ret5"] = close.pct_change(5)
    out["ret10"] = close.pct_change(10)
    out["ret20"] = close.pct_change(20)
    out["volatility20"] = out["ret1"].rolling(20).std()
    out["turnover_ma5"] = out["turnover"].rolling(5).mean()
    out["amount_ma5"] = out["amount"].rolling(5).mean()
    out["volume_ratio"] = volume / out["vol_ma20"]

    low_n = low.rolling(9).min()
    high_n = high.rolling(9).max()
    rsv = (close - low_n) / (high_n - low_n) * 100
    out["kdj_k"] = rsv.ewm(alpha=1 / 3, adjust=False).mean()
    out["kdj_d"] = out["kdj_k"].ewm(alpha=1 / 3, adjust=False).mean()
    out["kdj_j"] = 3 * out["kdj_k"] - 2 * out["kdj_d"]

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    out["macd_dif"] = ema12 - ema26
    out["macd_dea"] = out["macd_dif"].ewm(span=9, adjust=False).mean()
    out["macd_hist"] = 2 * (out["macd_dif"] - out["macd_dea"])

    out["boll_mid"] = out["ma20"]
    out["boll_std"] = close.rolling(20).std()
    out["boll_up"] = out["boll_mid"] + 2 * out["boll_std"]
    out["boll_low"] = out["boll_mid"] - 2 * out["boll_std"]
    out["boll_pct"] = (close - out["boll_low"]) / (out["boll_up"] - out["boll_low"])
    out["boll_width"] = (out["boll_up"] - out["boll_low"]) / out["boll_mid"]

    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    out["rsi14"] = 100 - 100 / (1 + rs)

    out["price_ma20_z"] = (close - out["ma20"]) / out["boll_std"]
    out["trend_strength"] = (out["ma5"] / out["ma20"] - 1) + (out["ma20"] / out["ma60"] - 1)
    out["volume_price_corr20"] = close.rolling(20).corr(volume)

    # Approximate chip/cost structure with a rolling volume-weighted cost distribution.
    out["cost_ma60"] = (close * volume).rolling(60).sum() / volume.rolling(60).sum()
    out["profit_ratio60"] = close / out["cost_ma60"] - 1
    out["chip_concentration60"] = close.rolling(60).std() / out["cost_ma60"]
    out["price_position60"] = (close - close.rolling(60).min()) / (
        close.rolling(60).max() - close.rolling(60).min()
    )

    if fundamentals is not None and not fundamentals.empty and "pubDate" in fundamentals.columns:
        keep = fundamentals.copy()
        key_cols = [
            c
            for c in keep.columns
            if c.startswith(("profit_", "growth_", "operation_", "balance_"))
        ]
        keep = keep[["pubDate"] + key_cols].dropna(subset=["pubDate"])
        if not keep.empty:
            out = pd.merge_asof(
                out.sort_values("date"),
                keep.sort_values("pubDate"),
                left_on="date",
                right_on="pubDate",
                direction="backward",
            )

    out[TARGET_NAME] = (out["open"].shift(-2) / out["open"].shift(-1) - 1) * 100
    out["target_win"] = (out[TARGET_NAME] > 0).astype(float)
    return out


def feature_columns(df: pd.DataFrame) -> list[str]:
    blocked = {
        "date",
        "code",
        "pubDate",
        TARGET_NAME,
        "target_win",
        "tradestatus",
        "adjustflag",
        "isST",
    }
    cols = []
    for c in df.columns:
        if c in blocked:
            continue
        if pd.api.types.is_numeric_dtype(df[c]):
            if df[c].notna().mean() >= 0.55 and pd.notna(df[c].iloc[-1]):
                cols.append(c)
    return cols


def train_test_split_time(df: pd.DataFrame, features: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    data = df[features + [TARGET_NAME, "target_win"]].replace([np.inf, -np.inf], np.nan)
    data = data.dropna(subset=[TARGET_NAME, "target_win"])
    usable = [c for c in features if data[c].notna().mean() >= 0.55]
    data = data[usable + [TARGET_NAME, "target_win"]].copy()
    data[usable] = data[usable].ffill().bfill()
    features[:] = usable
    if len(data) < 80:
        raise RuntimeError("Not enough clean samples after feature engineering")
    split = max(int(len(data) * 0.65), len(data) - 90)
    split = min(split, len(data) - 30)
    return data.iloc[:split].copy(), data.iloc[split:].copy()


def _standardize(train: pd.DataFrame, test: pd.DataFrame, features: list[str]) -> tuple[np.ndarray, np.ndarray]:
    mu = train[features].mean()
    sigma = train[features].std().replace(0, 1)
    train_x = train[features].fillna(mu)
    test_x = test[features].fillna(mu)
    return ((train_x - mu) / sigma).to_numpy(), ((test_x - mu) / sigma).to_numpy()


def ml_model_result(name: str, train: pd.DataFrame, test: pd.DataFrame, latest: pd.DataFrame, features: list[str], kind: str) -> ModelResult:
    sklearn = _import_optional("sklearn")
    X_train, X_test = _standardize(train, test, features)
    _, X_latest = _standardize(train, latest, features)
    y_cls = train["target_win"].astype(int).to_numpy()
    low_q, high_q = train[TARGET_NAME].quantile([0.01, 0.99])
    low_q = float(max(low_q, -10))
    high_q = float(min(high_q, 10))
    y_reg = train[TARGET_NAME].clip(low_q, high_q).to_numpy()

    if kind == "lightgbm" and _import_optional("lightgbm") is not None:
        import lightgbm as lgb

        cls = lgb.LGBMClassifier(
            n_estimators=80,
            learning_rate=0.04,
            max_depth=3,
            num_leaves=7,
            min_child_samples=12,
            random_state=42,
            verbose=-1,
        )
        reg = lgb.LGBMRegressor(
            n_estimators=80,
            learning_rate=0.04,
            max_depth=3,
            num_leaves=7,
            min_child_samples=12,
            random_state=42,
            verbose=-1,
        )
    elif sklearn is not None:
        from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
        from sklearn.linear_model import LogisticRegression, Ridge

        if kind == "multi_factor":
            cls = LogisticRegression(max_iter=1000, class_weight="balanced")
            reg = Ridge(alpha=3.0)
        else:
            cls = RandomForestClassifier(n_estimators=160, max_depth=4, min_samples_leaf=8, random_state=42)
            reg = RandomForestRegressor(n_estimators=160, max_depth=4, min_samples_leaf=8, random_state=42)
    else:
        return rule_result(name, test, latest, lambda x: np.sign(x["ret5"] + x["macd_hist"]))

    cls.fit(X_train, y_cls)
    reg.fit(X_train, y_reg)
    if hasattr(cls, "predict_proba"):
        proba = cls.predict_proba(X_test)[:, 1]
        latest_prob = float(cls.predict_proba(X_latest)[0, 1])
    else:
        pred_raw = cls.predict(X_test)
        proba = np.asarray(pred_raw, dtype=float)
        latest_prob = float(cls.predict(X_latest)[0])
    ret_pred = np.asarray(reg.predict(X_test), dtype=float)
    ret_pred = np.clip(ret_pred, low_q, high_q)
    latest_ret = float(np.clip(reg.predict(X_latest)[0], low_q, high_q))

    signal_mask = (proba >= 0.52) & (ret_pred > 0)
    if signal_mask.sum() == 0:
        signal_mask = proba >= np.nanmedian(proba)
    realized = test.loc[signal_mask, TARGET_NAME]
    win_rate = float((realized > 0).mean() * 100) if len(realized) else 0.0
    avg_return = float(realized.mean()) if len(realized) else 0.0
    latest_prob_pct = float(max(5, min(95, latest_prob * 100)))
    signal = 1 if latest_prob_pct >= 52 and latest_ret > 0 else -1 if latest_prob_pct <= 45 and latest_ret < 0 else 0
    return ModelResult(name, win_rate, avg_return, latest_prob_pct, latest_ret, signal, int(signal_mask.sum()))


def rule_result(name: str, test: pd.DataFrame, latest: pd.DataFrame, score_fn: Any) -> ModelResult:
    score = test.apply(score_fn, axis=1).astype(float)
    signal_mask = score > 0
    if signal_mask.sum() == 0:
        signal_mask = score >= score.median()
    realized = test.loc[signal_mask, TARGET_NAME]
    latest_score = float(score_fn(latest.iloc[0]))
    pred_prob = 50 + 18 * math.tanh(latest_score)
    pred_ret = float(realized.mean()) if len(realized) else 0.0
    signal = 1 if latest_score > 0.15 else -1 if latest_score < -0.15 else 0
    return ModelResult(
        name=name,
        win_rate=float((realized > 0).mean() * 100) if len(realized) else 0.0,
        avg_return=pred_ret,
        prediction_win_probability=float(max(5, min(95, pred_prob))),
        prediction_return=pred_ret,
        signal=signal,
        sample_size=int(signal_mask.sum()),
    )


def run_models(df: pd.DataFrame) -> tuple[list[ModelResult], pd.DataFrame]:
    features = feature_columns(df)
    latest_idx = df[features].replace([np.inf, -np.inf], np.nan).dropna(how="all").tail(1).index[0]
    latest = df.loc[[latest_idx], features + [TARGET_NAME, "target_win"]].copy()
    train, test = train_test_split_time(df.loc[: latest_idx - 2], features)
    latest[features] = latest[features].ffill().bfill()

    results = [
        ml_model_result("多因子模型(Logistic/Ridge)", train, test, latest, features, "multi_factor"),
        ml_model_result("LightGBM模型", train, test, latest, features, "lightgbm"),
        rule_result(
            "均值回归模型",
            test,
            latest,
            lambda r: (-r.get("price_ma20_z", 0) * 0.65)
            + ((35 - r.get("rsi14", 50)) / 35)
            + ((0.28 - r.get("boll_pct", 0.5)) * 1.2),
        ),
        rule_result(
            "趋势跟踪模型",
            test,
            latest,
            lambda r: (r.get("trend_strength", 0) * 8)
            + (r.get("macd_hist", 0) / max(abs(r.get("close", 1)), 1) * 100)
            + ((r.get("volume_ratio", 1) - 1) * 0.35),
        ),
        rule_result(
            "超跌反弹模型",
            test,
            latest,
            lambda r: ((-r.get("ret10", 0)) * 8)
            + ((30 - r.get("kdj_j", 50)) / 35)
            + ((r.get("turnover", 0) / max(r.get("turnover_ma5", 1), 1e-6) - 1) * 0.3)
            - max(r.get("profit_ratio60", 0), 0) * 2,
        ),
    ]
    return results, df.loc[[latest_idx]]


def operation_suggestion(results: list[ModelResult]) -> tuple[str, str]:
    best_win = max(results, key=lambda x: x.win_rate)
    best_return = max(results, key=lambda x: x.avg_return)
    avg_prob = np.average(
        [r.prediction_win_probability for r in results],
        weights=[max(r.sample_size, 1) for r in results],
    )
    avg_ret = np.average(
        [r.prediction_return for r in results],
        weights=[max(r.win_rate, 1) for r in results],
    )
    positive = sum(1 for r in results if r.signal > 0)
    negative = sum(1 for r in results if r.signal < 0)

    if avg_prob >= 58 and avg_ret > 0 and positive >= 2:
        action = "偏买入/持有"
    elif avg_prob <= 46 or (avg_ret < 0 and negative >= 2):
        action = "偏卖出/回避"
    else:
        action = "观望"
    reason = (
        f"胜率最高模型为{best_win.name}({best_win.win_rate:.2f}%), "
        f"收益率最高模型为{best_return.name}({best_return.avg_return:.2f}%). "
        f"模型加权预测胜率{avg_prob:.2f}%, 加权预测收益{avg_ret:.2f}%."
    )
    return action, reason


def simple_markdown_table(df: pd.DataFrame, max_cols: int = 8) -> str:
    if df.empty:
        return "No data."
    view = df.copy().tail(3)
    cols = list(view.columns[:max_cols])
    lines = [
        "| " + " | ".join(cols) + " |",
        "| " + " | ".join(["---"] * len(cols)) + " |",
    ]
    for _, row in view[cols].iterrows():
        vals = []
        for c in cols:
            v = row[c]
            if isinstance(v, float):
                vals.append(f"{v:.4g}")
            else:
                vals.append(str(v))
        lines.append("| " + " | ".join(vals) + " |")
    return "\n".join(lines)


def make_report(raw_input: str, out_dir: Path) -> Path:
    code, bs_code = normalize_symbol(raw_input)
    name = "城地香江" if code == "603887" else code
    today = dt.date.today()
    start = (today - dt.timedelta(days=560)).isoformat()
    end = today.isoformat()

    try:
        daily = fetch_baostock_daily(bs_code, start, end)
        daily = merge_eastmoney_turnover(daily, code, start, end)
    except Exception:
        daily = fetch_eastmoney_daily(code, start, end)
        if daily.empty:
            raise
    fundamentals = fetch_baostock_fundamentals(bs_code)
    news = fetch_eastmoney_news(code, name)
    df = add_indicators(daily, fundamentals)
    results, latest = run_models(df)
    action, reason = operation_suggestion(results)

    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = out_dir / f"{code}_quant_report_{stamp}.md"
    last = latest.iloc[0]
    model_rows = "\n".join(
        [
            f"| {r.name} | {r.win_rate:.2f}% | {r.avg_return:.2f}% | "
            f"{r.prediction_win_probability:.2f}% | {r.prediction_return:.2f}% | "
            f"{'买入/持有' if r.signal > 0 else '卖出/回避' if r.signal < 0 else '观望'} | {r.sample_size} |"
            for r in results
        ]
    )
    news_rows = "\n".join(
        [f"- {n['date']} [{n['title']}]({n['url']})" if n["url"] else f"- {n['date']} {n['title']}" for n in news]
    ) or "- 东方财富搜索接口本次未返回可解析资讯。"
    fund_tail = simple_markdown_table(fundamentals) if not fundamentals.empty else "无可解析财报数据。"

    content = f"""# {name}({code}) 单股次日量化建议

生成时间: {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

最近交易日: {last['date'].strftime('%Y-%m-%d')}
收盘价: {last['close']:.2f}
换手率: {last.get('turnover', np.nan):.2f}%
MACD柱: {last.get('macd_hist', np.nan):.4f}
KDJ-J: {last.get('kdj_j', np.nan):.2f}
BOLL位置: {last.get('boll_pct', np.nan):.2f}
近60日筹码/成本收益估计: {last.get('profit_ratio60', np.nan) * 100:.2f}%

## 模型结果

收益率定义: `(t+2开盘价 / t+1开盘价 - 1) * 100%`。

| 模型 | 历史信号胜率 | 历史信号平均收益率 | 预测次日胜率 | 预测次日收益率 | 信号 | 历史信号数 |
|---|---:|---:|---:|---:|---|---:|
{model_rows}

## 操作建议

结论: **{action}**

{reason}

该结果适合做模型研究和交易前筛选，不应直接作为实盘唯一依据。A股个股隔夜风险、公告停复牌、流动性、市场整体风险偏好会明显影响第二天开盘后的收益分布。

## 近一年重要讯息摘要

{news_rows}

## 最近财报数据

{fund_tail}
"""
    report_path.write_text(content, encoding="utf-8")

    csv_path = out_dir / f"{code}_features_{stamp}.csv"
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    return report_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Single-stock next-day quant model prototype.")
    parser.add_argument("stock", nargs="?", default="城地香江", help="Stock name or code, e.g. 城地香江 or 603887")
    parser.add_argument("--out", default=str(Path(__file__).resolve().parent), help="Output directory")
    args = parser.parse_args()
    try:
        report = make_report(args.stock, Path(args.out))
        print(report)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
