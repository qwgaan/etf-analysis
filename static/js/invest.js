/* 投资分析模块前端逻辑（独立模块，不依赖 ETF 的全局状态）。
   仅在用户切到「投资分析」tab 时由 app.js 调用 investInit() 初始化一次。 */
(function () {
  "use strict";

  let _inited = false;
  let _currentJob = null;     // 正在轮询的任务
  let _pollTimer = null;
  let _allReports = [];       // 全部报告缓存（弹窗用）
  let _reportSort = { col: "file", asc: true }; // 弹窗排序状态

  function el(id) { return document.getElementById(id); }

  function investInit() {
    if (_inited) return;
    _inited = true;
    bindEvents();
    loadConfig();
    loadReports();
  }

  // ----------------------------------------------------------
  // 配置加载与渲染
  // ----------------------------------------------------------
  async function loadConfig() {
    try {
      const r = await fetch("/api/invest/config").then(x => x.json());
      if (!r.ok) throw new Error(r.error || "加载失败");
      const llm = r.llm || {};
      const pf = r.profile || {};
      el("invest-status").textContent = "就绪";

      // LLM
      el("inv-llm-base").value = llm.base_url || "";
      el("inv-llm-model").value = llm.model || "";
      el("inv-llm-timeout").value = llm.timeout != null ? llm.timeout : 120;
      el("inv-llm-temp").value = llm.temperature != null ? llm.temperature : 0.4;
      el("inv-llm-info").textContent = llm.configured
        ? `已配置（key: ${llm.api_key_masked}）`
        : (llm.env_present ? "使用环境变量 LLM_API_KEY" : "未配置，将走规则引擎");

      // 画像
      el("inv-pf-name").value = pf.name || "";
      el("inv-pf-risk").value = pf.risk || "平衡";
      el("inv-pf-horizon").value = pf.horizon || "中线";
      el("inv-pf-pos").value = Math.round((pf.max_position != null ? pf.max_position : 0.15) * 100);
      el("inv-pf-div").checked = !!pf.dividend_focus;

      // 自定义权重回填
      const defW = pf.weights || {"技术面": 0.25, "基本面": 0.25, "估值": 0.25, "资金面": 0.25};
      el("inv-w-tech").value = Math.round((defW["技术面"] || 0.25) * 100);
      el("inv-w-fund").value = Math.round((defW["基本面"] || 0.25) * 100);
      el("inv-w-val").value = Math.round((defW["估值"] || 0.25) * 100);
      el("inv-w-flow").value = Math.round((defW["资金面"] || 0.25) * 100);
      toggleCustomWeights();

      renderWeights(r.weights);
    } catch (e) {
      el("invest-status").textContent = "加载失败：" + e.message;
    }
  }

  function renderWeights(w) {
    const box = el("inv-weights");
    if (!w) { box.innerHTML = ""; return; }
    const dims = w.weights || {};
    let html = "<div class='wp-title'>四维权重（自动归一化）</div><div class='wp-bars'>";
    for (const [k, v] of Object.entries(dims)) {
      const pct = Math.round(v * 100);
      html += `<div class='wp-row'><span class='wp-name'>${k}</span>`
            + `<span class='wp-bar'><span class='wp-fill' style='width:${pct}%'></span></span>`
            + `<span class='wp-val'>${pct}%</span></div>`;
    }
    html += "</div>";
    if (w.mapping && w.mapping.length) {
      html += "<div class='wp-title'>综合分 → 建议 / 仓位上限</div><table class='wp-table'>";
      for (const m of w.mapping) {
        html += `<tr><td>${m.score}</td><td>${m.stance}</td><td>${(m.position * 100).toFixed(1)}%</td></tr>`;
      }
      html += "</table>";
    }
    box.innerHTML = html;
  }

  // ----------------------------------------------------------
  // 事件绑定
  // ----------------------------------------------------------
  function bindEvents() {
    el("inv-llm-save").addEventListener("click", saveLLM);
    el("inv-llm-test").addEventListener("click", testLLM);
    el("inv-pf-save").addEventListener("click", saveProfile);
    el("inv-pf-preview").addEventListener("click", previewProfile);
    el("inv-pf-risk").addEventListener("change", toggleCustomWeights);
    el("inv-single-run").addEventListener("click", runSingle);
    el("inv-batch-run").addEventListener("click", runBatch);
    el("inv-all-reports").addEventListener("click", openReportManager);
    el("inv-report-modal-close").addEventListener("click", closeReportManager);
    el("inv-report-modal-overlay").addEventListener("click", closeReportManager);
    el("inv-report-filter").addEventListener("input", onReportFilter);
    el("inv-report-table").querySelectorAll("thead th.sortable").forEach(th => {
      th.addEventListener("click", onReportSort);
    });
  }

  function toggleCustomWeights() {
    const isCustom = el("inv-pf-risk").value === "自定义";
    el("inv-custom-weights").classList.toggle("hidden", !isCustom);
  }

  function gatherWeights() {
    return {
      "技术面": Number(el("inv-w-tech").value) / 100,
      "基本面": Number(el("inv-w-fund").value) / 100,
      "估值": Number(el("inv-w-val").value) / 100,
      "资金面": Number(el("inv-w-flow").value) / 100,
    };
  }

  async function saveLLM() {
    const data = {
      base_url: el("inv-llm-base").value.trim(),
      model: el("inv-llm-model").value.trim(),
      api_key: el("inv-llm-key").value.trim(),
      timeout: el("inv-llm-timeout").value,
      temperature: el("inv-llm-temp").value,
    };
    el("inv-llm-info").textContent = "保存中…";
    try {
      const r = await fetch("/api/invest/llm-config", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data),
      }).then(x => x.json());
      if (!r.ok) throw new Error(r.error || "保存失败");
      el("inv-llm-info").textContent = r.configured ? `已保存（key: ${r.api_key_masked}）` : "已保存（未配置 key，将走规则引擎）";
      el("inv-llm-key").value = "";
    } catch (e) {
      el("inv-llm-info").textContent = "保存失败：" + e.message;
    }
  }

  async function testLLM() {
    el("inv-llm-info").textContent = "连接测试中…";
    try {
      const r = await fetch("/api/invest/llm-test", { method: "POST" }).then(x => x.json());
      if (!r.ok) throw new Error(r.error || "测试失败");
      el("inv-llm-info").textContent = "✓ 连接成功：" + r.reply;
    } catch (e) {
      el("inv-llm-info").textContent = "✗ 连接失败：" + e.message;
    }
  }

  async function saveProfile() {
    const data = {
      name: el("inv-pf-name").value.trim(),
      risk: el("inv-pf-risk").value,
      horizon: el("inv-pf-horizon").value,
      max_position: el("inv-pf-pos").value,
      dividend_focus: el("inv-pf-div").checked,
    };
    if (data.risk === "自定义") {
      data.weights = gatherWeights();
    }
    try {
      const r = await fetch("/api/invest/profile", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data),
      }).then(x => x.json());
      if (!r.ok) throw new Error(r.error || "保存失败");
      renderWeights(r.weights);
      toast("画像已保存", "ok");
    } catch (e) {
      toast("画像保存失败：" + e.message, "error");
    }
  }

  async function previewProfile() {
    const data = {
      risk: el("inv-pf-risk").value,
      horizon: el("inv-pf-horizon").value,
      max_position: el("inv-pf-pos").value,
      dividend_focus: el("inv-pf-div").checked,
    };
    try {
      const r = await fetch("/api/invest/profile-preview", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data),
      }).then(x => x.json());
      if (!r.ok) throw new Error(r.error || "预览失败");
      renderWeights(r.weights);
    } catch (e) {
      toast("预览失败：" + e.message, "error");
    }
  }

  // ----------------------------------------------------------
  // 运行选项收集
  // ----------------------------------------------------------
  function gatherOptions() {
    return {
      use_llm: el("inv-opt-llm").checked,
      use_web: el("inv-opt-web").checked,
      use_search: el("inv-opt-search").checked,
      fresh: el("inv-opt-fresh").checked,
      start: el("inv-opt-start").value.trim() || "20250901",
      div: el("inv-opt-div").value.trim(),
      suffix: el("inv-opt-suffix").value.trim(),
    };
  }

  // ----------------------------------------------------------
  // 单只 / 批量运行
  // ----------------------------------------------------------
  async function runSingle() {
    const code = el("inv-single-code").value.trim();
    if (!code) { toast("请填写股票代码", "error"); return; }
    const name = el("inv-single-name").value.trim();
    try {
      const r = await fetch("/api/invest/run", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code, name, options: gatherOptions() }),
      }).then(x => x.json());
      if (!r.ok) throw new Error(r.error || "启动失败");
      startPolling(r.job_id);
    } catch (e) {
      toast("启动失败：" + e.message, "error");
    }
  }

  async function runBatch() {
    const text = el("inv-batch-text").value;
    if (!text.trim()) { toast("请填写批量清单", "error"); return; }
    try {
      const r = await fetch("/api/invest/batch", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, options: gatherOptions() }),
      }).then(x => x.json());
      if (!r.ok) throw new Error(r.error || "启动失败");
      startPolling(r.job_id, r.count);
    } catch (e) {
      toast("启动失败：" + e.message, "error");
    }
  }

  // ----------------------------------------------------------
  // 任务轮询
  // ----------------------------------------------------------
  function startPolling(jid, total) {
    if (_pollTimer) clearInterval(_pollTimer);
    _currentJob = jid;
    el("inv-job").classList.remove("hidden");
    el("inv-job-empty").classList.add("hidden");
    el("inv-job-stage").textContent = "已提交，等待执行…";
    el("inv-job-pct").textContent = "0%";
    el("inv-job-fill").style.width = "0%";
    el("inv-job-log").textContent = "";

    const tick = async () => {
      try {
        const r = await fetch("/api/invest/jobs/" + jid).then(x => x.json());
        if (!r.ok) { el("inv-job-stage").textContent = "任务不存在或已过期"; return; }
        el("inv-job-stage").textContent = (r.stage || "执行中") + (r.kind === "batch" ? `（${r.done || 0}/${r.total || "?"}）` : "");
        el("inv-job-pct").textContent = (r.progress || 0) + "%";
        el("inv-job-fill").style.width = (r.progress || 0) + "%";
        el("inv-job-log").textContent = (r.log || []).join("\n");
        el("inv-job-log").scrollTop = el("inv-job-log").scrollHeight;
        if (r.status === "done" || r.status === "error") {
          clearInterval(_pollTimer); _pollTimer = null;
          if (r.status === "error") {
            el("inv-job-stage").textContent = "任务失败：" + (r.error || "");
            toast("任务失败", "error");
          } else {
            el("inv-job-stage").textContent = "✓ 完成";
            toast("分析完成", "ok");
            onJobDone(r);
          }
        }
      } catch (e) {
        el("inv-job-stage").textContent = "轮询异常：" + e.message;
      }
    };
    tick();
    _pollTimer = setInterval(tick, 1500);
  }

  function onJobDone(job) {
    loadReports();
    // 若是单只，直接给出查看链接
    if (job.kind === "single" && job.result && job.result.html_rel) {
      const fn = job.result.html_rel.split('/').pop();
      const a = document.createElement("a");
      a.href = "/api/invest/report/" + encodeURIComponent(fn);
      a.target = "_blank"; a.rel = "noopener";
      a.className = "btn"; a.textContent = "查看报告：" + job.result.code + " " + (job.result.name || "");
      const box = el("inv-job-log");
      box.appendChild(document.createTextNode("\n"));
      box.appendChild(a);
    }
    if (job.kind === "batch" && job.result && job.result.summary) {
      renderBatchSummary(job.result.summary);
    }
  }

  function renderBatchSummary(sum) {
    const box = el("inv-job-log");
    box.appendChild(document.createTextNode("\n\n=== 批量汇总 ===\n"));
    box.appendChild(document.createTextNode(`成功 ${sum.ok} / 共 ${sum.total}（来源：${sum.use_llm ? "LLM" : "规则引擎"}）\n`));
    (sum.rows || []).forEach((r, i) => {
      if (r.error) {
        box.appendChild(document.createTextNode(`${i + 1}. ${r.code} ${r.name || ""} ✗ ${r.error}\n`));
      } else {
        const fn = r.html_rel.split('/').pop();
        const link = document.createElement("a");
        link.href = "/api/invest/report/" + encodeURIComponent(fn);
        link.target = "_blank"; link.rel = "noopener";
        link.textContent = `${i + 1}. ${r.code} ${r.name} · 综合分 ${r.adj_score} · ${r.stance} · 仓位 ${(r.position * 100).toFixed(0)}%`;
        box.appendChild(link);
        box.appendChild(document.createTextNode("\n"));
      }
    });
    box.scrollTop = box.scrollHeight;
  }

  // ----------------------------------------------------------
  // 报告列表
  // ----------------------------------------------------------
  async function loadReports() {
    try {
      const r = await fetch("/api/invest/reports?limit=10").then(x => x.json());
      if (!r.ok) return;
      const box = el("inv-reports");
      if (!r.reports || !r.reports.length) {
        box.innerHTML = "<div class='hint'>暂无报告，运行分析后会出现在这里</div>";
        return;
      }
      box.innerHTML = "";
      r.reports.forEach(rep => {
        const a = document.createElement("a");
        a.href = "/api/invest/report/" + encodeURIComponent(rep.file);
        a.target = "_blank"; a.rel = "noopener";
        a.className = "report-item";
        a.textContent = rep.file + " · " + (rep.size / 1024).toFixed(0) + " KB";
        box.appendChild(a);
      });
    } catch (e) { /* 忽略 */ }
  }

  // ----------------------------------------------------------
  // 全部报告弹窗（类 Windows 资源管理器：排序 + 过滤）
  // ----------------------------------------------------------
  async function openReportManager() {
    const modal = el("inv-report-modal");
    modal.classList.remove("hidden");
    el("inv-report-filter").value = "";
    _reportSort = { col: "file", asc: true };
    updateSortHeaders();
    try {
      const r = await fetch("/api/invest/reports?limit=all").then(x => x.json());
      if (!r.ok) throw new Error(r.error || "加载失败");
      _allReports = r.reports || [];
      renderReportTable();
    } catch (e) {
      el("inv-report-count").textContent = "加载失败：" + e.message;
    }
  }

  function closeReportManager() {
    el("inv-report-modal").classList.add("hidden");
  }

  function onReportFilter() {
    renderReportTable();
  }

  function onReportSort(ev) {
    const th = ev.currentTarget;
    const col = th.dataset.sort;
    if (!col) return;
    if (_reportSort.col === col) {
      _reportSort.asc = !_reportSort.asc;
    } else {
      _reportSort = { col, asc: true };
    }
    updateSortHeaders();
    renderReportTable();
  }

  function updateSortHeaders() {
    el("inv-report-table").querySelectorAll("thead th.sortable").forEach(th => {
      const col = th.dataset.sort;
      th.classList.remove("active", "asc", "desc");
      if (col === _reportSort.col) {
        th.classList.add("active", _reportSort.asc ? "asc" : "desc");
      }
    });
  }

  function formatBytes(bytes) {
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
    return (bytes / (1024 * 1024)).toFixed(2) + " MB";
  }

  function formatTime(ts) {
    const d = new Date(ts * 1000);
    return d.toLocaleString("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
  }

  function renderReportTable() {
    const filter = el("inv-report-filter").value.trim().toLowerCase();
    let rows = _allReports.slice();
    if (filter) {
      rows = rows.filter(r => r.file.toLowerCase().includes(filter));
    }
    rows.sort((a, b) => {
      let va, vb;
      if (_reportSort.col === "file") { va = a.file; vb = b.file; }
      else if (_reportSort.col === "size") { va = a.size; vb = b.size; }
      else { va = a.mtime; vb = b.mtime; }
      if (va < vb) return _reportSort.asc ? -1 : 1;
      if (va > vb) return _reportSort.asc ? 1 : -1;
      return 0;
    });

    const tbody = el("inv-report-table").querySelector("tbody");
    tbody.innerHTML = "";
    rows.forEach(r => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td><a href="/api/invest/report/${encodeURIComponent(r.file)}" target="_blank" rel="noopener">${escapeHtml(r.file)}</a></td>
        <td class="num">${formatBytes(r.size)}</td>
        <td class="num">${formatTime(r.mtime)}</td>
      `;
      tbody.appendChild(tr);
    });
    el("inv-report-count").textContent = `${rows.length} 个文件`;
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[c]));
  }

  // 暴露初始化入口
  window.investInit = investInit;
})();
