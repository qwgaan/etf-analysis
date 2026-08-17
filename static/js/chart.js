/* ETF 分析工作台 - ECharts 图表渲染
   依赖: utils.js(提供 $/fmt/fmtPct)、app.js(提供 state)
   ----------------------------------------- */

function windowedCategories() {
  if (!state.chartData) return null;
  const { chart, summary } = state.chartData;
  const cats = chart.categories;
  if (state.currentRange === "year") {
    // 取今年以来数据
    const year = new Date(summary.last_date).getFullYear().toString();
    const idx = cats.findIndex(d => d.startsWith(year));
    return idx >= 0 ? { from: idx, to: cats.length - 1 } : { from: 0, to: cats.length - 1 };
  }
  if (state.currentRange === "week52") {
    return { from: Math.max(0, cats.length - 260), to: cats.length - 1 };
  }
  return { from: 0, to: cats.length - 1 };
}

// ---------- 主图:K线 + MA4条 + 成交量 ----------
function drawMainChart() {
  const { chart } = state.chartData;
  const win = windowedCategories();
  if (!win) return;
  const slice = (arr) => arr.slice(win.from, win.to + 1);
  const dom = $("#main-chart");
  const ec = echarts.init(dom);
  ec.setOption({
    backgroundColor: "transparent",
    animation: false,
    legend: { top: 4, textStyle: { fontSize: 11 } },
    tooltip: { trigger: "axis", axisPointer: { type: "cross" } },
    grid: [
      { left: 60, right: 16, top: 32, height: 220 },     // K线
      { left: 60, right: 16, top: 268, height: 60 },     // 成交量
    ],
    xAxis: [
      { type: "category", data: slice(chart.categories), scale: true,
        boundaryGap: false, axisLabel: { fontSize: 10 } },
      { type: "category", data: slice(chart.categories), gridIndex: 1,
        scale: true, boundaryGap: false, axisLabel: { show: false } },
    ],
    yAxis: [
      { scale: true, axisLabel: { fontSize: 10 }, splitLine: { lineStyle: { color: "#f0f1f3" } } },
      { gridIndex: 1, axisLabel: { fontSize: 10 }, splitNumber: 2 },
    ],
    dataZoom: [
      { type: "inside", xAxisIndex: [0, 1] },
      { type: "slider", xAxisIndex: [0, 1], height: 14, bottom: 12 },
    ],
    series: [
      {
        type: "candlestick", name: "K线",
        data: slice(chart.categories).map((d, i) => [
          chart.open[win.from + i], chart.close[win.from + i],
          chart.low[win.from + i], chart.high[win.from + i],
        ]),
        itemStyle: {
          color: "#dc2626", color0: "#16a34a",
          borderColor: "#dc2626", borderColor0: "#16a34a",
        },
      },
      { name: "MA20", type: "line", data: slice(chart.ma20), smooth: true, lineStyle: { width: 1 }, symbol: "none" },
      { name: "MA50", type: "line", data: slice(chart.ma50), smooth: true, lineStyle: { width: 1 }, symbol: "none" },
      { name: "MA150", type: "line", data: slice(chart.ma150), smooth: true, lineStyle: { width: 1 }, symbol: "none" },
      { name: "MA200", type: "line", data: slice(chart.ma200), smooth: true, lineStyle: { width: 1 }, symbol: "none" },
      { name: "成交量", type: "bar", xAxisIndex: 1, yAxisIndex: 1,
        data: slice(chart.volume), itemStyle: { color: "#9ca3af" } },
    ],
  });
  window._ecMain = ec;
}

// ---------- 当天分时图(1 分钟折线 + 均价线 + 成交量) ----------
function drawIntradayChart(data) {
  const dom = $("#main-chart");
  const ec = echarts.init(dom);
  const upColor = "#dc2626";
  const downColor = "#16a34a";

  // 成交量按当前价 vs 昨收 着色(更贴合分时图习惯)
  const prevClose = data.summary && data.summary.prev_close;
  const volumes = data.close.map((c, i) => ({
    value: data.volume[i],
    itemStyle: { color: prevClose ? (c >= prevClose ? upColor : downColor) : "#9ca3af" },
  }));

  // 昨收参考线(水平虚线)
  const markLines = [];
  if (prevClose) {
    markLines.push({
      yAxis: prevClose,
      label: { formatter: "昨收 " + fmt(prevClose), fontSize: 9, position: "insideEndTop" },
      lineStyle: { color: "#6b7280", type: "dashed", width: 1 },
    });
  }

  ec.setOption({
    backgroundColor: "transparent",
    animation: false,
    legend: { top: 4, textStyle: { fontSize: 11 }, data: ["现价", "均价", "成交量"] },
    tooltip: { trigger: "axis", axisPointer: { type: "cross" } },
    grid: [
      { left: 60, right: 16, top: 32, height: 220 },
      { left: 60, right: 16, top: 268, height: 60 },
    ],
    xAxis: [
      { type: "category", data: data.times, scale: true, boundaryGap: false, axisLabel: { fontSize: 10 } },
      { type: "category", data: data.times, gridIndex: 1, scale: true, boundaryGap: false, axisLabel: { show: false } },
    ],
    yAxis: [
      { scale: true, axisLabel: { fontSize: 10 }, splitLine: { lineStyle: { color: "#f0f1f3" } } },
      { gridIndex: 1, axisLabel: { fontSize: 10 }, splitNumber: 2 },
    ],
    dataZoom: [
      { type: "inside", xAxisIndex: [0, 1] },
      { type: "slider", xAxisIndex: [0, 1], height: 14, bottom: 12 },
    ],
    series: [
      {
        name: "现价",
        type: "line",
        data: data.close,
        smooth: false,
        symbol: "none",
        lineStyle: { color: "#2563eb", width: 2 },
        markLine: { symbol: "none", data: markLines, animation: false },
      },
      {
        name: "均价",
        type: "line",
        data: data.vwap,
        smooth: false,
        symbol: "none",
        lineStyle: { color: "#f59e0b", width: 1.5, type: "dashed" },
      },
      { name: "成交量", type: "bar", xAxisIndex: 1, yAxisIndex: 1, data: volumes },
    ],
  });
  window._ecMain = ec;
}

// ---------- BIAS 副图 ----------
function drawBiasChart() {
  const { chart } = state.chartData;
  const win = windowedCategories();
  if (!win) return;
  const slice = arr => arr.slice(win.from, win.to + 1);
  const ec = echarts.init($("#bias-chart"));
  const bcfg = state.config.bias_thresholds;
  const levels = [
    ...(bcfg.bias20_levels || []).map(v => ({ value: v, label: `BIAS20 ${v}%`, color: v >= 15 ? "#dc2626" : "#d97706" })),
    ...(bcfg.bias60_levels || []).map(v => ({ value: v, label: `BIAS60 ${v}%`, color: "#7c3aed" })),
    0,
  ];
  ec.setOption({
    backgroundColor: "transparent",
    legend: { top: 2, textStyle: { fontSize: 10 } },
    tooltip: { trigger: "axis" },
    grid: { left: 60, right: 16, top: 26, bottom: 26 },
    xAxis: { type: "category", data: slice(chart.categories), axisLabel: { fontSize: 10 } },
    yAxis: {
      type: "value", axisLabel: { fontSize: 10, formatter: "{value}%" },
      splitLine: { lineStyle: { color: "#f0f1f3" } },
    },
    series: [
      { name: "BIAS20", type: "line", data: slice(chart.bias20), smooth: true, symbol: "none",
        lineStyle: { color: "#ea580c", width: 1.2 },
        markLine: {
          symbol: "none",
          data: (bcfg.bias20_levels || []).map(v => ({
            yAxis: v, label: { formatter: v + "%", fontSize: 9 }, lineStyle: { color: v >= 15 ? "#dc2626" : "#d97706", type: "dashed" },
          })),
        },
      },
      { name: "BIAS60", type: "line", data: slice(chart.bias60), smooth: true, symbol: "none",
        lineStyle: { color: "#7c3aed", width: 1.2 },
        markLine: {
          symbol: "none",
          data: (bcfg.bias60_levels || []).map(v => ({
            yAxis: v, label: { formatter: v + "%", fontSize: 9 }, lineStyle: { color: "#7c3aed", type: "dashed" },
          })),
        },
      },
    ],
  });
  window._ecBias = ec;
}

// ---------- 距高/低点 柱状图 ----------
function drawRangeChart() {
  const { chart, summary } = state.chartData;
  const win = windowedCategories();
  if (!win) return;
  const slice = arr => arr.slice(win.from, win.to + 1);
  const ec = echarts.init($("#range-chart"));

  // 选距高/低 用周或年,依据当前 range
  let distHi, distLo;
  if (state.currentRange === "week52") {
    distHi = chart.high_52w.map((h, i) => h ? (chart.close[i] - h) / h * 100 : null);
    distLo = chart.low_52w.map((lo, i) => lo ? (chart.close[i] - lo) / lo * 100 : null);
  } else if (state.currentRange === "year") {
    distHi = chart.yearly_high.map((h, i) => h ? (chart.close[i] - h) / h * 100 : null);
    distLo = chart.yearly_low.map((lo, i) => lo ? (chart.close[i] - lo) / lo * 100 : null);
  } else {
    distHi = chart.high_52w.map((h, i) => h ? (chart.close[i] - h) / h * 100 : null);
    distLo = chart.low_52w.map((lo, i) => lo ? (chart.close[i] - lo) / lo * 100 : null);
  }

  ec.setOption({
    backgroundColor: "transparent",
    legend: { top: 2, textStyle: { fontSize: 10 } },
    tooltip: { trigger: "axis", valueFormatter: v => (v == null ? "—" : v.toFixed(2) + "%") },
    grid: { left: 60, right: 16, top: 26, bottom: 26 },
    xAxis: { type: "category", data: slice(chart.categories), axisLabel: { fontSize: 10 } },
    yAxis: { type: "value", axisLabel: { fontSize: 10, formatter: "{value}%" },
      splitLine: { lineStyle: { color: "#f0f1f3" } } },
    series: [
      { name: "距高回撤", type: "bar", stack: "r", data: slice(distHi),
        itemStyle: { color: "#dc2626" } },
      { name: "距低反弹", type: "bar", stack: "r", data: slice(distLo),
        itemStyle: { color: "#16a34a" } },
    ],
  });
  window._ecRange = ec;
}
