#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模块③（多维交叉验证）与模块④（私董会四幕僚）的 LLM 生成器。

设计原则：
  · LLM 只做"归因/辩论/综合"，所有数字来自本地数据层与证据层，禁止让模型自行编造行情数据
  · 强制矛盾标注：prompt 要求至少输出 2 条矛盾/反证，缺失则视为失败并重试
  · 强制来源标注：每条论据必须指明来自 A/B/C/D 哪类证据或本地指标
  · 全部走 chat_json，输出 schema 固定，便于转成 build_report 的 blocks
  · LLM 失败时由 run_report.py 回落到规则引擎，不阻断流水线

对外接口：
  metrics_to_text(computed, extra)          → 指标摘要文本
  gen_module3(...)  → dict  交叉验证结果（含 flow_score 资金面评分）
  gen_module4(...)  → dict  四幕僚 + 主席汇总
  blocks_from_module3(m3) / blocks_from_module4(m4) → report blocks
"""
import sys, json
from invest.engine.llm_client import chat_json

MAX_EV_CHARS = 9000


# ============================================================
# 指标摘要
# ============================================================
def metrics_to_text(name, code, computed, is_bank, pe=None, pb=None, div_yield=None,
                    tech_s=None, fund_s=None, val_s=None):
    t = computed.get("tech", {}) or {}
    f = computed.get("fund", {}) or {}
    annual = f.get("annual") or []
    L = [f"标的：{name}（{code}）{'｜行业属性：银行（无毛利率口径，负债率~90% 属正常，估值以 PB 为主）' if is_bank else ''}"]
    L.append("\n### 本地技术面指标（akshare 实拉，截至 %s）" % t.get("date_last"))
    L.append(f"- 收盘 {t.get('close')}，当日涨跌 {t.get('chg_pct')}%，量比 {t.get('vol_ratio')}")
    L.append(f"- MA5/10/20/60 = {t.get('ma5')}/{t.get('ma10')}/{t.get('ma20')}/{t.get('ma60')}"
             f"，多头排列={t.get('bull_arrangement')}")
    L.append(f"- RSI(14)={t.get('rsi14')}，MACD 柱={(t.get('macd') or {}).get('bar')}"
             f"（DIF={(t.get('macd') or {}).get('dif')}, DEA={(t.get('macd') or {}).get('dea')}）")
    L.append(f"- 52 周区间 {t.get('lo52')}~{t.get('hi52')}，距高 {t.get('from_hi52_pct')}%，距低 {t.get('from_lo52_pct')}%")
    L.append("\n### 本地基本面指标（年度）")
    for r in annual[:4]:
        L.append(f"- {r.get('报告期')}：营收 {r.get('营业总收入')}，归母净利 {r.get('净利润')}，"
                 f"EPS {r.get('基本每股收益')}，ROE {r.get('净资产收益率')}，每股净资产 {r.get('每股净资产')}"
                 + ("" if is_bank else f"，毛利率 {r.get('销售毛利率')}"))
    latest = f.get("latest") or {}
    if latest:
        L.append(f"- 最新单期({latest.get('报告期','')})：资产负债率 {latest.get('资产负债率')}")
    L.append("\n### 估值（本地计算）")
    L.append(f"- PE(静态,按最新年报EPS) = {('%.1fx' % pe) if pe else '—'}；"
             f"PB = {('%.2fx' % pb) if pb else '—'}"
             + (f"；股息率(按输入每股分红) ≈ {div_yield*100:.2f}%" if div_yield else ""))
    if tech_s is not None:
        L.append(f"\n### 规则引擎打分（1~5，仅供参考，可在你的分析中质疑）")
        L.append(f"- 技术面 {tech_s}｜基本面 {fund_s}｜估值 {val_s}")
    return "\n".join(L)


# ============================================================
# 模块③ 多维交叉验证
# ============================================================
M3_SYS = """你是一位极其严谨的卖方研究总监，负责"多维交叉验证"。你的天职是对抗确认偏误。

工作要求：
1. 只使用【本地指标】与【外部证据】中出现的事实与数字。绝对禁止编造任何未在输入中出现的数字、目标价、机构名或日期。
2. 必须区分"一致信号"与"矛盾信号"。**至少必须给出 2 条矛盾/反证**（contradictions 数组长度 >= 2）。如果表面上高度一致，就要挖掘更深的背离（例如：股价与业绩节奏错位、估值分位与基本面趋势错位、机构口头看多与实际筹码流向错位、短期改善与长期趋势错位）。
3. 每条结论必须在 source 字段标注依据来源：使用 "本地技术面"/"本地基本面"/"A研报"/"B估值分位"/"C新闻"/"D资金面"/"E主题检索"/"F北向"/"G财务"/"H利率"/"I基金重仓" 之一或组合。
3b. 【E 主题检索】条目自带置信档与日期：[A]=财经媒体正文（可放心引用，但须连同日期与媒体名一起写进 text），[B]=公告或可能含同业的研报标题（引用前先确认标的是否为本公司，若是同业只能作横向参照，必须写明"同业"）。**引用管理层/高管表态时必须点明其身份与原话要点**，这类一手表态是最有价值的证据。E 段中带日期的近期表态优先级高于旧新闻。
3c. 【F 北向】【G 财务】【H 利率】【I 基金重仓】是硬数据层，权威性高：F=沪深港通北向净流入/外资增持榜（机构外资行为）；G=多期归母净利润/营收/ROE/EPS/毛利率（akshare 财务摘要口径，可能与公司公告原文有微小差异，若 E 段引用了公告口径的具体数字，以公告为准并注明）；H=LPR+国债收益率曲线（银行净息差核心驱动，10Y-2Y 斜率陡峭化利好息差修复）；I=基金重仓占比（机构季度持仓，反映机构态度，注意两融个股数据 akshare 暂不可得、不引用）。涉及估值/息差/业绩结论时，优先用 G/H 的硬数字而非 E 的新闻措辞。
4. flow_score 是你对【资金面】的独立打分（1.0~5.0，一位小数）。必须基于 D 类（股东户数趋势、户均持股市值、即时资金流）+ F 类（北向净流入方向、外资增持榜）+ I 类（基金重仓占比、机构持仓变化）证据综合判断，而不是凭感觉给 3.0。北向持续净流入+基金重仓占比高=偏多；北向流出+股东户数持续上升（筹码散户化）=偏空。
5. 语言：简体中文，专业、克制、不用营销腔。每条 text 控制在 40~110 字。

输出 JSON schema（严格遵守，不要多加字段）：
{
  "summary": "3~5 句话的交叉验证综述，点明核心一致点与核心背离",
  "consistent": [{"title":"简短标题","text":"论述","source":"依据来源"}],
  "contradictions": [{"title":"矛盾·xxx","text":"论述，必须说明这个矛盾对决策意味着什么","source":"依据来源"}],
  "risks": [{"title":"风险·xxx","text":"论述","source":"依据来源"}],
  "flow_score": 3.0,
  "flow_reason": "资金面打分理由，必须引用具体数字",
  "consensus_target": "机构一致预期概述（若 A 类证据有 EPS/PE 预测则据此描述，无则写'无可靠一致预期数据'）",
  "tags": [{"label":"不超过8字","kind":"bull"}]
}
kind 只能是 bull / bear / neutral。consistent 2~4 条，contradictions 2~4 条，risks 2~3 条，tags 3~5 个。"""


def gen_module3(name, code, is_bank, metrics_text, evidence_text, verbose=True):
    user = (f"【本地指标】\n{metrics_text}\n\n"
            f"【外部证据】\n{(evidence_text or '（无外部证据，请据本地指标做内部一致性/背离分析，并在 source 标注仅本地）')[:MAX_EV_CHARS]}\n\n"
            f"请对 {name}（{code}）执行多维交叉验证，按 schema 输出 JSON。")
    if verbose:
        print("[llm] 生成模块③ 多维交叉验证 …", file=sys.stderr)
    d = chat_json(M3_SYS, user, temperature=0.35, max_tokens=3000)
    # 校验：矛盾必须 >= 2
    if len(d.get("contradictions") or []) < 2:
        if verbose:
            print("[llm] 矛盾条目不足 2 条，追加一次强制重试 …", file=sys.stderr)
        d2 = chat_json(M3_SYS, user + "\n\n注意：上一次输出的矛盾信号不足 2 条，本次必须给出至少 2 条真实背离。",
                       temperature=0.5, max_tokens=3000)
        if len(d2.get("contradictions") or []) >= len(d.get("contradictions") or []):
            d = d2
    try:
        fs = float(d.get("flow_score") or 3.0)
        d["flow_score"] = round(max(1.0, min(5.0, fs)), 1)
    except Exception:
        d["flow_score"] = 3.0
    return d


# ============================================================
# 模块④ 私董会四幕僚
# ============================================================
M4_SYS = """你在主持一场投资私董会。四位幕僚各有固定人格，必须真实交锋、不许和稀泥。

四位角色：
- 看多队长（Bull Captain）：立场偏多，寻找上行不对称机会。
- 看空队长（Bear Captain）：立场偏空，寻找下行风险与逻辑漏洞。
- 行业老炮（Industry Veteran）：中立偏务实，从生意模式、行业格局、监管与周期位置出发，最看不起纯图形派。
- 数据审计（Data Auditor）：中立偏严谨，只关心数据口径、可验证性、样本与时点是否自洽，负责挑穿另外三人话里的数据硬伤。

铁律：
1. 每人必须给出 3 条核心论据（args）、1 条"我自己最担忧的反例"（counter）、1 条"立场变更条件"（flip）。counter 必须是真正打自己脸的内容，不许敷衍。
2. 所有论据只能基于输入的【本地指标】【交叉验证结果】。禁止编造数字。
3. 数据审计必须至少指出 1 处口径/时点/可验证性问题（例如：静态 PE 用的是去年年报 EPS、单期指标与年报跨期、股东户数为季度低频数据、资金流为单日快照等）。
4. 主席汇总必须包含：共识（≥3 位认同）、关键分歧（一句话点明争点本质）、盲点（至少 2 条，即四人都没充分讨论的东西）、最终结论。
5. 简体中文，务实克制。每条 args 40~100 字。

输出 JSON schema（严格遵守）：
{
  "bull":     {"stance":"偏多","args":["","",""],"counter":"","flip":""},
  "bear":     {"stance":"偏空","args":["","",""],"counter":"","flip":""},
  "veteran":  {"stance":"中立偏务实","args":["","",""],"counter":"","flip":""},
  "auditor":  {"stance":"中立偏严谨","args":["","",""],"counter":"","flip":""},
  "moderator":{"consensus":["",""],"disagreement":"","blindspots":["",""],"conclusion":"","stance_word":"偏积极|中性|偏谨慎"}
}"""


def gen_module4(name, code, is_bank, metrics_text, m3, verbose=True):
    m3_brief = json.dumps({
        "summary": m3.get("summary"),
        "consistent": m3.get("consistent"),
        "contradictions": m3.get("contradictions"),
        "risks": m3.get("risks"),
        "flow_score": m3.get("flow_score"),
        "flow_reason": m3.get("flow_reason"),
        "consensus_target": m3.get("consensus_target"),
    }, ensure_ascii=False, indent=1)
    user = (f"【本地指标】\n{metrics_text}\n\n"
            f"【模块③ 交叉验证结果】\n{m3_brief}\n\n"
            f"请围绕『是否以及如何参与 {name}（{code}）』召开私董会，按 schema 输出 JSON。")
    if verbose:
        print("[llm] 生成模块④ 私董会四幕僚 …", file=sys.stderr)
    return chat_json(M4_SYS, user, temperature=0.6, max_tokens=4000)


# ============================================================
# → report blocks
# ============================================================
def blocks_from_module3(m3, name, code):
    rows = []
    for it in (m3.get("consistent") or []):
        rows.append(["一致 · " + str(it.get("title", "")), str(it.get("text", "")), str(it.get("source", ""))])
    for it in (m3.get("contradictions") or []):
        rows.append([str(it.get("title", "")) if str(it.get("title", "")).startswith("矛盾")
                     else "矛盾 · " + str(it.get("title", "")),
                     str(it.get("text", "")), str(it.get("source", ""))])
    for it in (m3.get("risks") or []):
        rows.append([str(it.get("title", "")) if str(it.get("title", "")).startswith("风险")
                     else "风险 · " + str(it.get("title", "")),
                     str(it.get("text", "")), str(it.get("source", ""))])
    blocks = [
        {"type": "paragraph", "text": str(m3.get("summary") or "")},
        {"type": "table", "caption": "交叉验证矩阵（一致 / 矛盾 / 风险，含依据来源）",
         "headers": ["类型", "论述", "依据"], "rows": rows},
    ]
    if m3.get("consensus_target"):
        blocks.append({"type": "callout", "variant": "info", "title": "机构一致预期",
                       "text": str(m3.get("consensus_target"))})
    blocks.append({"type": "callout", "variant": "warn", "title": f"资金面独立评分 {m3.get('flow_score')}/5",
                   "text": str(m3.get("flow_reason") or "")})
    tags = []
    for t in (m3.get("tags") or [])[:6]:
        k = str(t.get("kind", "neutral"))
        tags.append({"label": str(t.get("label", ""))[:10],
                     "kind": k if k in ("bull", "bear", "neutral") else "neutral"})
    if tags:
        blocks.append({"type": "tags", "items": tags})
    return blocks


_ROLE_TITLE = {
    "bull": "看多队长（Bull Captain）",
    "bear": "看空队长（Bear Captain）",
    "veteran": "行业老炮（Industry Veteran）",
    "auditor": "数据审计（Data Auditor）",
}


def blocks_from_module4(m4):
    blocks = []
    for key in ("bull", "bear", "veteran", "auditor"):
        r = m4.get(key) or {}
        blocks.append({"type": "heading", "level": 3,
                       "text": f"{_ROLE_TITLE[key]} · {r.get('stance', '')}"})
        items = [f"核心论据{i+1}：{a}" for i, a in enumerate((r.get("args") or [])[:3])]
        if r.get("counter"):
            items.append(f"最担忧反例：{r['counter']}")
        if r.get("flip"):
            items.append(f"立场变更条件：{r['flip']}")
        blocks.append({"type": "list", "items": items})
    mod = m4.get("moderator") or {}
    blocks.append({"type": "heading", "level": 3, "text": "主席汇总（Moderator）"})
    items = []
    cons = mod.get("consensus") or []
    if cons:
        items.append("共识（≥3 位认同）：" + "；".join(str(c) for c in cons))
    if mod.get("disagreement"):
        items.append("关键分歧：" + str(mod["disagreement"]))
    bl = mod.get("blindspots") or []
    if bl:
        items.append("盲点：" + "；".join(f"{i+1}) {b}" for i, b in enumerate(bl)))
    if mod.get("conclusion"):
        items.append("结论：" + str(mod["conclusion"]))
    blocks.append({"type": "list", "items": items})
    if mod.get("stance_word"):
        blocks.append({"type": "callout", "variant": (
            "success" if "积极" in str(mod["stance_word"]) else
            ("warn" if "谨慎" in str(mod["stance_word"]) else "info")),
            "title": f"私董会综合立场：{mod['stance_word']}",
            "text": str(mod.get("conclusion") or "")})
    return blocks
