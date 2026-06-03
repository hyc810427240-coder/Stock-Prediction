const form = document.getElementById("stockForm");
const input = document.getElementById("stockInput");
const runBtn = document.getElementById("runBtn");
const statusBox = document.getElementById("status");
const result = document.getElementById("result");
const chartCanvas = document.getElementById("priceChart");
const fundCanvas = document.getElementById("fundChart");
let activeFinancialMetric = "";

function fmt(value, suffix = "") {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
  return `${value}${suffix}`;
}

function signalClass(text) {
  if (text.includes("买入")) return "buy";
  if (text.includes("卖出")) return "sell";
  return "wait";
}

function setStatus(text, mode = "") {
  statusBox.textContent = text;
  statusBox.className = `status ${mode}`.trim();
}

function renderModels(models) {
  const body = document.getElementById("modelBody");
  body.innerHTML = models.map((m) => `
    <tr>
      <td>${m.name}</td>
      <td>${fmt(m.winRate, "%")}</td>
      <td>${fmt(m.avgReturn, "%")}</td>
      <td>${fmt(m.predWin, "%")}</td>
      <td>${fmt(m.predReturn, "%")}</td>
      <td><span class="tag ${signalClass(m.signal)}">${m.signal}</span></td>
      <td>${m.sampleSize}</td>
    </tr>
  `).join("");
}

function renderNews(news) {
  const list = document.getElementById("newsList");
  if (!news || news.length === 0) {
    list.innerHTML = "<li>东方财富、腾讯财经、同花顺和新浪财经本次均未返回可解析资讯。</li>";
    return;
  }
  list.innerHTML = news.map((item) => {
    const title = item.url ? `<a href="${item.url}" target="_blank" rel="noreferrer">${item.title}</a>` : item.title;
    const meta = [item.source, item.date].filter(Boolean).join(" · ");
    return `<li>${title}<small>${meta}</small></li>`;
  }).join("");
}

function renderQuoteMetrics(metrics) {
  const list = document.getElementById("indicatorList");
  const base = [
    ["MACD柱", fmt(window.currentData?.weighted?.macdHist)],
    ["成本收益估计", fmt(window.currentData?.weighted?.chipProfit, "%")],
    ["最近交易日", window.currentData?.latestDate || "--"],
    ["量比", fmt(window.currentData?.weighted?.volumeRatio)],
  ];
  const extra = (metrics || []).map((m) => [m.label, m.value]);
  list.innerHTML = base.concat(extra).map(([label, value]) => `
    <div><dt>${label}</dt><dd>${value}</dd></div>
  `).join("");
}

function renderFundamentals(fundamentals, selectedKey = "") {
  const wrap = document.getElementById("fundamentals");
  const tabs = document.getElementById("fundMetricTabs");
  const metrics = fundamentals?.metrics || [];
  if (!metrics.length) {
    tabs.innerHTML = "";
    wrap.textContent = "无可解析财报数据。";
    drawFinancialChart({ labels: [], values: [], yoy: [] });
    return;
  }
  activeFinancialMetric = selectedKey || activeFinancialMetric || fundamentals.active || metrics[0].key;
  const metric = metrics.find((item) => item.key === activeFinancialMetric) || metrics[0];
  activeFinancialMetric = metric.key;
  tabs.innerHTML = metrics.map((item) => `
    <button type="button" class="${item.key === activeFinancialMetric ? "active" : ""}" data-metric="${item.key}">${item.label}</button>
  `).join("");
  tabs.querySelectorAll("button").forEach((button) => {
    button.addEventListener("click", () => renderFundamentals(window.latestFundamentals, button.dataset.metric));
  });
  document.getElementById("fundChartMeta").textContent = `${metric.label} / 同比`;
  drawFinancialChart(metric);
  wrap.innerHTML = metric.rows.map((row) => {
    const yoy = row.yoy === null || row.yoy === undefined ? "--" : `${row.yoy}%`;
    const yoyClass = Number(row.yoy) >= 0 ? "rise" : "fall";
    return `
      <div class="financial-row">
        <span>${row.period}</span>
        <strong>${row.value}</strong>
        <em class="${yoyClass}">${yoy}</em>
      </div>
    `;
  }).join("");
}

function drawChart(chart) {
  const ctx = chartCanvas.getContext("2d");
  const rect = chartCanvas.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  chartCanvas.width = Math.max(1, Math.floor(rect.width * ratio));
  chartCanvas.height = Math.floor(rect.height * ratio);
  ctx.scale(ratio, ratio);

  const w = rect.width;
  const h = rect.height;
  ctx.clearRect(0, 0, w, h);

  const series = [chart.close, chart.ma20, chart.bollUp, chart.bollLow];
  const values = series.flat().filter((v) => v !== null && v !== undefined);
  if (values.length === 0) return;

  const pad = { left: 46, right: 14, top: 18, bottom: 34 };
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const x = (i) => pad.left + (i / Math.max(chart.dates.length - 1, 1)) * (w - pad.left - pad.right);
  const y = (v) => pad.top + ((max - v) / span) * (h - pad.top - pad.bottom);

  ctx.strokeStyle = "#d8ddd6";
  ctx.lineWidth = 1;
  ctx.beginPath();
  for (let i = 0; i < 4; i += 1) {
    const gy = pad.top + i * ((h - pad.top - pad.bottom) / 3);
    ctx.moveTo(pad.left, gy);
    ctx.lineTo(w - pad.right, gy);
  }
  ctx.stroke();

  ctx.fillStyle = "#65716c";
  ctx.font = "12px Microsoft YaHei, Arial";
  ctx.fillText(max.toFixed(2), 8, pad.top + 4);
  ctx.fillText(min.toFixed(2), 8, h - pad.bottom);
  ctx.fillText(chart.dates[0] || "", pad.left, h - 10);
  ctx.fillText(chart.dates[chart.dates.length - 1] || "", Math.max(pad.left, w - 98), h - 10);

  function line(data, color, width = 2) {
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.beginPath();
    let started = false;
    data.forEach((v, i) => {
      if (v === null || v === undefined) return;
      if (!started) {
        ctx.moveTo(x(i), y(v));
        started = true;
      } else {
        ctx.lineTo(x(i), y(v));
      }
    });
    ctx.stroke();
  }

  line(chart.bollUp, "#9aa6a1", 1.5);
  line(chart.bollLow, "#9aa6a1", 1.5);
  line(chart.ma20, "#b55d28", 1.8);
  line(chart.close, "#24745a", 2.4);

  const legend = [
    ["收盘", "#24745a"],
    ["MA20", "#b55d28"],
    ["BOLL", "#9aa6a1"],
  ];
  legend.forEach(([label, color], i) => {
    const lx = pad.left + i * 72;
    ctx.fillStyle = color;
    ctx.fillRect(lx, 10, 14, 3);
    ctx.fillStyle = "#65716c";
    ctx.fillText(label, lx + 20, 14);
  });
}

function drawFinancialChart(data) {
  const ctx = fundCanvas.getContext("2d");
  const rect = fundCanvas.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  fundCanvas.width = Math.max(1, Math.floor(rect.width * ratio));
  fundCanvas.height = Math.floor(rect.height * ratio);
  ctx.scale(ratio, ratio);

  const w = rect.width;
  const h = rect.height;
  ctx.clearRect(0, 0, w, h);

  const labels = data.labels || [];
  const values = data.values || [];
  const yoy = data.yoy || [];
  if (labels.length === 0 || values.length === 0) {
    ctx.fillStyle = "#65716c";
    ctx.font = "14px Microsoft YaHei, Arial";
    ctx.fillText("无可解析财报数据", 20, 40);
    return;
  }

  const pad = { left: 58, right: 58, top: 28, bottom: 48 };
  const plotW = w - pad.left - pad.right;
  const plotH = h - pad.top - pad.bottom;
  const minVal = Math.min(0, ...values);
  const maxVal = Math.max(0, ...values);
  const valSpan = maxVal - minVal || 1;
  const yoyVals = yoy.filter((v) => v !== null && v !== undefined && Number.isFinite(Number(v)));
  const minYoy = Math.min(0, ...yoyVals);
  const maxYoy = Math.max(0, ...yoyVals);
  const yoySpan = maxYoy - minYoy || 1;
  const x = (i) => pad.left + (i + 0.5) * (plotW / labels.length);
  const yVal = (v) => pad.top + ((maxVal - v) / valSpan) * plotH;
  const yYoy = (v) => pad.top + ((maxYoy - v) / yoySpan) * plotH;
  const zeroY = yVal(0);

  ctx.strokeStyle = "#d8ddd6";
  ctx.lineWidth = 1;
  ctx.beginPath();
  for (let i = 0; i < 4; i += 1) {
    const gy = pad.top + i * (plotH / 3);
    ctx.moveTo(pad.left, gy);
    ctx.lineTo(w - pad.right, gy);
  }
  ctx.moveTo(pad.left, zeroY);
  ctx.lineTo(w - pad.right, zeroY);
  ctx.stroke();

  ctx.font = "12px Microsoft YaHei, Arial";
  ctx.fillStyle = "#65716c";
  ctx.fillText(formatMoneyAxis(maxVal), 8, pad.top + 4);
  ctx.fillText(formatMoneyAxis(minVal), 8, pad.top + plotH);
  ctx.fillText(`${Math.round(maxYoy)}%`, w - 44, pad.top + 4);
  ctx.fillText(`${Math.round(minYoy)}%`, w - 44, pad.top + plotH);

  const barW = Math.min(52, plotW / labels.length * 0.42);
  values.forEach((v, i) => {
    const bx = x(i) - barW / 2;
    const by = Math.min(zeroY, yVal(v));
    const bh = Math.max(2, Math.abs(zeroY - yVal(v)));
    ctx.fillStyle = "#3578f6";
    ctx.fillRect(bx, by, barW, bh);
    ctx.fillStyle = "#65716c";
    ctx.textAlign = "center";
    ctx.fillText(data.valueText?.[i] || formatMoneyAxis(v), x(i), by - 6);
  });

  ctx.strokeStyle = "#e2a22a";
  ctx.lineWidth = 2;
  ctx.beginPath();
  let started = false;
  yoy.forEach((v, i) => {
    if (v === null || v === undefined) return;
    if (!started) {
      ctx.moveTo(x(i), yYoy(v));
      started = true;
    } else {
      ctx.lineTo(x(i), yYoy(v));
    }
  });
  ctx.stroke();
  yoy.forEach((v, i) => {
    if (v === null || v === undefined) return;
    ctx.fillStyle = "#e2a22a";
    ctx.beginPath();
    ctx.arc(x(i), yYoy(v), 4, 0, Math.PI * 2);
    ctx.fill();
  });

  ctx.textAlign = "center";
  labels.forEach((label, i) => {
    ctx.fillStyle = "#65716c";
    const parts = label.replace("年", "年|").split("|");
    ctx.fillText(parts[0], x(i), h - 26);
    if (parts[1]) ctx.fillText(parts[1], x(i), h - 10);
  });
  ctx.textAlign = "left";
  ctx.fillStyle = "#3578f6";
  ctx.fillRect(pad.left, 10, 12, 8);
  ctx.fillStyle = "#65716c";
  ctx.fillText(data.label || "财务指标", pad.left + 18, 18);
  ctx.fillStyle = "#e2a22a";
  ctx.fillRect(pad.left + 108, 13, 16, 3);
  ctx.fillStyle = "#65716c";
  ctx.fillText("同比", pad.left + 130, 18);
}

function formatMoneyAxis(value) {
  const abs = Math.abs(value || 0);
  const sign = value < 0 ? "-" : "";
  if (abs >= 100000000) return `${sign}${(abs / 100000000).toFixed(1)}亿`;
  if (abs >= 10000) return `${sign}${Math.round(abs / 10000)}万`;
  return `${sign}${Math.round(abs)}`;
}

function render(data) {
  window.currentData = data;
  document.getElementById("stockMeta").textContent = `${data.name}(${data.code}) · 最近交易日 ${data.latestDate} · ${data.generatedAt}`;
  document.getElementById("actionText").textContent = data.action;
  const stars = Math.max(1, Math.min(5, data.tradePlan.stars || 1));
  document.getElementById("starText").textContent = `${"★".repeat(stars)}${"☆".repeat(5 - stars)}`;
  document.getElementById("starScore").textContent = `${stars}/5`;
  document.getElementById("tradeAdvice").innerHTML = `
    <strong>第二天操作建议</strong>
    <span>${data.tradePlan.advice}</span>
    <em>支撑位 ${fmt(data.tradePlan.support)} · 压力位 ${fmt(data.tradePlan.resistance)} · 止损参考 ${fmt(data.tradePlan.stopLoss)}</em>
  `;
  document.getElementById("reasonText").textContent = `模型摘要：${data.reason}`;
  document.getElementById("closeMetric").textContent = fmt(data.weighted.close);
  document.getElementById("turnoverMetric").textContent = fmt(data.weighted.turnover, "%");
  document.getElementById("kdjMetric").textContent = fmt(data.weighted.kdjJ);
  document.getElementById("bollMetric").textContent = fmt(data.weighted.bollPct);
  document.getElementById("targetDef").textContent = data.weighted.targetDefinition;
  document.getElementById("dataSource").textContent = data.dataSource;

  renderQuoteMetrics(data.quoteMetrics);
  renderModels(data.models);
  renderNews(data.news);
  renderFundamentals(data.fundamentals);
  window.latestChart = data.chart;
  window.latestFundamentals = data.fundamentals;
  result.classList.remove("hidden");
  requestAnimationFrame(() => {
    drawChart(data.chart);
    renderFundamentals(data.fundamentals, activeFinancialMetric);
  });
}

async function runAnalysis(stock) {
  setStatus("正在获取行情、财报和资讯，并训练模型。首次运行通常需要 10-30 秒。", "busy");
  runBtn.disabled = true;
  try {
    const res = await fetch("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ stock }),
    });
    const payload = await res.json();
    if (!payload.ok) throw new Error(payload.error || "模型运行失败");
    render(payload.data);
    setStatus("模型运行完成。");
  } catch (err) {
    result.classList.add("hidden");
    setStatus(`运行失败：${err.message}`, "error");
  } finally {
    runBtn.disabled = false;
  }
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  runAnalysis(input.value.trim() || "城地香江");
});

window.addEventListener("resize", () => {
  if (!result.classList.contains("hidden") && window.latestChart) {
    drawChart(window.latestChart);
    renderFundamentals(window.latestFundamentals, activeFinancialMetric);
  }
});
