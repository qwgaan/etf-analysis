/* ETF 分析工作台 - 通用工具函数
   ----------------------------------------- */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function toast(msg, type = "info") {
  const el = $("#toast");
  if (!el) return;
  el.textContent = msg;
  el.classList.remove("hidden");
  if (type === "error") el.style.background = "#dc2626";
  else if (type === "success") el.style.background = "#16a34a";
  else el.style.background = "rgba(20,30,50,0.92)";
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add("hidden"), 2200);
}

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

const escAttr = (s) => String(s).replace(/"/g, "&quot;").replace(/</g, "&lt;");
