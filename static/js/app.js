/* ETF 分析 - 前端主控
   -----------------------------------------
   流程:
   1. 启动: 拉 ETF 列表 + 当前配置 + 缓存默认 BIAS/DD 阈值
   2. 用户点 ETF -> 拉图表 + 摘要 + 信号面板
   3. 顶部 "运行筛选" -> 拉 /api/screen 填底部表
   4. 配置抽屉: 实时编辑,Save 写回,Reset 还原
*/
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
  etfs: [],
  etfIndex: new Map(),
  selectedCode: null,
  chartData: null,
  currentRange: "year",
  config: null,
  defaults: null,
  screenData: [],
  screenSort: { key: "fully_passed", dir: -1 },
  enabledRules: [1, 2, 3, 4, 5, 6],   // 当前启用的规则编号(后端返回)
  passedCodes: null,                  // ETF 列表页「仅显示全过」时缓存的通过代码集合
};

// ---------- 菜单导航切换 ----------
function switchView(view) {
  $$(".tab").forEach(t => t.classList.toggle("active", t.dataset.view === view));
  $("#view-etf").classList.toggle("hidden", view !== "etf");
  $("#view-screen").classList.toggle("hidden", view !== "screen");
  $("#view-watch").classList.toggle("hidden", view !== "watch");
  if (view === "etf") {
    setTimeout(() => {
      [window._ecMain, window._ecBias, window._ecRange].forEach(c => c && c.resize());
    }, 50);
  }
  if (view === "watch") {
    loadGroups();
  }
}
$$(".tab").forEach(t => t.addEventListener("click", () => switchView(t.dataset.view)));

// ---------- toast ----------
function toast(msg, type = "info") {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.remove("hidden");
  if (type === "error") el.style.background = "#dc2626";
  else if (type === "success") el.style.background = "#16a34a";
  else el.style.background = "rgba(20,30,50,0.92)";
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add("hidden"), 2200);
}

// ---------- 工具 ----------
function fmt(v, digits = 2) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return Number(v).toFixed(digits);
}
function fmtPct(v, digits = 2) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const sign = v > 0 ? "+" : "";
  return sign + Number(v).toFixed(digits) + "%";
}
function fmtK(v) {
  if (!v) return "—";
  if (v > 1e8) return (v / 1e8).toFixed(2) + "亿";
  if (v > 1e4) return (v / 1e4).toFixed(2) + "万";
  return v.toFixed(0);
}
function arraySlice(arr, n) {
  return arr.slice(Math.max(0, arr.length - n));
}

// ---------- 加载初始数据 ----------
async function bootstrap() {
  try {
    const [etfsRes, cfgRes] = await Promise.all([
      fetch("/api/etfs").then(r => r.json()),
      fetch("/api/config").then(r => r.json()),
    ]);
    state.etfs = etfsRes.items;
    state.etfIndex = new Map(state.etfs.map(e => [e.code, e]));
    state.config = cfgRes.current;
    state.defaults = cfgRes.diff;
    renderETFList();
    renderConfigForm();
    syncRuleCheckboxes();
    toast(`加载完成 · ${state.etfs.length} 只 ETF`, "success");
  } catch (e) {
    toast("初始化失败: " + e.message, "error");
  }
}

async function renderETFList() {
  const ul = $("#etf-list");
  const q = $("#etf-search").value.trim().toLowerCase();
  const onlyPassed = $("#filter-fully-passed").checked;

  // 先按搜索关键字过滤
  let items = state.etfs.filter(e =>
    !q || e.code.includes(q) || (e.name || "").toLowerCase().includes(q)
  );

  if (onlyPassed) {
    if (!state.passedCodes) {
      ul.innerHTML = `<li class="etf-loading"><div>正在加载全过规则列表...</div></li>`;
      $("#etf-count").textContent = "—";
      await loadPassedCodes();
      return; // loadPassedCodes 会再次调用 renderETFList
    }
    items = items.filter(e => state.passedCodes.has(e.code));
  }

  $("#etf-count").textContent = items.length;
  if (!items.length) {
    ul.innerHTML = `<li class="etf-empty"><div>${onlyPassed ? "没有符合所有启用规则的 ETF" : "无匹配结果"}</div></li>`;
    return;
  }
  ul.innerHTML = items.slice(0, 200).map(e => `
    <li data-code="${e.code}">
      <div>
        <div class="name">${e.name}</div>
        <div class="code">${e.code}</div>
      </div>
    </li>
  `).join("");
  $$("#etf-list li").forEach(li => {
    li.addEventListener("click", () => selectETF(li.dataset.code));
  });
}

async function loadPassedCodes() {
  try {
    const params = new URLSearchParams({ only_pass: "1", use_cache: "1" });
    const r = await fetch(`/api/screen?${params}`).then(r => r.json());
    const codes = new Set((r.items || []).map(x => x.code));
    state.passedCodes = codes;
    $("#etf-count").textContent = codes.size;
  } catch (e) {
    toast("加载全过列表失败: " + e.message, "error");
    state.passedCodes = new Set();
  }
  renderETFList();
}

$("#etf-search").addEventListener("input", renderETFList);
$("#filter-fully-passed").addEventListener("change", renderETFList);

// ---------- 选 ETF ----------
async function selectETF(code) {
  state.selectedCode = code;
  $$("#etf-list li").forEach(li => li.classList.toggle("active", li.dataset.code === code));
  const meta = state.etfIndex.get(code);
  $("#chart-title").textContent = `${code} · ${meta ? meta.name : ""}`;
  $("#chart-meta").textContent = "加载中...";
  try {
    const r = await fetch(`/api/chart/${code}`).then(r => r.json());
    if (!r.ok) throw new Error(r.error || "拉取失败");
    state.chartData = r;
    $("#chart-meta").textContent =
      `数据 ${r.summary.total_days} 个交易日 · 截止 ${r.summary.last_date} · 现价 ${fmt(r.summary.last_close)}`;
    drawMainChart();
    drawBiasChart();
    drawRangeChart();
    renderSummary(r.summary);
    renderSignals(r.summary);
    evaluateMarkForCurrent(r);
  } catch (e) {
    toast("加载失败: " + e.message, "error");
    $("#chart-meta").textContent = "—";
  }
}

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
  let distHi, distLo, hiKey, loKey;
  if (state.currentRange === "week52") {
    hiKey = "high_52w"; loKey = "low_52w";
    distHi = chart.high_52w.map((h, i) => h ? (chart.close[i] - h) / h * 100 : null);
    distLo = chart.low_52w.map((lo, i) => lo ? (chart.close[i] - lo) / lo * 100 : null);
  } else if (state.currentRange === "year") {
    hiKey = "yearly_high"; loKey = "yearly_low";
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

// ---------- 摘要 + 信号面板 ----------
function renderSummary(s) {
  $("#sum-close").textContent = fmt(s.last_close, 3);
  $("#sum-52").textContent = `${fmt(s.high_52w, 3)} / ${fmt(s.low_52w, 3)}`;
  $("#sum-ytd").textContent = `${fmt(s.ytd_high, 3)} / ${fmt(s.ytd_low, 3)}`;
  $("#sum-dist-52").innerHTML = `${fmtPct(s.dist_52w_high_pct)} / <b style="color:#16a34a">${fmtPct(s.dist_52w_low_pct)}</b>`;
  $("#sum-dist-ytd").innerHTML = `${fmtPct(s.dist_ytd_high_pct)} / <b style="color:#16a34a">${fmtPct(s.dist_ytd_low_pct)}</b>`;
  // 详情摘要显示真正的「年内最大回撤」(含低点日期/价格),与警戒档的「当前价格年内回撤」区分
  const ddMax = s.ytd_max_drawdown;
  const ddMaxDate = s.ytd_max_drawdown_date;
  const ddMaxPrice = s.ytd_max_drawdown_price;
  const ddEl = $("#sum-ytd-dd");
  if (ddMax != null && !isNaN(ddMax)) {
    const extra = ddMaxPrice != null ? `(低点 ${fmt(ddMaxPrice, 3)} @ ${ddMaxDate || "—"})` : "";
    ddEl.innerHTML = `${fmtPct(ddMax)} <span class="muted">${extra}</span>`;
    ddEl.className = ddMax < 0 ? "down" : "";
  } else {
    ddEl.textContent = "—";
    ddEl.className = "";
  }
}

function renderSignals(s) {
  // BIAS20
  const bcfg = state.config.bias_thresholds;
  drawBiasSignal("bias20", s.bias20_now, [
    ...(bcfg.bias20_levels || []).map(v => ({ v, text: `> ${v}%` })),
  ]);
  drawBiasSignal("bias60", s.bias60_now, [
    ...(bcfg.bias60_levels || []).map(v => ({ v, text: `> ${v}%` })),
  ]);

  // 今年回撤
  const dcfg = state.config.drawdown_thresholds;
  drawDrawdownSignal(s.ytd_drawdown, dcfg.ytd_levels, dcfg.ytd_level_tags || []);

  // 信号总览
  const reason = explainOverall(s);
  $("#signal-tag").textContent = reason.tag;
  $("#signal-tag").className = "pill " + reason.cls;
}

function drawBiasSignal(key, val, levels) {
  if (val == null) {
    $(`#${key}-value`).textContent = "—";
    $(`#${key}-bar`).style.width = "0%";
    $(`#${key}-reasons`).textContent = "—";
    return;
  }
  const el = $(`#${key}-value`);
  el.textContent = fmtPct(val);
  el.className = "value " + (val > 0 ? "up" : "down");
  // 进度条:缩放到 -10 .. +20,clamp
  const pct = Math.max(-100, Math.min(100, ((val + 10) / 30) * 100));
  $(`#${key}-bar`).style.width = pct + "%";
  $(`#${key}-bar`).style.background = val > 15 ? "#dc2626" : val > 10 ? "#ea580c" : val > 0 ? "#4f46e5" : "#16a34a";
  // 文案
  const triggered = levels.filter(l => val >= l.v);
  let txt = triggered.length ? `已触发: ${triggered.map(l => l.text).join("、")}` : "未触发警戒";
  $(`#${key}-reasons`).textContent = txt;
}

function drawDrawdownSignal(val, levels, tags) {
  const el = $("#dd-value");
  el.textContent = fmtPct(val);
  el.className = "value " + (val < -5 ? "down" : "up");
  const maxAbs = Math.max(20, Math.abs(val || 0));
  const pct = Math.min(100, (Math.abs(val || 0) / maxAbs) * 100);
  $("#dd-bar").style.width = pct + "%";
  $("#dd-bar").style.background = "#dc2626";
  const triggered = [];
  if (levels && levels.length) {
    for (let i = 0; i < levels.length; i++) {
      if (val <= -levels[i]) {
        triggered.push(`${tags[i] || levels[i] + "%"} (${levels[i]}%)`);
      }
    }
  }
  $("#dd-reasons").textContent = triggered.length
    ? `已抵档位: ${triggered.join("、")}`
    : "未抵任何警戒档";
}

function explainOverall(s) {
  const parts = [];
  if (s.bias20_now >= 15 || s.bias60_now >= 20) parts.push("BIAS 触顶");
  else if (s.bias20_now >= 10) parts.push("BIAS 高位");
  if (s.ytd_drawdown <= -20) parts.push("深度回撤");
  else if (s.ytd_drawdown <= -10) parts.push("中度回撤");
  if (!parts.length) return { tag: "常态", cls: "neutral" };
  if (parts.some(p => p.includes("触顶") || p.includes("深度"))) return { tag: parts.join(" · "), cls: "red" };
  return { tag: parts.join(" · "), cls: "green" };
}

// ---------- Mark 模板对当前 ETF 的评估 ----------
function evaluateMarkForCurrent(r) {
  if (!r || !state.chartData) return;
  const fcfg = state.config.mark_filter;
  const win = windowedCategories();
  const chart = state.chartData.chart;
  const slice = arr => arr.slice(win.from, win.to + 1);
  const close = slice(chart.close);
  const ma50 = slice(chart.ma50);
  const ma150 = slice(chart.ma150);
  const ma200 = slice(chart.ma200);
  const last = arr => arr[arr.length - 1];
  const c = last(close), m50 = last(ma50), m150 = last(ma150), m200 = last(ma200);

  // 规则 1
  let r1ok, r1why = "";
  if (!fcfg.rule1_enabled) { r1ok = true; r1why = "已关闭"; }
  else if (fcfg.rule1_strict_alignment) {
    r1ok = c && m50 && m150 && m200 && c > m50 && m50 > m150 && m150 > m200;
    r1why = `close ${fmt(c)} > MA50 ${fmt(m50)} > MA150 ${fmt(m150)} > MA200 ${fmt(m200)}`;
  } else {
    r1ok = c && m200 && c > m200;
    r1why = `close ${fmt(c)} vs MA200 ${fmt(m200)}`;
  }
  // 规则 2:MA200 斜率
  let r2ok = false, r2why = "—";
  if (!fcfg.rule2_enabled) { r2ok = true; r2why = "已关闭"; }
  else {
    const look = fcfg.rule2_lookback || 20;
    const arr = ma200;
    if (arr.length < look) { r2ok = false; r2why = `MA200 不足 ${look} 日样本`; }
    else {
      const seg = arr.slice(-look);
      const xs = Array.from({ length: look }, (_, i) => i);
      const mx = xs.reduce((a, b) => a + b, 0) / look;
      const my = seg.reduce((a, b) => a + (b || 0), 0) / look;
      let num = 0, den = 0;
      xs.forEach((x, i) => { num += (x - mx) * (seg[i] - my); den += (x - mx) * (x - mx); });
      const slope = den ? num / den : 0;
      r2ok = slope > (fcfg.rule2_min_slope || 0);
      r2why = `近 ${look} 日斜率 ${slope.toFixed(4)} > ${fcfg.rule2_min_slope || 0}`;
    }
  }
  // 规则 3/4 — 用 server 返回的 dist
  const r3ok = fcfg.rule3_enabled
    ? (r.summary.dist_52w_low_pct != null && r.summary.dist_52w_low_pct >= (fcfg.rule3_min_distance_pct || 25))
    : true;
  r3why = fcfg.rule3_enabled
    ? `距 ${fcfg.rule3_window_weeks} 周低点 ${fmtPct(r.summary.dist_52w_low_pct)} (阈值 ≥${fcfg.rule3_min_distance_pct}%)`
    : "已关闭";
  const r4ok = fcfg.rule4_enabled
    ? (r.summary.dist_52w_high_pct != null && r.summary.dist_52w_high_pct >= -(fcfg.rule4_max_distance_pct || 25))
    : true;
  r4why = fcfg.rule4_enabled
    ? `距 ${fcfg.rule4_window_weeks} 周高点 ${fmtPct(r.summary.dist_52w_high_pct)} (阈值 ≥-${fcfg.rule4_max_distance_pct}%)`
    : "已关闭";
  // 规则 5/6 — 今年低点/高点
  const r5ok = fcfg.rule5_enabled
    ? (r.summary.dist_ytd_low_pct != null && r.summary.dist_ytd_low_pct >= (fcfg.rule5_min_distance_pct || 15))
    : true;
  r5why = fcfg.rule5_enabled
    ? `距今年低点 ${fmtPct(r.summary.dist_ytd_low_pct)} (阈值 ≥${fcfg.rule5_min_distance_pct}%)`
    : "已关闭";
  const r6ok = fcfg.rule6_enabled
    ? (r.summary.dist_ytd_high_pct != null && r.summary.dist_ytd_high_pct >= -(fcfg.rule6_max_distance_pct || 25))
    : true;
  r6why = fcfg.rule6_enabled
    ? `距今年高点 ${fmtPct(r.summary.dist_ytd_high_pct)} (阈值 ≥-${fcfg.rule6_max_distance_pct}%)`
    : "已关闭";

  function setRule(key, ok, why) {
    const li = $(`#mark-rules li[data-key="${key}"]`);
    if (!li) return;
    li.classList.toggle("pass", ok);
    li.querySelector(".status").textContent = ok ? "✓ 通过" : "× 未过";
    li.querySelector(".status").className = "status " + (ok ? "pass" : "fail");
    li.querySelector(".reason").textContent = why;
  }
  setRule("rule1", r1ok, r1why);
  setRule("rule2", r2ok, r2why);
  setRule("rule3", r3ok, r3why);
  setRule("rule4", r4ok, r4why);
  setRule("rule5", r5ok, r5why);
  setRule("rule6", r6ok, r6why);
}

// ---------- range 切换 ----------
$$(".range-toggle button").forEach(b => {
  b.addEventListener("click", () => {
    $$(".range-toggle button").forEach(x => x.classList.toggle("active", x === b));
    state.currentRange = b.dataset.range;
    if (state.chartData) {
      drawMainChart();
      drawBiasChart();
      drawRangeChart();
    }
  });
});

// ---------- 全市场筛选 ----------
$("#btn-screen").addEventListener("click", async () => {
  const status = $("#screen-status");
  status.textContent = "正在跑 Mark 模板筛选(优先本地缓存)...";
  const params = new URLSearchParams({ use_cache: "1" });
  const screenLimit = (state.config && state.config.display && state.config.display.screen_limit) || 0;
  if (screenLimit > 0) params.set("limit", String(screenLimit));
  if ($("#filter-fully-passed-screen").checked) params.set("only_pass", "1");

  // 进度条 UI
  const prog = $("#screen-progress");
  const progLabel = $("#screen-progress-label");
  const progCounts = $("#screen-progress-counts");
  const progFill = $("#screen-progress-fill");
  prog.classList.remove("hidden");
  progFill.style.width = "0%";
  progLabel.textContent = "准备中...";
  progCounts.textContent = "0 / 0";

  let pollTimer = null;
  pollTimer = setInterval(async () => {
    try {
      const p = await fetch("/api/screen/progress").then(r => r.json());
      if (!p.running && p.phase !== "完成" && p.total === 0) return; // 后端还没真正开始
      const pct = p.total > 0
        ? Math.min(100, Math.round((p.done / p.total) * 100))
        : (p.phase === "完成" ? 100 : 0);
      progFill.style.width = pct + "%";
      progLabel.textContent = p.phase || "筛选中...";
      let txt = `${p.done} / ${p.total}`;
      if (p.matched > 0) txt += ` · 已匹配 ${p.matched}`;
      progCounts.textContent = txt;
    } catch (e) { /* 忽略轮询瞬时错误 */ }
  }, 350);

  try {
    const r = await fetch(`/api/screen?${params}`).then(r => r.json());
    if (pollTimer) clearInterval(pollTimer);
    prog.classList.add("hidden");
    state.screenData = r.items || [];
    state.enabledRules = r.enabled_rules || [1, 2, 3, 4];
    updateScreenHeader();
    renderScreenTable();
    const scanned = r.scanned ?? state.screenData.length;
    status.textContent = `完成 · 扫描 ${scanned} 只 / 共 ${r.total ?? "?"} 只 · 命中 ${r.count}`;
    // Mark 模板面板的"符合条件"计数
    $("#mtp-matched-screen").textContent = r.matched ?? "—";
    $("#mtp-scanned-screen").textContent = scanned;
    toast(`筛选完成 · 命中 ${r.count} 只`, "success");
  } catch (e) {
    if (pollTimer) clearInterval(pollTimer);
    prog.classList.add("hidden");
    status.textContent = "筛选失败";
    toast("筛选失败: " + e.message, "error");
  }
});

// 规则名表头映射(短名,etfwin 风格)
const RULE_LABELS = {
  1: "均线",
  2: "MA200↑",
  3: "远离低",
  4: "接近高",
  5: "远离年低",
  6: "接近年高",
};

// 按当前启用的规则动态构建表头(代码/名称/价格/BIAS/回撤 + N 个规则列)
function updateScreenHeader() {
  const head = $("#screen-table thead tr");
  if (!head) return;
  const fixed = `
    <th data-sort="code">代码</th>
    <th data-sort="name">名称</th>
    <th data-sort="close">现价</th>
    <th data-sort="bias20">BIAS20</th>
    <th data-sort="bias60">BIAS60</th>
    <th data-sort="ytd_drawdown">今年回撤</th>
    <th data-sort="dd52w">52周回撤</th>`;
  const rules = state.enabledRules.map(n =>
    `<th data-sort="rule${n}" title="规则 ${n}">${RULE_LABELS[n] || "规则" + n}</th>`
  ).join("");
  head.innerHTML = fixed + rules;
  // 重新绑定排序事件(原 thead 上的事件是固定的,重建后失效)
  $$("#screen-table thead th").forEach(th => {
    th.addEventListener("click", () => {
      const k = th.dataset.sort;
      if (!k) return;
      if (state.screenSort.key === k) state.screenSort.dir *= -1;
      else { state.screenSort.key = k; state.screenSort.dir = -1; }
      renderScreenTable();
    });
  });
}

function renderScreenTable() {
  const tbody = $("#screen-tbody");
  if (!state.screenData.length) {
    tbody.innerHTML = `<tr><td colspan="11" style="text-align:center;color:#999;padding:20px">
      暂无数据 —— 点击上方「运行筛选」,或先用 \`python warmup.py\` 预热全市场 K 线缓存</td></tr>`;
    return;
  }
  const sort = state.screenSort;
  const data = [...state.screenData];
  data.sort((a, b) => {
    const va = a[sort.key] ?? -1e9, vb = b[sort.key] ?? -1e9;
    return sort.dir * (va > vb ? 1 : va < vb ? -1 : 0);
  });
  // 根据当前启用的规则动态生成规则列
  const ruleCells = (r) => state.enabledRules.map(n =>
    `<td title="规则 ${n}: ${r.rules["rule" + n].reason}">${dot(r.rules["rule" + n].ok)}</td>`
  ).join("");
  tbody.innerHTML = data.map(r => `
    <tr class="${r.fully_passed ? "full-pass" : ""}" data-code="${r.code}" style="cursor:pointer">
      <td>${r.code}</td>
      <td>${r.name}</td>
      <td>${fmt(r.close, 3)}</td>
      <td class="${r.bias20 > 0 ? "up" : "down"}">${fmtPct(r.bias20)}</td>
      <td class="${r.bias60 > 0 ? "up" : "down"}">${fmtPct(r.bias60)}</td>
      <td class="${r.ytd_drawdown < 0 ? "down" : ""}">${fmtPct(r.ytd_drawdown)}</td>
      <td class="${r.dd52w < 0 ? "down" : ""}">${fmtPct(r.dd52w)}</td>
      ${ruleCells(r)}
    </tr>
  `).join("");
  $$("#screen-tbody tr").forEach(tr => tr.addEventListener("click", () => {
    switchView("etf");           // 切回单只分析视图
    selectETF(tr.dataset.code);  // 并选中该 ETF
  }));
}

function dot(ok) {
  return `<span class="dot ${ok ? "ok" : "no"}">${ok ? "●" : "○"}</span>`;
}

// 表头排序事件已移到 updateScreenHeader() 内(动态构建后立即重新绑定)。

// ---------- 导出 ----------
$("#btn-export-csv").addEventListener("click", () => window.location = "/api/export/csv");
$("#btn-export-json").addEventListener("click", () => window.location = "/api/export/json");

// ---------- 版本说明 ----------
$("#btn-version").addEventListener("click", openVersionModal);
$("#version-close").addEventListener("click", () => $("#version-modal").classList.add("hidden"));
$("#version-overlay").addEventListener("click", () => $("#version-modal").classList.add("hidden"));

function parseVer(s) {
  if (!s) return null;
  const m = String(s).replace(/^v/i, "").match(/^(\d+)\.(\d+)\.(\d+)/);
  return m ? parseInt(m[1]) * 1e6 + parseInt(m[2]) * 1e3 + parseInt(m[3]) : null;
}

function openVersionModal() {
  $("#version-modal").classList.remove("hidden");
  const cur = $("#ver-current"), lat = $("#ver-latest"), st = $("#ver-status");
  cur.textContent = "检测中…";
  lat.textContent = "检测中…";
  st.className = "ver-status";
  st.textContent = "正在检查更新…";
  $("#ver-link-wrap").classList.add("hidden");
  fetch("/api/version").then(r => r.json()).then(v => {
    const ver = v.version || "?";
    cur.textContent = "v" + ver;
    return fetch("/api/version/latest").then(r => r.json()).then(latest => {
      if (!latest.ok) {
        st.className = "ver-status err";
        st.textContent = "无法连接 GitHub 检测更新:" + (latest.error || "") + "。可手动前往仓库查看。";
        return;
      }
      const lv = latest.latest || "?";
      lat.textContent = "v" + lv;
      const curN = parseVer(ver), latN = parseVer(lv);
      if (curN != null && latN != null && curN >= latN) {
        st.className = "ver-status ok";
        st.textContent = "✅ 已是最新版本。";
      } else {
        st.className = "ver-status warn";
        st.textContent = "🔔 有新版本可用:当前 v" + ver + " → 最新 v" + lv;
        $("#ver-link").href = latest.url || ("https://github.com/" + (v.repo || "qwgaan/etf-analysis") + "/releases");
        $("#ver-link-wrap").classList.remove("hidden");
      }
    });
  }).catch(e => {
    st.className = "ver-status err";
    st.textContent = "检测失败:" + e.message;
  });
}

// ---------- 自选分组 导出 / 导入 ----------
const escAttr = (s) => String(s).replace(/"/g, "&quot;").replace(/</g, "&lt;");

$("#watch-export").addEventListener("click", openExportModal);
$("#export-all").addEventListener("click", () => $$("#export-group-list .exp-grp").forEach(c => c.checked = true));
$("#export-none").addEventListener("click", () => $$("#export-group-list .exp-grp").forEach(c => c.checked = false));
$("#export-close").addEventListener("click", () => $("#export-modal").classList.add("hidden"));
$("#export-overlay").addEventListener("click", () => $("#export-modal").classList.add("hidden"));
$("#export-cancel").addEventListener("click", () => $("#export-modal").classList.add("hidden"));
$("#export-confirm").addEventListener("click", () => {
  const sel = $$("#export-group-list .exp-grp").filter(c => c.checked).map(c => c.value);
  $("#export-modal").classList.add("hidden");
  const q = sel.length ? ("?groups=" + encodeURIComponent(sel.join(","))) : "";
  window.location = "/api/watchlist/export" + q;
  toast("开始导出自选分组", "success");
});

function openExportModal() {
  const list = $("#export-group-list");
  if (!state.watchGroups.length) { toast("暂无分组可导出", "error"); return; }
  list.innerHTML = state.watchGroups.map(g => `
    <label class="export-group-item">
      <input type="checkbox" class="exp-grp" value="${escAttr(g.name)}" checked />
      <span class="gname">${g.name}</span>
      <span class="gcount">${g.count} 只</span>
    </label>`).join("");
  $("#export-modal").classList.remove("hidden");
}

$("#watch-import").addEventListener("click", () => $("#watch-import-file").click());
$("#watch-import-file").addEventListener("change", handleImportFile);
$("#import-close").addEventListener("click", () => $("#import-modal").classList.add("hidden"));
$("#import-overlay").addEventListener("click", () => $("#import-modal").classList.add("hidden"));
$("#import-cancel").addEventListener("click", () => $("#import-modal").classList.add("hidden"));
$("#import-confirm").addEventListener("click", async () => {
  if (!_importData) return;
  const modes = {};
  $$("#import-conflict-list input[type=radio]:checked").forEach(r => {
    modes[r.name.replace(/^mode-/, "")] = r.value;
  });
  $("#import-modal").classList.add("hidden");
  try {
    const r = await fetch("/api/watchlist/import", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ groups: _importData, modes }),
    }).then(r => r.json());
    if (!r.ok) { toast("导入失败:" + (r.error || ""), "error"); return; }
    toast("导入成功", "success");
    await loadGroups();
  } catch (e) {
    toast("导入失败:" + e.message, "error");
  }
  _importData = null;
});

let _importData = null;
async function handleImportFile(e) {
  const file = e.target.files && e.target.files[0];
  e.target.value = ""; // 允许重复选同一文件
  if (!file) return;
  let parsed;
  try {
    parsed = JSON.parse(await file.text());
  } catch (err) {
    toast("文件解析失败:不是合法 JSON", "error");
    return;
  }
  let groups = Array.isArray(parsed) ? parsed : (parsed.groups || []);
  if (!Array.isArray(groups)) { toast("文件格式错误:缺少 groups 数组", "error"); return; }
  groups = groups.filter(g => g && g.name);
  if (!groups.length) { toast("文件中没有可导出的分组", "error"); return; }
  _importData = groups;
  let current = [];
  try {
    const r = await fetch("/api/watchlist").then(r => r.json());
    current = (r.groups || []).map(g => g.name);
  } catch (_) { /* 忽略,全当新增 */ }
  const conflicts = groups.filter(g => current.includes(g.name));
  const appends = groups.filter(g => !current.includes(g.name));
  renderImportModal(conflicts, appends);
}

function renderImportModal(conflicts, appends) {
  const cl = $("#import-conflict-list");
  if (conflicts.length) {
    cl.innerHTML = conflicts.map(g => `
      <div class="import-conflict-item">
        <div class="cname">「${escAttr(g.name)}」(当前已存在,${g.codes ? g.codes.length : 0} 只)</div>
        <div class="copt">
          <label><input type="radio" name="mode-${escAttr(g.name)}" value="merge" checked /> 合并(保留现有,追加导入新增)</label>
          <label><input type="radio" name="mode-${escAttr(g.name)}" value="overwrite" /> 覆盖(整组替换为导入内容)</label>
        </div>
      </div>`).join("");
  } else {
    cl.innerHTML = `<div class="import-append-info">没有重名分组,将全部追加为新分组。</div>`;
  }
  const ai = $("#import-append-info");
  ai.innerHTML = appends.length
    ? `将追加 <b>${appends.length}</b> 个新分组:` + appends.map(g => `${escAttr(g.name)}(${g.codes ? g.codes.length : 0}只)`).join("、")
    : "";
  $("#import-modal").classList.remove("hidden");
}

// ---------- 配置抽屉 ----------
$("#btn-config").addEventListener("click", () => {
  $("#config-drawer").classList.toggle("hidden");
});
$("#config-close").addEventListener("click", () => $("#config-drawer").classList.add("hidden"));

function renderConfigForm() {
  const c = state.config;
  const m = $("#cfg-mark");
  m.innerHTML = cfgField("rule1_enabled", "规则1 · 均线多头排列", "boolean", c.mark_filter.rule1_enabled) +
    cfgField("rule1_strict_alignment", "强制 MA50>MA150>MA200", "boolean", c.mark_filter.rule1_strict_alignment) +
    cfgField("rule2_enabled", "规则2 · MA200 持续上升", "boolean", c.mark_filter.rule2_enabled) +
    cfgField("rule2_lookback", "斜率回看日数 (≈ 1 月)", "number", c.mark_filter.rule2_lookback, { step: 1, min: 5, max: 60 }) +
    cfgField("rule2_min_slope", "最小斜率(> 此值)", "number", c.mark_filter.rule2_min_slope, { step: 0.001 }) +
    cfgField("rule3_enabled", "规则3 · 远离 52 周低点", "boolean", c.mark_filter.rule3_enabled) +
    cfgField("rule3_min_distance_pct", "最小距离 %", "number", c.mark_filter.rule3_min_distance_pct, { step: 1 }) +
    cfgField("rule4_enabled", "规则4 · 接近 52 周新高", "boolean", c.mark_filter.rule4_enabled) +
    cfgField("rule4_max_distance_pct", "最大回撤 %", "number", c.mark_filter.rule4_max_distance_pct, { step: 1 }) +
    cfgField("rule5_enabled", "规则5 · 远离今年低点", "boolean", c.mark_filter.rule5_enabled) +
    cfgField("rule5_min_distance_pct", "最小距离 %", "number", c.mark_filter.rule5_min_distance_pct, { step: 1 }) +
    cfgField("rule6_enabled", "规则6 · 接近今年高点", "boolean", c.mark_filter.rule6_enabled) +
    cfgField("rule6_max_distance_pct", "最大回撤 %", "number", c.mark_filter.rule6_max_distance_pct, { step: 1 });

  $("#cfg-bias").innerHTML =
    cfgField("bias20_levels", "BIAS20 减仓阈值(逗号分隔,如 10,15)", "text", (c.bias_thresholds.bias20_levels || []).join(",")) +
    cfgField("bias60_levels", "BIAS60 减仓阈值(逗号分隔,如 20)", "text", (c.bias_thresholds.bias60_levels || []).join(","));

  $("#cfg-dd").innerHTML =
    cfgField("ytd_levels", "今年回撤三档数值(逗号, 如 10,15,20)", "text", (c.drawdown_thresholds.ytd_levels || []).join(",")) +
    cfgField("ytd_level_tags", "三档标签(逗号)", "text", (c.drawdown_thresholds.ytd_level_tags || []).join(","));

  $("#cfg-display").innerHTML =
    cfgField("default_range", "默认范围(year/week52/all)", "text", c.display.default_range) +
    cfgField("kline_years", "K线回看年数", "number", c.display.kline_years, { step: 1, min: 1, max: 10 }) +
    cfgField("screen_limit", "全市场扫描上限(0=全量)", "number", c.display.screen_limit, { step: 100, min: 0 }) +
    cfgField("auto_refresh_seconds", "K线缓存寿命 (秒)", "number", c.display.auto_refresh_seconds, { step: 3600 });

  $("#cfg-wxpusher").innerHTML =
    cfgField("spt_token", "WxPusher SPT_TOKEN", "text", (c.wxpusher && c.wxpusher.spt_token) || "", { full: true });

  // alert_schedule: 未配置过(undefined)用默认 3 个;显式空数组则全部留空
  let sched;
  if (c.alert_schedule === undefined) {
    sched = ["10:00", "13:30", "16:00"];
  } else {
    sched = Array.isArray(c.alert_schedule) ? c.alert_schedule : [];
  }
  const hol = (c.alert_holidays || []).join(",");
  $("#cfg-alert-schedule").innerHTML =
    `<label class="full"><span>推送时间 1</span><input type="time" id="cfg_alert_t1" value="${sched[0] || ""}"></label>` +
    `<label class="full"><span>推送时间 2</span><input type="time" id="cfg_alert_t2" value="${sched[1] || ""}"></label>` +
    `<label class="full"><span>推送时间 3</span><input type="time" id="cfg_alert_t3" value="${sched[2] || ""}"></label>` +
    `<label class="full"><span>额外休市日(逗号分隔)</span><input type="text" id="cfg_alert_holidays" value="${hol}" placeholder="如 2026-10-01,2026-10-02"></label>` +
    `<label class="full muted"><span class="muted">说明</span><span class="muted">仅在交易日(周一~周五,排除上方休市日)的上述时间自动扫描订阅并推送。全部留空则关闭自动推送。</span></label>`;

  $("#cfg-raw").textContent = JSON.stringify(c, null, 2);
}

// ---------- Mark 模板规则面板 ----------
function syncRuleCheckboxes() {
  if (!state.config || !state.config.mark_filter) return;
  const m = state.config.mark_filter;
  $$('input[type="checkbox"][data-rule]').forEach(cb => {
    const r = Number(cb.dataset.rule);
    cb.checked = !!m[`rule${r}_enabled`];
  });
}

async function onRuleToggle(cb) {
  if (!state.config) return;
  const r = Number(cb.dataset.rule);
  const key = `rule${r}_enabled`;
  // 乐观更新
  state.config.mark_filter[key] = cb.checked;
  state.passedCodes = null; // 规则启用状态变化,缓存失效
  const enabledCount = [1,2,3,4,5,6].filter(i => state.config.mark_filter[`rule${i}_enabled`]).length;
  $("#etf-rules-counts").textContent = `${enabledCount}/6 启用`;
  try {
    await fetch("/api/config", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(state.config),
    });
  } catch (e) {
    toast("保存规则失败: " + e.message, "error");
  }
  // 选了同一只 ETF 时,实时更新右侧 Mark 规则显示
  if (state.chartData) evaluateMarkForCurrent(state.chartData);
}

// 折叠面板
$("#mtp-toggle-screen").addEventListener("click", () => {
  const rules = $("#mtp-rules-screen");
  const btn = $("#mtp-toggle-screen");
  const isHidden = rules.classList.toggle("hidden");
  btn.setAttribute("aria-expanded", String(!isHidden));
  btn.querySelector(".mtp-toggle-text").textContent = isHidden ? "查看筛选逻辑" : "收起筛选逻辑";
});

// 规则 checkbox 事件委托(全市场面板 + 列表页左侧面板共用)
document.addEventListener("change", (e) => {
  const t = e.target;
  if (t && t.matches && t.matches('input[type="checkbox"][data-rule]')) {
    onRuleToggle(t);
  }
});

function cfgField(name, label, type, value, extra = {}) {
  const id = "cfg_" + name.replace(/\./g, "_");
  let input = "";
  if (type === "boolean") {
    input = `<input type="checkbox" id="${id}" name="${name}" ${value ? "checked" : ""}>`;
  } else if (type === "number") {
    const step = extra.step ?? "any";
    input = `<input type="number" id="${id}" name="${name}" value="${value}" step="${step}"${extra.min !== undefined ? ` min="${extra.min}"` : ""}${extra.max !== undefined ? ` max="${extra.max}"` : ""}>`;
  } else {
    input = `<input type="text" id="${id}" name="${name}" value="${(value ?? "").toString()}">`;
  }
  const cls = type === "boolean" ? "full boolean" : "full";
  return `<label class="${cls}">${input}<span>${label}</span></label>`;
}

$("#cfg-save").addEventListener("click", async () => {
  const c = JSON.parse(JSON.stringify(state.config)); // deep clone
  // 收集值回填
  ["rule1_enabled", "rule1_strict_alignment", "rule2_enabled"].forEach(k => {
    c.mark_filter[k] = $("#cfg_" + k).checked;
  });
  c.mark_filter.rule2_lookback = +$("#cfg_rule2_lookback").value;
  c.mark_filter.rule2_min_slope = +$("#cfg_rule2_min_slope").value;
  c.mark_filter.rule3_enabled = $("#cfg_rule3_enabled").checked;
  c.mark_filter.rule3_min_distance_pct = +$("#cfg_rule3_min_distance_pct").value;
  c.mark_filter.rule4_enabled = $("#cfg_rule4_enabled").checked;
  c.mark_filter.rule4_max_distance_pct = +$("#cfg_rule4_max_distance_pct").value;
  c.mark_filter.rule5_enabled = $("#cfg_rule5_enabled").checked;
  c.mark_filter.rule5_min_distance_pct = +$("#cfg_rule5_min_distance_pct").value;
  c.mark_filter.rule6_enabled = $("#cfg_rule6_enabled").checked;
  c.mark_filter.rule6_max_distance_pct = +$("#cfg_rule6_max_distance_pct").value;
  c.bias_thresholds.bias20_levels = $("#cfg_bias20_levels").value.split(",").map(s => +s.trim()).filter(v => !isNaN(v));
  c.bias_thresholds.bias60_levels = $("#cfg_bias60_levels").value.split(",").map(s => +s.trim()).filter(v => !isNaN(v));
  c.drawdown_thresholds.ytd_levels = $("#cfg_ytd_levels").value.split(",").map(s => +s.trim()).filter(v => !isNaN(v));
  c.drawdown_thresholds.ytd_level_tags = $("#cfg_ytd_level_tags").value.split(",").map(s => s.trim()).filter(Boolean);
  c.display.default_range = $("#cfg_default_range").value.trim();
  c.display.kline_years = +$("#cfg_kline_years").value;
  c.display.screen_limit = +$("#cfg_screen_limit").value;
  c.display.auto_refresh_seconds = +$("#cfg_auto_refresh_seconds").value;
  if (!c.wxpusher) c.wxpusher = {};
  c.wxpusher.spt_token = $("#cfg_spt_token").value.trim();
  c.alert_schedule = ["cfg_alert_t1", "cfg_alert_t2", "cfg_alert_t3"]
    .map(id => $("#" + id).value.trim()).filter(Boolean);
  c.alert_holidays = $("#cfg_alert_holidays").value.split(",").map(s => s.trim()).filter(Boolean);

  try {
    const r = await fetch("/api/config", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(c) }).then(r => r.json());
    if (!r.ok) throw new Error(r.error || "保存失败");
    state.config = r.current;
    state.passedCodes = null; // 配置变化后全过缓存失效
    toast("已保存", "success");
    renderConfigForm();
    if (state.selectedCode) selectETF(state.selectedCode);
    if ($("#filter-fully-passed").checked) renderETFList();
  } catch (e) {
    toast(e.message, "error");
  }
});

$("#cfg-reset").addEventListener("click", async () => {
  if (!confirm("确定要恢复所有配置到默认?会清除自定义阈值。")) return;
  const r = await fetch("/api/config/reset", { method: "POST" }).then(r => r.json());
  state.config = r.current;
  state.passedCodes = null; // 重置后全过缓存失效
  toast("已重置", "success");
  renderConfigForm();
  if (state.selectedCode) selectETF(state.selectedCode);
});

// ---------- 启动 ----------
window.addEventListener("resize", () => {
  if (window._ecMain) window._ecMain.resize();
  if (window._ecBias) window._ecBias.resize();
  if (window._ecRange) window._ecRange.resize();
});

// ---------- 历史数据预热 ----------
let warmupPollTimer = null;

$("#btn-warmup").addEventListener("click", async () => {
  const btn = $("#btn-warmup");
  if (btn.disabled) return;
  btn.disabled = true;
  btn.textContent = "检查缓存中...";
  try {
    const status = await fetch("/api/warmup/status").then(r => r.json());

    // 已全部缓存:直接弹窗提示,不再启动下载
    if (status.cache_count >= status.total && status.total > 0) {
      const latest = status.cache_latest_date || "—";
      alert(`目前 ${status.cache_count} 条记录已下载完成,截止至 ${latest} 最新数据。`);
      btn.disabled = false;
      btn.textContent = "⬇ 下载历史数据";
      return;
    }

    // 已在运行中:直接显示进度并轮询
    if (status.preheat.running) {
      toast("历史数据下载已在进行中", "info");
      $("#warmup-progress").classList.remove("hidden");
      pollWarmup();
      return;
    }

    // 启动后台预热
    const cfg = state.config || {};
    const years = (cfg.display && cfg.display.kline_years) || 3;
    btn.textContent = "启动中...";
    const r = await fetch("/api/warmup/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ limit: 0, years, sleep: 0.05 }),
    }).then(r => r.json());
    if (!r.ok && r.reason) {
      toast("预热已在进行中", "info");
    } else {
      toast(`后台预热已启动,首次到 ${status.total} 只约 10-20 分钟`, "success");
    }
    $("#warmup-progress").classList.remove("hidden");
    pollWarmup();
    // 按钮状态由 pollWarmup 接管,这里不恢复
  } catch (e) {
    toast("启动预热失败: " + e.message, "error");
    btn.disabled = false;
    btn.textContent = "⬇ 下载历史数据";
  }
});

function pollWarmup() {
  if (warmupPollTimer) clearInterval(warmupPollTimer);
  const btn = $("#btn-warmup");
  const tick = async () => {
    try {
      const r = await fetch("/api/warmup/status").then(r => r.json());
      const p = r.preheat;
      const fill = $("#warmup-fill");
      const counts = $("#warmup-counts");
      const label = $("#warmup-label");

      // 运行中保持按钮为"下载中..."
      if (p.running) {
        btn.disabled = true;
        btn.textContent = "下载中...";
      }

      if (p.total > 0) {
        const pct = (p.done / p.total) * 100;
        fill.style.width = pct.toFixed(1) + "%";
        counts.textContent = `${p.done} / ${p.total} · 成功 ${p.ok} · 失败 ${p.fail} · 缓存 ${r.cache_count}/${r.total}`;
        label.textContent = p.running
          ? `下载中 · 当前 ${p.current || "..."}`
          : `已完成 · 用时 ${p.elapsed} · 新增 ${p.added} 只`;
      } else if (p.finished_at && p.added === 0) {
        // 启动时没有待拉取的(全部已缓存)
        $("#warmup-progress").classList.add("hidden");
        toast("所有 ETF 已缓存", "info");
        clearInterval(warmupPollTimer);
        warmupPollTimer = null;
        btn.disabled = false;
        btn.textContent = "⬇ 下载历史数据";
        return;
      }

      if (!p.running) {
        clearInterval(warmupPollTimer);
        warmupPollTimer = null;
        btn.disabled = false;
        btn.textContent = "⬇ 下载历史数据";
        // 3 秒后自动隐藏进度条
        setTimeout(() => $("#warmup-progress").classList.add("hidden"), 3000);
      }
    } catch (e) {
      clearInterval(warmupPollTimer);
      warmupPollTimer = null;
      btn.disabled = false;
      btn.textContent = "⬇ 下载历史数据";
    }
  };
  tick();
  warmupPollTimer = setInterval(tick, 1500);
}

// ---------- 自选 ETF 池(多组) ----------
state.watchGroups = [];
state.watchActiveGroup = "";
state.watchView = localStorage.getItem("watchView") || "card"; // card | list

async function loadGroups() {
  try {
    const r = await fetch("/api/watchlist").then(r => r.json());
    state.watchGroups = r.groups || [];
    // 没有组时,自动建一个默认组
    if (!state.watchGroups.length) {
      await fetch("/api/watchlist/group/create", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: "默认组" }),
      });
      const r2 = await fetch("/api/watchlist").then(r => r.json());
      state.watchGroups = r2.groups || [];
    }
    if (!state.watchActiveGroup || !state.watchGroups.some(g => g.name === state.watchActiveGroup)) {
      state.watchActiveGroup = state.watchGroups[0]?.name || "默认组";
    }
    renderGroupTabs();
    renderWatchlist();
  } catch (e) {
    $("#watch-status").textContent = "加载组失败: " + e.message;
  }
}

function renderGroupTabs() {
  const tabs = $("#watch-group-tabs");
  tabs.innerHTML = state.watchGroups.map(g => `
    <div class="watch-group-tab ${g.name === state.watchActiveGroup ? "active" : ""}" data-group="${g.name}">
      <span>${g.name}</span><span class="cnt">${g.count}</span>
    </div>
  `).join("");
  $$("#watch-group-tabs .watch-group-tab").forEach(t => {
    t.addEventListener("click", () => {
      state.watchActiveGroup = t.dataset.group;
      renderGroupTabs();
      renderWatchlist();
    });
  });
}

// 视图切换:卡片 / 列表
function setWatchView(view) {
  state.watchView = view;
  localStorage.setItem("watchView", view);
  $$("[data-watch-view]").forEach(b => b.classList.toggle("active", b.dataset.watchView === view));
  $("#watch-grid").classList.toggle("hidden", view !== "card");
  $("#watch-list").classList.toggle("hidden", view !== "list");
  renderWatchlist();
}

$$('[data-watch-view]').forEach(b => {
  b.addEventListener("click", () => setWatchView(b.dataset.watchView));
});

async function renderWatchlist() {
  const status = $("#watch-status");
  status.textContent = "加载中...";
  const grid = $("#watch-grid");
  const list = $("#watch-list");
  const empty = $("#watch-empty");
  const group = encodeURIComponent(state.watchActiveGroup || "默认组");
  try {
    const r = await fetch(`/api/watchlist/screen?group=${group}`).then(r => r.json());
    state.watchItems = r.items || [];
    state.enabledRules = r.enabled_rules || state.enabledRules;

    // 同步视图显示
    grid.classList.toggle("hidden", state.watchView !== "card");
    list.classList.toggle("hidden", state.watchView !== "list");

    if (!state.watchItems.length) {
      grid.innerHTML = "";
      $("#watch-list-tbody").innerHTML = "";
      empty.classList.remove("hidden");
      status.textContent = `组「${state.watchActiveGroup}」还没有自选 ETF · 输入代码添加`;
      return;
    }
    empty.classList.add("hidden");

    if (state.watchView === "list") {
      renderWatchlistAsList();
    } else {
      renderWatchlistAsCards();
    }

    const uncached = r.uncached || [];
    const cached = r.cached || 0;
    status.textContent = `组「${state.watchActiveGroup}」共 ${state.watchItems.length} 只 · 已缓存 ${cached} 只${uncached.length ? " · 未缓存：" + uncached.slice(0, 3).join(", ") + (uncached.length > 3 ? " ..." : "") : ""}`;
  } catch (e) {
    status.textContent = "加载失败: " + e.message;
  }
}

function renderWatchlistAsCards() {
  const grid = $("#watch-grid");
  grid.innerHTML = state.watchItems.map(cardHtml).join("");
  $$("#watch-grid .watch-card").forEach(card => {
    card.querySelector(".watch-card-close").addEventListener("click", async (e) => {
      e.stopPropagation();
      await removeFromWatchlist(card.dataset.code);
    });
    const yearlyBtn = card.querySelector(".watch-yearly-btn");
    if (yearlyBtn) {
      yearlyBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        showYearly(card.dataset.code);
      });
    }
    card.addEventListener("click", () => {
      switchView("etf");
      selectETF(card.dataset.code);
    });
  });
}

function renderWatchlistAsList() {
  const tbody = $("#watch-list-tbody");
  const enabledRules = state.enabledRules || [1, 2, 3, 4];
  tbody.innerHTML = state.watchItems.map(r => {
    const rs = r.rules || {};
    const ruleTags = enabledRules.map(n => {
      const k = "rule" + n;
      if (!rs[k]) return "";
      const cls = rs[k].ok ? "pass" : "fail";
      return `<span class="list-rule ${cls}" title="${rs[k].reason}">${RULE_LABELS[n] || ("规则" + n)}</span>`;
    }).join("");
    return `
      <tr class="${r.fully_passed ? 'full-pass' : ''}" data-code="${r.code}">
        <td>${r.code}</td>
        <td><div class="list-name">${r.name}</div><div class="list-cache">${r.cached ? "已缓存" : "未缓存"}</div></td>
        <td>${fmt(r.close, 3)}</td>
        <td class="${r.bias20 != null && r.bias20 > 0 ? 'up' : 'down'}">${fmtPct(r.bias20)}</td>
        <td class="${r.bias60 != null && r.bias60 > 0 ? 'up' : 'down'}">${fmtPct(r.bias60)}</td>
        <td class="${r.ytd_drawdown != null && r.ytd_drawdown < 0 ? 'down' : ''}">${fmtPct(r.ytd_drawdown)}</td>
        <td class="${r.dd52w != null && r.dd52w < 0 ? 'down' : ''}">${fmtPct(r.dd52w)}</td>
        <td><div class="list-rules">${ruleTags}</div></td>
        <td>
          <button class="btn mini list-yearly" data-code="${r.code}" title="上市以来逐年表现">📊 逐年</button>
          <button class="btn mini list-remove" data-code="${r.code}" title="移出当前组">移除</button>
        </td>
      </tr>
    `;
  }).join("");
  $$("#watch-list-tbody tr").forEach(tr => {
    tr.addEventListener("click", (e) => {
      if (e.target.closest(".list-remove") || e.target.closest(".list-yearly")) return;
      switchView("etf");
      selectETF(tr.dataset.code);
    });
  });
  $$("#watch-list-tbody .list-remove").forEach(btn => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      removeFromWatchlist(btn.dataset.code);
    });
  });
  $$("#watch-list-tbody .list-yearly").forEach(btn => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      showYearly(btn.dataset.code);
    });
  });
}

async function removeFromWatchlist(code) {
  await fetch("/api/watchlist/remove", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code, group: state.watchActiveGroup }),
  });
  toast(`已从「${state.watchActiveGroup}」移除 ${code}`, "success");
  loadGroups();
}

function cardHtml(r) {
  const b20 = r.bias20;
  const b60 = r.bias60;
  const cfg = (state.config && state.config.bias_thresholds) || {};
  const l20 = (cfg.bias20_levels || [10, 15]);
  const l60 = (cfg.bias60_levels || [20]);
  const triggered20 = l20.some(l => b20 != null && b20 >= l);
  const triggered60 = l60.some(l => b60 != null && b60 >= l);
  const rs = r.rules || {};
  const enabledRules = state.enabledRules || [1, 2, 3, 4];
  const ruleClass = (k) => rs[k] ? (rs[k].ok ? "pass" : "fail") : "";
  const ruleCells = enabledRules.map(n => {
    const k = "rule" + n;
    const tag = (RULE_LABELS[n] || ("规则" + n));
    const mark = rs[k] ? (rs[k].ok ? "✓" : "×") : "—";
    return `<div class="watch-card-rule ${ruleClass(k)}" title="${rs[k] ? rs[k].reason : ''}"><div class="rule-tag">${tag}</div>${mark}</div>`;
  }).join("");
  return `
    <div class="watch-card" data-code="${r.code}">
      <div class="watch-card-head">
        <div>
          <div class="watch-card-title">${r.name}</div>
          <div class="watch-card-code">${r.code} · ${r.cached ? "已缓存" : "未缓存"}</div>
        </div>
        <button class="watch-card-close" title="移出当前组">×</button>
      </div>
      <div class="watch-card-row"><span class="label">现价</span><span class="value">${fmt(r.close, 3)}</span></div>
      <div class="watch-card-row"><span class="label">今年回撤</span><span class="value ${r.ytd_drawdown != null && r.ytd_drawdown < 0 ? "down" : ""}">${fmtPct(r.ytd_drawdown)}</span></div>
      <div class="watch-card-row"><span class="label">52周回撤</span><span class="value ${r.dd52w != null && r.dd52w < 0 ? "down" : ""}">${fmtPct(r.dd52w)}</span></div>
      <div class="watch-card-bias">
        <div class="b ${triggered20 ? "hot" : ""}">
          <div class="label">BIAS20</div>
          <div class="bv ${b20 != null && b20 > 0 ? "up" : "down"}">${fmtPct(b20)}</div>
        </div>
        <div class="b ${triggered60 ? "hot" : ""}">
          <div class="label">BIAS60</div>
          <div class="bv ${b60 != null && b60 > 0 ? "up" : "down"}">${fmtPct(b60)}</div>
        </div>
      </div>
      <div class="watch-card-rules">${ruleCells}</div>
      <div class="watch-card-actions">
        <button class="btn mini watch-yearly-btn" data-code="${r.code}" title="上市以来逐年表现">📊 逐年</button>
      </div>
    </div>
  `;
}

async function showYearly(code) {
  const modal = $("#yearly-modal");
  const tbody = $("#yearly-tbody");
  const empty = $("#yearly-empty");
  const title = $("#yearly-title");
  tbody.innerHTML = `<tr><td colspan="3" style="text-align:center;color:#999;padding:20px">加载中...</td></tr>`;
  empty.classList.add("hidden");
  modal.classList.remove("hidden");
  try {
    const r = await fetch(`/api/yearly/${code}`).then(r => r.json());
    if (!r.ok) { toast(r.error || "加载失败", "error"); modal.classList.add("hidden"); return; }
    title.textContent = `${r.name || r.code} · 逐年表现`;
    if (!r.items || !r.items.length) {
      tbody.innerHTML = "";
      empty.classList.remove("hidden");
      return;
    }
    empty.classList.add("hidden");
    tbody.innerHTML = r.items.map(row => `
      <tr>
        <td>${row.year}</td>
        <td class="${row.return > 0 ? "up" : row.return < 0 ? "down" : ""}">${fmtPct(row.return)}</td>
        <td class="${row.max_drawdown < 0 ? "down" : ""}">${fmtPct(row.max_drawdown)}</td>
        <td>${row.max_drawdown_date || "—"}</td>
        <td>${fmt(row.max_drawdown_price, 3)}</td>
      </tr>
    `).join("");
  } catch (e) {
    toast("加载逐年表现失败: " + e.message, "error");
    modal.classList.add("hidden");
  }
}

function hideYearly() { $("#yearly-modal").classList.add("hidden"); }
$("#yearly-close").addEventListener("click", hideYearly);
$("#yearly-overlay").addEventListener("click", hideYearly);

// ---------- 自选 ETF 输入搜索下拉 ----------
let watchSuggestTimer = null;
let watchSuggestIndex = -1;
let watchSuggestItems = [];

function hideWatchSuggest() {
  const box = $("#watch-suggest");
  box.classList.add("hidden");
  watchSuggestIndex = -1;
}

function renderWatchSuggest(items) {
  const box = $("#watch-suggest");
  watchSuggestItems = items;
  watchSuggestIndex = -1;
  if (!items || !items.length) {
    box.innerHTML = `<div class="watch-suggest-empty">未找到匹配 ETF</div>`;
    box.classList.remove("hidden");
    return;
  }
  box.innerHTML = items.map((it, i) => `
    <div class="watch-suggest-item" data-index="${i}" data-code="${it.code}">
      <span class="suggest-name">${it.name}</span>
      <span class="suggest-code">${it.code}</span>
    </div>
  `).join("");
  box.classList.remove("hidden");
  $$("#watch-suggest .watch-suggest-item").forEach(el => {
    el.addEventListener("click", () => {
      selectWatchSuggestion(el.dataset.code);
    });
    el.addEventListener("mouseenter", () => {
      watchSuggestIndex = parseInt(el.dataset.index, 10);
      refreshWatchSuggestHighlight();
    });
  });
}

function refreshWatchSuggestHighlight() {
  $$("#watch-suggest .watch-suggest-item").forEach((el, i) => {
    el.classList.toggle("active", i === watchSuggestIndex);
  });
}

function selectWatchSuggestion(code) {
  $("#watch-input").value = code;
  hideWatchSuggest();
  addCurrentWatch();
}

async function doWatchSearch(q) {
  if (!q) { hideWatchSuggest(); return; }
  try {
    const r = await fetch(`/api/etfs/search?q=${encodeURIComponent(q)}&limit=15`).then(r => r.json());
    renderWatchSuggest(r.items || []);
  } catch (e) {
    hideWatchSuggest();
  }
}

async function addCurrentWatch() {
  const input = $("#watch-input").value.trim();
  if (!input) { toast("请输入代码或名称", "error"); return; }
  let code = "";
  if (/^\d{6}$/.test(input)) {
    code = input;
  } else {
    const q = input.toLowerCase();
    const found = state.etfs.find(e =>
      (e.name || "").toLowerCase().includes(q) || e.code.includes(q)
    );
    if (found) code = found.code;
  }
  if (!code) { toast("找不到匹配的 ETF", "error"); return; }
  const r = await fetch("/api/watchlist/add", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code, group: state.watchActiveGroup }),
  }).then(r => r.json());
  if (!r.ok) { toast(r.error || "添加失败", "error"); return; }
  $("#watch-input").value = "";
  hideWatchSuggest();
  toast(`已加入「${state.watchActiveGroup}」${code}`, "success");
  loadGroups();
}

$("#watch-input").addEventListener("input", () => {
  const q = $("#watch-input").value.trim();
  if (watchSuggestTimer) clearTimeout(watchSuggestTimer);
  if (!q) { hideWatchSuggest(); return; }
  watchSuggestTimer = setTimeout(() => doWatchSearch(q), 150);
});

$("#watch-input").addEventListener("keydown", (e) => {
  const box = $("#watch-suggest");
  const visible = !box.classList.contains("hidden");
  if (e.key === "Enter") {
    e.preventDefault();
    if (visible && watchSuggestIndex >= 0 && watchSuggestItems[watchSuggestIndex]) {
      selectWatchSuggestion(watchSuggestItems[watchSuggestIndex].code);
    } else {
      addCurrentWatch();
    }
    return;
  }
  if (!visible) return;
  if (e.key === "ArrowDown") {
    e.preventDefault();
    watchSuggestIndex = Math.min(watchSuggestIndex + 1, watchSuggestItems.length - 1);
    refreshWatchSuggestHighlight();
    return;
  }
  if (e.key === "ArrowUp") {
    e.preventDefault();
    watchSuggestIndex = Math.max(watchSuggestIndex - 1, 0);
    refreshWatchSuggestHighlight();
    return;
  }
  if (e.key === "Escape") {
    hideWatchSuggest();
  }
});

$("#watch-add").addEventListener("click", addCurrentWatch);

// 点击外部隐藏下拉
document.addEventListener("click", (e) => {
  if (!e.target.closest(".watch-search-wrap")) hideWatchSuggest();
});

// 新建组
$("#watch-group-add").addEventListener("click", async () => {
  const name = prompt("输入新组名称:");
  if (!name || !name.trim()) return;
  const r = await fetch("/api/watchlist/group/create", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: name.trim() }),
  }).then(r => r.json());
  if (!r.ok) { toast(r.error || "新建失败", "error"); return; }
  state.watchActiveGroup = name.trim();
  toast(`已新建组「${name.trim()}」`, "success");
  loadGroups();
});

// 重命名组
$("#watch-group-rename").addEventListener("click", async () => {
  const cur = state.watchActiveGroup;
  const newName = prompt(`重命名组「${cur}」为:`, cur);
  if (!newName || !newName.trim() || newName.trim() === cur) return;
  const r = await fetch("/api/watchlist/group/rename", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ old_name: cur, new_name: newName.trim() }),
  }).then(r => r.json());
  if (!r.ok) { toast(r.error || "重命名失败", "error"); return; }
  state.watchActiveGroup = newName.trim();
  toast("已重命名", "success");
  loadGroups();
});

// 删除组
$("#watch-group-delete").addEventListener("click", async () => {
  const cur = state.watchActiveGroup;
  if (!confirm(`确定删除组「${cur}」?组内 ${state.watchItems.length} 只 ETF 的收藏会一并移除。`)) return;
  const r = await fetch("/api/watchlist/group/delete", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: cur }),
  }).then(r => r.json());
  if (!r.ok) { toast(r.error || "删除失败", "error"); return; }
  toast("组已删除", "success");
  state.watchActiveGroup = "";
  loadGroups();
});

$("#watch-refresh").addEventListener("click", () => renderWatchlist());

// ---------- 自选 ETF 警戒订阅(逐只订阅 + 定时自动推送 + 测试) ----------
state.alertItems = [];
state.alertThresholds = {};      // 全局阈值
state.alertCodeThresholds = {};  // 每只 ETF 独立阈值 {code: {bias20_levels, ...}}

function saveAlertSub(code, alerts) {
  return fetch("/api/watchlist/alert/subscribe", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ group: state.watchActiveGroup, code, alerts }),
  }).then(r => r.json()).catch(e => ({ ok: false, error: e.message }));
}

function saveAlertThresholds(code, thresholds) {
  return fetch("/api/watchlist/alert/thresholds", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ group: state.watchActiveGroup, code, thresholds }),
  }).then(r => r.json()).catch(e => ({ ok: false, error: e.message }));
}

function renderAlertList(items, codeThresholds) {
  state.alertItems = items || [];
  if (codeThresholds) state.alertCodeThresholds = codeThresholds;
  const globalTh = state.alertThresholds || {};
  const box = $("#alert-list");
  const status = $("#alert-status");
  if (!items || !items.length) {
    box.innerHTML = `<div class="alert-empty">当前组没有自选 ETF</div>`;
    status.textContent = "";
    return;
  }
  const triggeredCount = items.filter(it => it.triggered_any).length;
  status.textContent = `共 ${items.length} 只 · 已订阅并触发 ${triggeredCount} 只`;
  box.innerHTML = items.map(it => {
    const a = it.subscribed || it.alerts || { bias20: false, bias60: false, dd: false };
    const hot = it.hot || { bias20: false, bias60: false, dd: false };
    const subAttr = (sig) => `class="sub-chk" data-sig="${sig}" ${a[sig] ? "checked" : ""}`;
    const codeTh = (state.alertCodeThresholds || {})[it.code] || {};
    const b20v = (codeTh.bias20_levels || globalTh.bias20_levels || []).join(",");
    const b60v = (codeTh.bias60_levels || globalTh.bias60_levels || []).join(",");
    const ytdv = (codeTh.ytd_levels || globalTh.ytd_levels || []).join(",");
    const hasCustom = !!(codeTh.bias20_levels || codeTh.bias60_levels || codeTh.ytd_levels);
    return `
    <div class="alert-item ${it.triggered_any ? "alert-item-hot" : ""}" data-code="${it.code}">
      <div class="alert-item-main">
        <div class="alert-item-title">${it.name} <span class="alert-code">${it.code}</span></div>
        <div class="alert-item-values">
          现价 ${fmt(it.close, 3)} · BIAS20 ${fmtPct(it.bias20)} · BIAS60 ${fmtPct(it.bias60)} · 当前价格年内回撤 ${fmtPct(it.ytd_drawdown)}
        </div>
        <div class="alert-item-signals">
          ${it.triggered.length ? it.triggered.map(s => `<span class="alert-tag">${s}</span>`).join("") : '<span class="alert-tag muted">未触发</span>'}
        </div>
        <div class="alert-subs">
          <label class="${hot.bias20 ? "hot" : ""}"><input type="checkbox" ${subAttr("bias20")}/> BIAS20</label>
          <label class="${hot.bias60 ? "hot" : ""}"><input type="checkbox" ${subAttr("bias60")}/> BIAS60</label>
          <label class="${hot.dd ? "hot" : ""}"><input type="checkbox" ${subAttr("dd")}/> 回撤档</label>
        </div>
        <div class="alert-th-line">
          <span class="alert-th-label">阈值</span>
          <label>BIAS20<input type="text" class="th-inp th-b20" value="${b20v}" placeholder="全局" title="逗号分隔,如 10,15"></label>
          <label>BIAS60<input type="text" class="th-inp th-b60" value="${b60v}" placeholder="全局" title="逗号分隔,如 20"></label>
          <label>回撤<input type="text" class="th-inp th-ytd" value="${ytdv}" placeholder="全局" title="逗号分隔,如 10,15,20"></label>
          <button class="th-reset ${hasCustom ? "" : "hidden"}" title="恢复使用全局阈值">恢复全局</button>
          <span class="th-hint">${hasCustom ? "已自定义" : "使用全局"}</span>
        </div>
      </div>
    </div>`;
  }).join("");

  // 勾选即自动保存订阅
  $$("#alert-list .sub-chk").forEach(cb => {
    cb.addEventListener("change", async () => {
      const item = cb.closest(".alert-item");
      const code = item.dataset.code;
      const cur = { bias20: false, bias60: false, dd: false };
      item.querySelectorAll(".sub-chk").forEach(x => { cur[x.dataset.sig] = x.checked; });
      const r = await saveAlertSub(code, cur);
      if (!r.ok) toast("保存订阅失败: " + (r.error || ""), "error");
    });
  });

  // 阈值输入自动保存(失去焦点或回车)
  const saveThForItem = async (item) => {
    const code = item.dataset.code;
    const b20 = item.querySelector(".th-b20").value.trim();
    const b60 = item.querySelector(".th-b60").value.trim();
    const ytd = item.querySelector(".th-ytd").value.trim();
    const thresholds = {};
    if (b20) thresholds.bias20_levels = b20.split(",").map(s => +s.trim()).filter(v => !isNaN(v));
    if (b60) thresholds.bias60_levels = b60.split(",").map(s => +s.trim()).filter(v => !isNaN(v));
    if (ytd) thresholds.ytd_levels = ytd.split(",").map(s => +s.trim()).filter(v => !isNaN(v));
    const r = await saveAlertThresholds(code, thresholds);
    if (!r.ok) {
      toast("保存阈值失败: " + (r.error || ""), "error");
      return;
    }
    // 更新本地状态并刷新提示
    state.alertCodeThresholds[code] = thresholds;
    const hasCustom = !!(thresholds.bias20_levels || thresholds.bias60_levels || thresholds.ytd_levels);
    item.querySelector(".th-reset").classList.toggle("hidden", !hasCustom);
    item.querySelector(".th-hint").textContent = hasCustom ? "已自定义" : "使用全局";
  };

  $$("#alert-list .th-inp").forEach(inp => {
    inp.addEventListener("change", () => saveThForItem(inp.closest(".alert-item")));
    inp.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); inp.blur(); } });
  });

  $$("#alert-list .th-reset").forEach(btn => {
    btn.addEventListener("click", async () => {
      const item = btn.closest(".alert-item");
      const code = item.dataset.code;
      item.querySelector(".th-b20").value = "";
      item.querySelector(".th-b60").value = "";
      item.querySelector(".th-ytd").value = "";
      const r = await saveAlertThresholds(code, {});
      if (!r.ok) { toast("恢复全局失败: " + (r.error || ""), "error"); return; }
      delete state.alertCodeThresholds[code];
      btn.classList.add("hidden");
      item.querySelector(".th-hint").textContent = "使用全局";
      toast("已恢复全局阈值", "success");
    });
  });
}

async function loadAlertPreview() {
  const status = $("#alert-status");
  status.textContent = "计算中...";
  try {
    const r = await fetch("/api/watchlist/alert/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ group: state.watchActiveGroup }),
    }).then(r => r.json());
    if (!r.ok) throw new Error(r.error || "预览失败");
    state.alertThresholds = r.thresholds || {};
    renderAlertList(r.items, r.code_thresholds || {});
  } catch (e) {
    status.textContent = "预览失败: " + e.message;
    toast("预览失败: " + e.message, "error");
  }
}

function openAlertModal() {
  const cfg = state.config || {};
  const bcfg = cfg.bias_thresholds || {};
  const dcfg = cfg.drawdown_thresholds || {};
  const b20 = (bcfg.bias20_levels || [10, 15]).join("/");
  const b60 = (bcfg.bias60_levels || [20]).join("/");
  const ytd = (dcfg.ytd_levels || [10, 15, 20]).join("/");
  $("#alert-levels-info").textContent = `BIAS20 ${b20}% · BIAS60 ${b60}% · 当前价格年内回撤 ${ytd}%`;
  $("#alert-result").classList.add("hidden");
  $("#alert-result").textContent = "";
  $("#alert-subscribe-all").checked = false;
  $("#alert-modal").classList.remove("hidden");
  loadAlertPreview();
}

function hideAlertModal() {
  $("#alert-modal").classList.add("hidden");
}

$("#watch-alert").addEventListener("click", openAlertModal);
$("#alert-close").addEventListener("click", hideAlertModal);
$("#alert-overlay").addEventListener("click", hideAlertModal);

// 本组全订阅 / 取消三档
$("#alert-subscribe-all").addEventListener("change", async (e) => {
  const checked = e.target.checked;
  const alerts = { bias20: checked, bias60: checked, dd: checked };
  for (const it of state.alertItems) {
    const item = $(`#alert-list .alert-item[data-code="${it.code}"]`);
    if (item) item.querySelectorAll(".sub-chk").forEach(x => { x.checked = checked; });
    await saveAlertSub(it.code, alerts);
  }
  toast(checked ? "已订阅本组全部三档" : "已清空本组订阅", "success");
});

// 仅订阅当前已触发的档
$("#alert-select-triggered").addEventListener("click", async () => {
  for (const it of state.alertItems) {
    const hot = it.hot || {};
    const alerts = { bias20: !!hot.bias20, bias60: !!hot.bias60, dd: !!hot.dd };
    const item = $(`#alert-list .alert-item[data-code="${it.code}"]`);
    if (item) item.querySelectorAll(".sub-chk").forEach(x => { x.checked = !!hot[x.dataset.sig]; });
    await saveAlertSub(it.code, alerts);
  }
  toast("已按当前触发项更新订阅", "success");
});

$("#alert-preview").addEventListener("click", loadAlertPreview);

// 精简测试推送:立即按本组已勾选条件推送(忽略当天去重)
$("#alert-push").addEventListener("click", async () => {
  const btn = $("#alert-push");
  btn.disabled = true;
  btn.textContent = "测试推送中...";
  try {
    const r = await fetch("/api/watchlist/alert/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ group: state.watchActiveGroup }),
    }).then(r => r.json());
    const resultBox = $("#alert-result");
    resultBox.classList.remove("hidden");
    if (!r.ok) {
      resultBox.textContent = `测试失败: ${r.error || "未知错误"}`;
      toast("测试失败: " + (r.error || "未知错误"), "error");
    } else if (!r.sent) {
      resultBox.textContent = r.message || "本组没有勾选任何警戒条件 / 当前无触发";
      toast(resultBox.textContent, "info");
    } else {
      resultBox.textContent = `已测试推送 ${r.triggered_count} 只触发 ETF。\n\nWxPusher 返回:\n${JSON.stringify(r.wxpusher, null, 2)}`;
      toast(`测试推送成功 · ${r.triggered_count} 只触发`, "success");
    }
  } catch (e) {
    toast("测试失败: " + e.message, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = "🧪 立即测试推送";
  }
});

bootstrap().then(() => {
  // 初始化自选视图按钮状态
  setWatchView(state.watchView);

  // URL 参数支持:
  //   ?code=510300       自动选中某只 ETF
  //   ?view=screen/watch 直接打开对应视图
  //   ?auto=1            配合 view=screen,自动运行一次筛选
  const params = new URLSearchParams(location.search);
  const code = params.get("code");
  const view = params.get("view");
  if (view === "screen") {
    switchView("screen");
    if (params.get("auto") === "1") $("#btn-screen").click();
  } else if (view === "watch") {
    switchView("watch");
  } else if (code && state.etfIndex.has(code)) {
    selectETF(code);
  }
});
