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

function renderETFList() {
  $("#etf-count").textContent = state.etfs.length;
  const ul = $("#etf-list");
  const q = $("#etf-search").value.trim().toLowerCase();
  const onlyPassed = $("#filter-fully-passed").checked;
  // 仅显示"全过"的过滤放在联动层做,不在这里
  const items = state.etfs.filter(e =>
    !q || e.code.includes(q) || (e.name || "").toLowerCase().includes(q)
  );
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
  const dd = s.ytd_drawdown;
  const ddEl = $("#sum-ytd-dd");
  ddEl.textContent = fmtPct(dd);
  ddEl.className = dd > 0 ? "" : (dd < 0 ? "down" : "");
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

  // 年度回撤
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

  function setRule(key, ok, why) {
    const li = $(`#mark-rules li[data-key="${key}"]`);
    li.classList.toggle("pass", ok);
    li.querySelector(".status").textContent = ok ? "✓ 通过" : "× 未过";
    li.querySelector(".status").className = "status " + (ok ? "pass" : "fail");
    li.querySelector(".reason").textContent = why;
  }
  setRule("rule1", r1ok, r1why);
  setRule("rule2", r2ok, r2why);
  setRule("rule3", r3ok, r3why);
  setRule("rule4", r4ok, r4why);
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
  const params = new URLSearchParams({ limit: "500", use_cache: "1" });
  if ($("#filter-fully-passed-screen").checked) params.set("only_pass", "1");
  try {
    const r = await fetch(`/api/screen?${params}`).then(r => r.json());
    state.screenData = r.items || [];
    renderScreenTable();
    const scanned = r.scanned ?? state.screenData.length;
    status.textContent = `完成 · 扫描 ${scanned} 只 / 共 ${r.total ?? "?"} 只 · 命中 ${r.count}`;
    // Mark 模板面板的"符合条件"计数
    $("#mtp-matched-screen").textContent = r.matched ?? "—";
    $("#mtp-scanned-screen").textContent = scanned;
    toast(`筛选完成 · 命中 ${r.count} 只`, "success");
  } catch (e) {
    status.textContent = "筛选失败";
    toast("筛选失败: " + e.message, "error");
  }
});

function renderScreenTable() {
  const tbody = $("#screen-tbody");
  if (!state.screenData.length) {
    tbody.innerHTML = `<tr><td colspan="10" style="text-align:center;color:#999;padding:20px">
      暂无数据 —— 点击上方「运行筛选」,或先用 \`python warmup.py\` 预热全市场 K 线缓存</td></tr>`;
    return;
  }
  const sort = state.screenSort;
  const data = [...state.screenData];
  data.sort((a, b) => {
    const va = a[sort.key] ?? -1e9, vb = b[sort.key] ?? -1e9;
    return sort.dir * (va > vb ? 1 : va < vb ? -1 : 0);
  });
  tbody.innerHTML = data.map(r => `
    <tr class="${r.fully_passed ? "full-pass" : ""}" data-code="${r.code}" style="cursor:pointer">
      <td>${r.code}</td>
      <td>${r.name}</td>
      <td>${fmt(r.close, 3)}</td>
      <td class="${r.bias20 > 0 ? "up" : "down"}">${fmtPct(r.bias20)}</td>
      <td class="${r.bias60 > 0 ? "up" : "down"}">${fmtPct(r.bias60)}</td>
      <td class="${r.ytd_drawdown < 0 ? "down" : ""}">${fmtPct(r.ytd_drawdown)}</td>
      <td>${dot(r.rules.rule1.ok)}</td>
      <td>${dot(r.rules.rule2.ok)}</td>
      <td>${dot(r.rules.rule3.ok)}</td>
      <td>${dot(r.rules.rule4.ok)}</td>
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

$$("#screen-table thead th").forEach(th => {
  th.addEventListener("click", () => {
    const k = th.dataset.sort;
    if (!k) return;
    if (state.screenSort.key === k) state.screenSort.dir *= -1;
    else { state.screenSort.key = k; state.screenSort.dir = -1; }
    renderScreenTable();
  });
});

// ---------- 导出 ----------
$("#btn-export-csv").addEventListener("click", () => window.location = "/api/export/csv");
$("#btn-export-json").addEventListener("click", () => window.location = "/api/export/json");

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
    cfgField("rule4_max_distance_pct", "最大回撤 %", "number", c.mark_filter.rule4_max_distance_pct, { step: 1 });

  $("#cfg-bias").innerHTML =
    cfgField("bias20_levels", "BIAS20 减仓阈值(逗号分隔,如 10,15)", "text", (c.bias_thresholds.bias20_levels || []).join(",")) +
    cfgField("bias60_levels", "BIAS60 减仓阈值(逗号分隔,如 20)", "text", (c.bias_thresholds.bias60_levels || []).join(","));

  $("#cfg-dd").innerHTML =
    cfgField("ytd_levels", "年内回撤三档数值(逗号, 如 10,15,20)", "text", (c.drawdown_thresholds.ytd_levels || []).join(",")) +
    cfgField("ytd_level_tags", "三档标签(逗号)", "text", (c.drawdown_thresholds.ytd_level_tags || []).join(","));

  $("#cfg-display").innerHTML =
    cfgField("default_range", "默认范围(year/week52/all)", "text", c.display.default_range) +
    cfgField("kline_years", "K线回看年数", "number", c.display.kline_years, { step: 1, min: 1, max: 10 }) +
    cfgField("auto_refresh_seconds", "K线缓存寿命 (秒)", "number", c.display.auto_refresh_seconds, { step: 3600 });

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
  const enabledCount = [1,2,3,4].filter(i => state.config.mark_filter[`rule${i}_enabled`]).length;
  $("#etf-rules-counts").textContent = `${enabledCount}/4 启用`;
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
  c.bias_thresholds.bias20_levels = $("#cfg_bias20_levels").value.split(",").map(s => +s.trim()).filter(v => !isNaN(v));
  c.bias_thresholds.bias60_levels = $("#cfg_bias60_levels").value.split(",").map(s => +s.trim()).filter(v => !isNaN(v));
  c.drawdown_thresholds.ytd_levels = $("#cfg_ytd_levels").value.split(",").map(s => +s.trim()).filter(v => !isNaN(v));
  c.drawdown_thresholds.ytd_level_tags = $("#cfg_ytd_level_tags").value.split(",").map(s => s.trim()).filter(Boolean);
  c.display.default_range = $("#cfg_default_range").value.trim();
  c.display.kline_years = +$("#cfg_kline_years").value;
  c.display.auto_refresh_seconds = +$("#cfg_auto_refresh_seconds").value;

  try {
    const r = await fetch("/api/config", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(c) }).then(r => r.json());
    if (!r.ok) throw new Error(r.error || "保存失败");
    state.config = r.current;
    toast("已保存", "success");
    renderConfigForm();
    if (state.selectedCode) selectETF(state.selectedCode);
  } catch (e) {
    toast(e.message, "error");
  }
});

$("#cfg-reset").addEventListener("click", async () => {
  if (!confirm("确定要恢复所有配置到默认?会清除自定义阈值。")) return;
  const r = await fetch("/api/config/reset", { method: "POST" }).then(r => r.json());
  state.config = r.current;
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
  const cfg = state.config || {};
  const years = (cfg.display && cfg.display.kline_years) || 3;
  btn.disabled = true;
  btn.textContent = "启动中...";
  try {
    const r = await fetch("/api/warmup/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ limit: 0, years, sleep: 0.05 }),
    }).then(r => r.json());
    if (!r.ok && r.reason) {
      toast("预热已在进行中", "info");
    } else {
      toast("后台预热已启动,首次到 1576 只约 10-20 分钟", "success");
    }
    $("#warmup-progress").classList.remove("hidden");
    pollWarmup();
  } catch (e) {
    toast("启动预热失败: " + e.message, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = "⬇ 下载历史数据";
  }
});

function pollWarmup() {
  if (warmupPollTimer) clearInterval(warmupPollTimer);
  const tick = async () => {
    try {
      const r = await fetch("/api/warmup/status").then(r => r.json());
      const p = r.preheat;
      const fill = $("#warmup-fill");
      const counts = $("#warmup-counts");
      const label = $("#warmup-label");
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
        return;
      }
      if (!p.running) {
        clearInterval(warmupPollTimer);
        warmupPollTimer = null;
        // 3 秒后自动隐藏进度条
        setTimeout(() => $("#warmup-progress").classList.add("hidden"), 3000);
      }
    } catch (e) {
      clearInterval(warmupPollTimer);
    }
  };
  tick();
  warmupPollTimer = setInterval(tick, 1500);
}

// ---------- 自选 ETF 池(多组) ----------
state.watchGroups = [];
state.watchActiveGroup = "";

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

async function renderWatchlist() {
  const status = $("#watch-status");
  status.textContent = "加载中...";
  const grid = $("#watch-grid");
  const empty = $("#watch-empty");
  const group = encodeURIComponent(state.watchActiveGroup || "默认组");
  try {
    const r = await fetch(`/api/watchlist/screen?group=${group}`).then(r => r.json());
    state.watchItems = r.items || [];
    if (!state.watchItems.length) {
      grid.innerHTML = "";
      empty.classList.remove("hidden");
      status.textContent = `组「${state.watchActiveGroup}」还没有自选 ETF · 输入代码添加`;
      return;
    }
    empty.classList.add("hidden");
    grid.innerHTML = state.watchItems.map(cardHtml).join("");
    // 绑定卡片事件
    $$("#watch-grid .watch-card").forEach(card => {
      card.querySelector(".watch-card-close").addEventListener("click", async (e) => {
        e.stopPropagation();
        const code = card.dataset.code;
        await fetch("/api/watchlist/remove", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ code, group: state.watchActiveGroup }),
        });
        toast(`已从「${state.watchActiveGroup}」移除 ${code}`, "success");
        loadGroups();
      });
      card.addEventListener("click", () => {
        switchView("etf");
        selectETF(card.dataset.code);
      });
    });
    const uncached = r.uncached || [];
    const cached = r.cached || 0;
    status.textContent = `组「${state.watchActiveGroup}」共 ${state.watchItems.length} 只 · 已缓存 ${cached} 只${uncached.length ? " · 未缓存：" + uncached.slice(0, 3).join(", ") + (uncached.length > 3 ? " ..." : "") : ""}`;
  } catch (e) {
    status.textContent = "加载失败: " + e.message;
  }
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
  const ruleTags = {
    rule1: "均线", rule2: "MA200↑", rule3: "远离低", rule4: "接近高",
  };
  const ruleClass = (k) => rs[k] ? (rs[k].ok ? "pass" : "fail") : "";
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
      <div class="watch-card-row"><span class="label">年度最大回撤</span><span class="value ${r.ytd_drawdown != null && r.ytd_drawdown < 0 ? "down" : ""}">${fmtPct(r.ytd_drawdown)}</span></div>
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
      <div class="watch-card-rules">
        <div class="watch-card-rule ${ruleClass('rule1')}"><div class="rule-tag">${ruleTags.rule1}</div>${rs.rule1 ? (rs.rule1.ok ? "✓" : "×") : "—"}</div>
        <div class="watch-card-rule ${ruleClass('rule2')}"><div class="rule-tag">${ruleTags.rule2}</div>${rs.rule2 ? (rs.rule2.ok ? "✓" : "×") : "—"}</div>
        <div class="watch-card-rule ${ruleClass('rule3')}"><div class="rule-tag">${ruleTags.rule3}</div>${rs.rule3 ? (rs.rule3.ok ? "✓" : "×") : "—"}</div>
        <div class="watch-card-rule ${ruleClass('rule4')}"><div class="rule-tag">${ruleTags.rule4}</div>${rs.rule4 ? (rs.rule4.ok ? "✓" : "×") : "—"}</div>
      </div>
    </div>
  `;
}

$("#watch-add").addEventListener("click", async () => {
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
  toast(`已加入「${state.watchActiveGroup}」${code}`, "success");
  loadGroups();
});

$("#watch-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") $("#watch-add").click();
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

bootstrap().then(() => {
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
