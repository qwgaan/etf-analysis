#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""杂志风投资尽调报告 → 自包含 HTML 渲染器（零第三方依赖）。

设计目标：
- 单文件、零依赖（仅标准库），输出**自包含 HTML**（CSS 内联，可直接双击打开/打印成 PDF）。
- 对齐 stock-advisor 模块 ⑤ 与 Prompt 8 的 Output Format。
- 不依赖飞书（lark-doc 已移除），纯本地产物。

报告结构（JSON / dict）见 skills/magazine-layout/references/template-spec.md。
"""
import argparse
import html
import json
import sys
from datetime import date


# ----------------------------------------------------------------------------
# 颜色与主题（浅色主题，杂志风）
# ----------------------------------------------------------------------------
# 涨=红、跌=绿（中国 A 股惯例）；中性灰。
SIGNAL_COLORS = {
    "up": "#c0392b",      # 涨 / 看多
    "down": "#1e8449",    # 跌 / 看空
    "flat": "#7a8699",    # 中性
}
VARIANT_COLORS = {
    "info":    ("#1f4e79", "#eaf2fb"),
    "warn":    ("#9a6b00", "#fdf3da"),
    "risk":    ("#9b2226", "#fbe9ea"),
    "success": ("#1e6b3a", "#e8f5ec"),
}
# 评分段位配色（0-5）
SCORE_COLORS = [("#c0392b", "弱"), ("#c0392b", "弱"), ("#d98324", "偏弱"),
                ("#c9a227", "中性"), ("#3a9d5d", "良"), ("#1e8449", "优")]


def esc(text) -> str:
    return html.escape("" if text is None else str(text))


def _score_color(score: float):
    idx = max(0, min(5, int(round(score))))
    return SCORE_COLORS[idx]


# ----------------------------------------------------------------------------
# 块渲染
# ----------------------------------------------------------------------------
def render_paragraph(b: dict) -> str:
    return f'<p class="para">{esc(b.get("text", ""))}</p>'


def render_heading(b: dict) -> str:
    lvl = b.get("level", 3)
    return f'<h{lvl} class="sub-h">{esc(b.get("text", ""))}</h{lvl}>'


def render_divider(_b: dict) -> str:
    return '<hr class="divider" />'


def render_list(b: dict) -> str:
    items = b.get("items", [])
    lis = "".join(f"<li>{esc(i)}</li>" for i in items)
    return f'<ul class="dot-list">{lis}</ul>'


def render_table(b: dict) -> str:
    caption = b.get("caption")
    headers = b.get("headers", [])
    rows = b.get("rows", [])
    thead = ""
    if headers:
        ths = "".join(f"<th>{esc(h)}</th>" for h in headers)
        thead = f"<thead><tr>{ths}</tr></thead>"
    body = ""
    for r in rows:
        tds = "".join(f"<td>{esc(c)}</td>" for c in r)
        body += f"<tr>{tds}</tr>"
    cap = f'<div class="tbl-cap">{esc(caption)}</div>' if caption else ""
    return f'{cap}<table class="data-tbl">{thead}<tbody>{body}</tbody></table>'


def render_callout(b: dict) -> str:
    variant = b.get("variant", "info")
    color, bg = VARIANT_COLORS.get(variant, VARIANT_COLORS["info"])
    title = b.get("title", "")
    text = b.get("text", "")
    t = f'<div class="callout-title">{esc(title)}</div>' if title else ""
    return (f'<div class="callout" style="border-left-color:{color};background:{bg}">'
            f'{t}<div class="callout-body">{esc(text)}</div></div>')


def render_tags(b: dict) -> str:
    items = b.get("items", [])
    out = ""
    for it in items:
        label = it.get("label", "")
        kind = it.get("kind", "flat")   # bull / bear / neutral
        color = SIGNAL_COLORS.get(kind, SIGNAL_COLORS["flat"])
        out += f'<span class="tag" style="color:{color};border-color:{color}">{esc(label)}</span>'
    return f'<div class="tag-row">{out}</div>'


def render_verdict(v: dict) -> str:
    signal = v.get("signal", "")
    rating = v.get("rating", "")
    oneliner = v.get("oneliner", "")
    score = v.get("score")
    breakdown = v.get("score_breakdown", [])
    # 信号灯配色
    sig_color = SIGNAL_COLORS["flat"]
    if "买入" in signal or "🟢" in signal:
        sig_color = SIGNAL_COLORS["up"]
    elif "卖出" in signal or "🔴" in signal:
        sig_color = SIGNAL_COLORS["down"]

    score_html = ""
    if score is not None:
        sc, _ = _score_color(float(score))
        score_html = (f'<div class="verdict-score"><span class="vs-num" style="color:{sc}">'
                      f'{esc(score)}</span><span class="vs-max">/5</span></div>')
    bd_html = ""
    if breakdown:
        cells = ""
        for name, val in breakdown:
            c, _ = _score_color(float(val))
            cells += (f'<div class="bd-cell"><div class="bd-name">{esc(name)}</div>'
                      f'<div class="bd-val" style="color:{c}">{esc(val)}</div></div>')
        bd_html = f'<div class="bd-row">{cells}</div>'

    return f'''
    <div class="verdict-box">
      <div class="verdict-head">
        <div class="verdict-signal" style="color:{sig_color}">{esc(signal)}</div>
        {score_html}
      </div>
      <div class="verdict-rating">{esc(rating)}</div>
      <div class="verdict-oneliner">{esc(oneliner)}</div>
      {bd_html}
    </div>'''


BLOCK_RENDERERS = {
    "paragraph": render_paragraph,
    "heading": render_heading,
    "divider": render_divider,
    "list": render_list,
    "table": render_table,
    "callout": render_callout,
    "tags": render_tags,
}


def render_block(b: dict) -> str:
    btype = b.get("type", "paragraph")
    if btype == "verdict":
        return render_verdict(b.get("value", {}))
    fn = BLOCK_RENDERERS.get(btype, render_paragraph)
    return fn(b)


def render_kpis(kpis: list) -> str:
    if not kpis:
        return ""
    cards = ""
    for k in kpis:
        label = esc(k.get("label", ""))
        value = esc(k.get("value", ""))
        sub = esc(k.get("sub", ""))
        trend = k.get("trend", "flat")
        arrow = {"up": "▲", "down": "▼", "flat": "■"}.get(trend, "■")
        color = SIGNAL_COLORS.get(trend, SIGNAL_COLORS["flat"])
        cards += f'''
        <div class="kpi-card">
          <div class="kpi-label">{label}</div>
          <div class="kpi-value" style="color:{color}">{value} <span class="kpi-arrow">{arrow}</span></div>
          <div class="kpi-sub">{sub}</div>
        </div>'''
    return f'<div class="kpi-grid">{cards}</div>'


def render_sections(sections: list) -> str:
    out = ""
    for s in sections:
        sid = esc(s.get("id", ""))
        title = esc(s.get("title", ""))
        blocks = "".join(render_block(b) for b in s.get("blocks", []))
        out += f'''
        <section class="report-sec">
          <div class="sec-head"><span class="sec-num">{sid}</span><h2 class="sec-title">{title}</h2></div>
          <div class="sec-body">{blocks}</div>
        </section>'''
    return out


def render_monitor(monitor: list) -> str:
    if not monitor:
        return ""
    rows = ""
    for m in monitor:
        trigger = esc(m.get("trigger", ""))
        action = esc(m.get("action", ""))
        rows += (f'<tr><td class="mon-trig">{trigger}</td>'
                 f'<td class="mon-act">{action}</td></tr>')
    return f'''
    <section class="report-sec">
      <div class="sec-head"><span class="sec-num">★</span><h2 class="sec-title">未来监控清单</h2></div>
      <div class="sec-body">
        <table class="data-tbl monitor-tbl">
          <thead><tr><th>触发条件</th><th>应对动作</th></tr></thead>
          <tbody>{rows}</tbody>
        </table>
      </div>
    </section>'''


def render_references(refs: list) -> str:
    if not refs:
        return ""
    items = "".join(f"<li>{esc(r)}</li>" for r in refs)
    return f'''
    <section class="report-sec">
      <div class="sec-head"><span class="sec-num">§</span><h2 class="sec-title">引用来源清单</h2></div>
      <div class="sec-body"><ul class="ref-list">{items}</ul></div>
    </section>'''


# ----------------------------------------------------------------------------
# 主文档
# ----------------------------------------------------------------------------
CSS = """
:root{
  --ink:#16233b; --ink-soft:#3a4a66; --muted:#7a8699; --line:#e3e8f0;
  --gold:#c8a45c; --paper:#ffffff; --bg:#f4f6fa;
}
*{box-sizing:border-box;}
body{margin:0;background:var(--bg);color:var(--ink);
  font-family:"PingFang SC","Microsoft YaHei","Hiragino Sans GB",-apple-system,"Segoe UI",Roboto,sans-serif;
  font-size:15px;line-height:1.75;}
.wrap{max-width:880px;margin:0 auto;background:var(--paper);
  box-shadow:0 4px 30px rgba(20,35,59,.08);min-height:100vh;}
/* 封面 */
.cover{padding:54px 56px 46px;background:linear-gradient(135deg,#16233b 0%,#27406a 100%);
  color:#fff;position:relative;overflow:hidden;}
.cover::after{content:"";position:absolute;right:-60px;top:-60px;width:240px;height:240px;
  border:1px solid rgba(200,164,92,.35);border-radius:50%;}
.cover-eyebrow{font-size:12px;letter-spacing:.32em;color:var(--gold);text-transform:uppercase;}
.cover-stock{display:flex;align-items:baseline;gap:14px;margin:18px 0 6px;}
.cover-name{font-family:"Noto Serif SC","Songti SC",STSong,SimSun,serif;font-size:40px;font-weight:700;}
.cover-code{font-size:18px;color:#cdd6e6;letter-spacing:.08em;}
.cover-title{font-size:15px;color:#aeb9cc;margin-top:4px;}
.cover-meta{margin-top:30px;display:flex;gap:26px;flex-wrap:wrap;font-size:12.5px;color:#9fb0c9;}
.cover-meta b{color:#dfe6f2;font-weight:600;}
.cover-badge{position:absolute;right:34px;bottom:34px;border:1px solid rgba(200,164,92,.5);
  color:var(--gold);padding:6px 12px;border-radius:20px;font-size:12px;letter-spacing:.05em;}
/* KPI */
.kpi-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;padding:26px 56px 6px;}
.kpi-card{border:1px solid var(--line);border-radius:10px;padding:14px 16px;background:#fbfcfe;}
.kpi-label{font-size:12px;color:var(--muted);}
.kpi-value{font-size:23px;font-weight:700;margin:4px 0 2px;font-variant-numeric:tabular-nums;}
.kpi-arrow{font-size:13px;}
.kpi-sub{font-size:11.5px;color:var(--muted);}
/* 区块 */
.report-sec{padding:22px 56px 6px;}
.sec-head{display:flex;align-items:center;gap:14px;border-bottom:2px solid var(--ink);
  padding-bottom:8px;margin-bottom:16px;}
.sec-num{font-family:"Noto Serif SC",serif;font-size:26px;font-weight:700;color:var(--gold);
  font-variant-numeric:tabular-nums;}
.sec-title{margin:0;font-size:20px;font-weight:700;color:var(--ink);letter-spacing:.02em;}
.sec-body{padding-bottom:8px;}
.para{margin:10px 0;color:var(--ink-soft);}
.sub-h{font-size:16px;color:var(--ink);margin:18px 0 8px;}
.divider{border:none;border-top:1px dashed var(--line);margin:18px 0;}
/* 结论卡 */
.verdict-box{border:1px solid var(--line);border-radius:12px;padding:20px 22px;
  background:linear-gradient(180deg,#fbfcfe,#f6f8fc);}
.verdict-head{display:flex;align-items:flex-end;justify-content:space-between;}
.verdict-signal{font-size:22px;font-weight:700;}
.verdict-score{text-align:right;}
.vs-num{font-size:34px;font-weight:800;font-variant-numeric:tabular-nums;}
.vs-max{font-size:14px;color:var(--muted);}
.verdict-rating{font-size:14px;color:var(--ink-soft);margin-top:6px;font-weight:600;}
.verdict-oneliner{margin-top:8px;color:var(--ink-soft);font-size:15px;}
.bd-row{display:flex;gap:10px;margin-top:16px;flex-wrap:wrap;}
.bd-cell{flex:1;min-width:92px;border:1px solid var(--line);border-radius:8px;
  padding:8px 10px;text-align:center;background:#fff;}
.bd-name{font-size:12px;color:var(--muted);}
.bd-val{font-size:18px;font-weight:700;font-variant-numeric:tabular-nums;}
/* 表格 */
.tbl-cap{font-size:12.5px;color:var(--muted);margin:14px 0 4px;font-weight:600;}
.data-tbl{width:100%;border-collapse:collapse;margin:8px 0 14px;font-size:13.5px;}
.data-tbl th{background:#16233b;color:#fff;text-align:left;padding:9px 12px;font-weight:600;
  font-size:12.5px;letter-spacing:.02em;}
.data-tbl td{padding:8px 12px;border-bottom:1px solid var(--line);color:var(--ink-soft);
  font-variant-numeric:tabular-nums;}
.data-tbl tbody tr:nth-child(even){background:#fafbfd;}
.data-tbl tbody tr:hover{background:#f1f5fb;}
.monitor-tbl th{background:#27406a;}
.mon-trig{font-weight:600;color:var(--ink);}
.mon-act{color:#1e6b3a;}
/* 标注框 */
.callout{border-left:4px solid #1f4e79;border-radius:0 8px 8px 0;padding:12px 16px;margin:12px 0;}
.callout-title{font-weight:700;margin-bottom:4px;font-size:14px;}
.callout-body{color:var(--ink-soft);font-size:14px;}
/* 标签 */
.tag-row{margin:10px 0;display:flex;gap:8px;flex-wrap:wrap;}
.tag{border:1px solid;padding:3px 12px;border-radius:16px;font-size:12.5px;font-weight:600;}
/* 列表 */
.dot-list{margin:8px 0;padding-left:20px;color:var(--ink-soft);}
.dot-list li{margin:5px 0;}
.ref-list{margin:8px 0;padding-left:20px;color:var(--muted);font-size:13px;}
.ref-list li{margin:4px 0;}
/* 页脚 */
.footer{padding:22px 56px 40px;color:var(--muted);font-size:12px;border-top:1px solid var(--line);
  margin-top:18px;line-height:1.7;}
.footer .disc{background:#fbf6ea;border:1px solid #ecd9a8;color:#7a5b13;border-radius:8px;
  padding:10px 14px;margin-bottom:12px;}
@media print{
  body{background:#fff;}
  .wrap{box-shadow:none;max-width:none;}
  .cover{-webkit-print-color-adjust:exact;print-color-adjust:exact;}
  .data-tbl th{-webkit-print-color-adjust:exact;print-color-adjust:exact;}
}
"""


def render_report(report: dict) -> str:
    meta = report.get("meta", {})
    title = esc(meta.get("title", "投资者尽职调查报告"))
    name = esc(meta.get("stock_name", ""))
    code = esc(meta.get("stock_code", ""))
    rdate = esc(meta.get("report_date", date.today().isoformat()))
    analyst = esc(meta.get("analyst", "stock-advisor · AI 研究辅助"))
    source = esc(meta.get("source", ""))
    badge = esc(meta.get("badge", "本地 HTML · 无云端"))
    disclaimer = esc(meta.get("disclaimer",
        "本报告由 AI 基于公开数据自动生成，仅供研究参考，不构成任何投资建议。"
        "投资有风险，决策须谨慎，盈亏自负。"))

    kpis = render_kpis(report.get("kpis", []))
    verdict = render_verdict(report["verdict"]) if report.get("verdict") else ""
    sections = render_sections(report.get("sections", []))
    monitor = render_monitor(report.get("monitor", []))
    refs = render_references(report.get("references", []))

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{name} {code} · {title}</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
  <header class="cover">
    <div class="cover-eyebrow">Investment Due-Diligence Report</div>
    <div class="cover-stock">
      <span class="cover-name">{name}</span>
      <span class="cover-code">{code}</span>
    </div>
    <div class="cover-title">{title}</div>
    <div class="cover-meta">
      <span>报告日期 <b>{rdate}</b></span>
      <span>生成 <b>{analyst}</b></span>
      {f'<span>数据源 <b>{source}</b></span>' if source else ''}
    </div>
    <div class="cover-badge">{badge}</div>
  </header>

  {kpis}

  <section class="report-sec">
    <div class="sec-head"><span class="sec-num">✓</span><h2 class="sec-title">投资结论摘要</h2></div>
    <div class="sec-body">{verdict}</div>
  </section>

  {sections}
  {monitor}
  {refs}

  <footer class="footer">
    <div class="disc">⚠️ 免责声明：{disclaimer}</div>
    <div>本报告由 stock-advisor 流水线 + magazine-layout 本地渲染生成 · 数据源：{source or '—'} · 生成时间 {rdate}</div>
  </footer>
</div>
</body>
</html>"""


def main():
    ap = argparse.ArgumentParser(description="渲染杂志风投资尽调报告为自包含 HTML")
    ap.add_argument("--input", "-i", required=True, help="报告 JSON 文件路径")
    ap.add_argument("--output", "-o", help="输出 HTML 路径（默认与输入同名 .html）")
    args = ap.parse_args()

    with open(args.input, "r", encoding="utf-8") as f:
        report = json.load(f)

    html_str = render_report(report)
    out = args.output or (args.input.rsplit(".", 1)[0] + ".html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html_str)
    print(f"OK 报告已生成: {out}  ({len(html_str)} bytes)")


if __name__ == "__main__":
    main()
