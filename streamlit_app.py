from __future__ import annotations

import sys
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

APP_DIR = Path(__file__).resolve().parent / "outputs"
sys.path.insert(0, str(APP_DIR))

from web_app import analyze_stock  # noqa: E402


st.set_page_config(page_title="A股单股次日量化建议", layout="wide")


@st.cache_data(ttl=300, show_spinner=False)
def cached_analyze(stock: str):
    return analyze_stock(stock)


def pct(value):
    if value is None:
        return "--"
    return f"{value}%"


def metric_table(items):
    cols = st.columns(2)
    for idx, item in enumerate(items):
        with cols[idx % 2]:
            st.metric(item["label"], item["value"])


def draw_price_chart(chart):
    rows = []
    for i, date in enumerate(chart.get("dates", [])):
        for key, name in [("close", "收盘"), ("ma20", "MA20"), ("bollUp", "BOLL上轨"), ("bollLow", "BOLL下轨")]:
            value = chart.get(key, [None] * len(chart.get("dates", [])))[i]
            if value is not None:
                rows.append({"日期": date, "价格": value, "指标": name})
    df = pd.DataFrame(rows)
    if df.empty:
        st.info("暂无价格图表数据")
        return
    line = (
        alt.Chart(df)
        .mark_line()
        .encode(
            x=alt.X("日期:N", axis=alt.Axis(labelAngle=0, labelOverlap=True)),
            y=alt.Y("价格:Q", scale=alt.Scale(zero=False)),
            color="指标:N",
        )
        .properties(height=320)
    )
    st.altair_chart(line, use_container_width=True)


def draw_financial_metric(metric):
    labels = metric.get("labels", [])
    values = metric.get("values", [])
    yoy = metric.get("yoy", [])
    value_text = metric.get("valueText", [])
    if not labels:
        st.info("暂无该指标财报数据")
        return
    df = pd.DataFrame(
        {
            "报告期": labels,
            "数值": values,
            "同比": yoy,
            "显示值": value_text,
        }
    )
    bars = (
        alt.Chart(df)
        .mark_bar(color="#3578f6")
        .encode(
            x=alt.X("报告期:N", axis=alt.Axis(labelAngle=0)),
            y=alt.Y("数值:Q", title=metric.get("label", "财务指标")),
            tooltip=["报告期", "显示值", "同比"],
        )
    )
    line = (
        alt.Chart(df.dropna(subset=["同比"]))
        .mark_line(point=True, color="#e2a22a")
        .encode(
            x="报告期:N",
            y=alt.Y("同比:Q", title="同比(%)"),
            tooltip=["报告期", "同比"],
        )
    )
    st.altair_chart(alt.layer(bars, line).resolve_scale(y="independent").properties(height=330), use_container_width=True)
    st.dataframe(pd.DataFrame(metric.get("rows", [])).rename(columns={"period": "报告期", "value": "数值", "yoy": "同比"}), use_container_width=True, hide_index=True)


st.title("A股单股次日量化建议")
stock = st.text_input("股票名称或代码", value="城地香江", placeholder="例如：城地香江 / 603887 / 平安银行 / 000001")

if st.button("运行模型", type="primary") or "last_result" not in st.session_state:
    with st.spinner("正在获取行情、财报和资讯，并训练模型..."):
        st.session_state.last_result = cached_analyze(stock.strip() or "城地香江")

data = st.session_state.last_result

st.caption(f"{data['name']}({data['code']}) · 最近交易日 {data['latestDate']} · 数据源：{data['dataSource']}")

top_left, top_right = st.columns([1.2, 1])
with top_left:
    stars = int(data["tradePlan"].get("stars") or 1)
    st.header(f"{data['action']}  {'★' * stars}{'☆' * (5 - stars)}")
    st.error(
        "第二天操作建议\n\n"
        f"{data['tradePlan']['advice']}\n\n"
        f"支撑位 {data['tradePlan']['support']} · 压力位 {data['tradePlan']['resistance']} · 止损参考 {data['tradePlan']['stopLoss']}"
    )
    st.caption(f"模型摘要：{data['reason']}")

with top_right:
    cols = st.columns(2)
    cols[0].metric("收盘", data["weighted"]["close"])
    cols[1].metric("换手率", pct(data["weighted"]["turnover"]))
    cols[0].metric("KDJ-J", data["weighted"]["kdjJ"])
    cols[1].metric("BOLL位置", data["weighted"]["bollPct"])

st.subheader("模型结果")
st.dataframe(pd.DataFrame(data["models"]), use_container_width=True, hide_index=True)

chart_col, info_col = st.columns([1.1, 0.9])
with chart_col:
    st.subheader("近90日价格与BOLL")
    draw_price_chart(data["chart"])

with info_col:
    st.subheader("关键指标")
    base_metrics = [
        {"label": "MACD柱", "value": data["weighted"]["macdHist"]},
        {"label": "成本收益估计", "value": pct(data["weighted"]["chipProfit"])},
        {"label": "最近交易日", "value": data["latestDate"]},
        {"label": "量比", "value": data["weighted"]["volumeRatio"]},
    ]
    metric_table(base_metrics + data.get("quoteMetrics", []))

news_col, finance_col = st.columns([0.85, 1.15])
with news_col:
    st.subheader("重要讯息")
    news = data.get("news", [])
    if news:
        for item in news[:12]:
            title = item.get("title", "")
            meta = " · ".join([x for x in [item.get("source"), item.get("date")] if x])
            if item.get("url"):
                st.markdown(f"- [{title}]({item['url']})  \n  `{meta}`")
            else:
                st.markdown(f"- {title}  \n  `{meta}`")
    else:
        st.info("暂无可解析资讯")

with finance_col:
    st.subheader("最近财报")
    metrics = data.get("fundamentals", {}).get("metrics", [])
    if metrics:
        names = [m["label"] for m in metrics]
        selected = st.radio("财报指标", names, horizontal=True)
        metric = next(m for m in metrics if m["label"] == selected)
        draw_financial_metric(metric)
    else:
        st.info("暂无可解析财报数据")

st.caption("模型结果用于研究和交易前筛选，不构成投资建议。")
