#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键尽调报告编排器（stock-advisor 流水线 2.0 · LLM 全自动）。

链条：取数(_probe) → 计算(_compute) → 证据采集(web_evidence) → LLM 生成模块③④(llm_modules)
      → 组装报告 JSON → 渲染 HTML(build_report) → 导出 PDF(pdf_export)

模块分工：
  ①技术面 / ②基本面 / ⑤排版 —— akshare 实拉数据 + 规则引擎评分，全自动
  ③多维交叉验证 —— 证据层(机构研报共识/估值历史分位/个股新闻/股东户数/即时资金流/东财语义主题检索/北向资金/硬财务/利率环境/基金重仓)
                    喂给 LLM，强制矛盾标注 + 来源标注，并由 LLM 独立给出资金面评分
  ④私董会 —— LLM 扮演四幕僚(看多/看空/行业老炮/数据审计)交锋 + 主席汇总盲点
  自适应评分 —— 读 profile(风险偏好/投资周期/仓位上限) 调制权重、综合分与仓位建议

LLM 不可用或调用失败时，模块③④自动回落规则引擎，流水线不中断。

用法：
  python run_report.py --code 600036 --name 招商银行                       # 全自动(LLM+联网证据)
  python run_report.py --code 600036 --name 招商银行 --suffix llm          # 加后缀，避免覆盖旧报告
  python run_report.py --code 600519 --name 贵州茅台 --profile my.json     # 指定投资者画像
  python run_report.py --code 300750 --name 宁德时代 --no-llm              # 纯规则引擎
  python run_report.py --code 600276 --name 恒瑞医药 --no-web --no-pdf     # 不联网、仅出 HTML
  python run_report.py --code 600036 --name 招商银行 --fresh               # 强制刷新证据缓存
"""
import os, sys, json, argparse, time
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from invest import paths
from invest.engine import probe as probe_mod
from invest.engine import compute as compute_mod

DATE = date.today().isoformat()


# ============================================================
# 1) 取数 + 计算（原为 subprocess 调 CLI，已改为函数直调）
# ============================================================
def run_probe(code, start=None, on_progress=None):
    """取数：直接调 probe_stock()，不再起子进程。返回 probe dict。"""
    return probe_mod.probe_stock(code, start=start, on_progress=on_progress)


def run_compute(code, probe_data=None, on_progress=None):
    """计算：直接调 compute_all()，不再起子进程。返回 computed dict。"""
    if probe_data is None:
        probe_data = json.load(open(paths.probe_path(code), encoding="utf-8"))
    return compute_mod.compute_all(probe_data, on_progress=on_progress)


# ============================================================
# 2) 模块评分（规则引擎，1~5）
# ============================================================
def clamp(x, lo=1.0, hi=5.0):
    return max(lo, min(hi, x))

def score_tech(t):
    s = 2.5
    if t.get("bull_arrangement"):
        s += 1.0
    else:
        mas = [t.get(k) for k in ("ma5", "ma10", "ma20", "ma60") if t.get(k)]
        close = t.get("close")
        if mas and close and all(close < m for m in mas):
            s -= 1.0
    rsi = t.get("rsi14") or 50
    if 50 <= rsi <= 70: s += 0.5
    elif rsi > 70: s += 0.2
    elif 30 <= rsi < 50: s -= 0.2
    elif rsi < 30: s -= 0.3
    bar = (t.get("macd") or {}).get("bar")
    if bar is not None:
        s += 0.5 if bar > 0 else -0.5
    fhi = t.get("from_hi52_pct")
    if fhi is not None:
        if fhi > -5: s += 0.5
        elif fhi < -20: s -= 0.3
    vr = t.get("vol_ratio")
    if vr is not None and vr < 0.6: s -= 0.3
    return round(clamp(s), 1)

def score_fund(f, is_bank):
    s = 3.0
    annual = f.get("annual") or []
    if annual:
        cur, prev = annual[0], (annual[1] if len(annual) > 1 else None)
        def num(x):
            try: return float(str(x).replace("亿", "").replace("%", ""))
            except: return None
        # 增速
        if cur and prev:
            cnp, pnp = num(cur.get("净利润")), num(prev.get("净利润"))
            if cnp and pnp:
                g = (cnp - pnp) / abs(pnp)
                s += 0.5 if g > 0.05 else (-0.5 if g < 0 else 0.1)
        # ROE
        roe = num(cur.get("净资产收益率"))
        if roe is not None:
            if is_bank:
                s += 0.5 if roe >= 13 else (-0.3 if roe < 10 else 0)
            else:
                s += 0.5 if roe >= 15 else (0.2 if roe >= 10 else -0.5)
        # 毛利率（非银行）
        if not is_bank:
            gm = num(cur.get("销售毛利率"))
            if gm is not None:
                s += 0.5 if gm >= 50 else (-0.3 if gm < 20 else 0)
    # 负债率
    debt = (f.get("latest") or {}).get("资产负债率")
    if debt is not None:
        try:
            d = float(str(debt).replace("%", ""))
            if is_bank:
                s += 0.2 if d < 92 else (-0.3 if d > 94 else 0)  # 银行 90% 正常
            else:
                s += 0.5 if d < 40 else (0 if d < 70 else -0.5)
        except: pass
    return round(clamp(s), 1)

def refine_val_with_percentile(val_s, evidence):
    """用估值历史分位证据反哺规则引擎评分（避免只看绝对值导致顶格）。

    绝对 PE/PB 低 ≠ 便宜：还要看自身历史分位。分位高说明相对自己已不便宜。
    返回 (修正后评分, 修正说明 or None)
    """
    v = (evidence or {}).get("valuation") or {}
    if not v:
        return val_s, None
    adj, notes = 0.0, []
    for key, w in (("市净率", 1.0), ("市盈率(TTM)", 0.8)):
        d = v.get(key)
        if not d:
            continue
        p = d.get("percentile")
        if p is None:
            continue
        if p < 20:
            a = 0.3 * w; notes.append(f"{key}处 {p}% 低分位(+{a:.1f})")
        elif p > 70:
            a = -0.5 * w; notes.append(f"{key}处 {p}% 高分位({a:.1f})")
        elif p > 55:
            a = -0.25 * w; notes.append(f"{key}处 {p}% 中高分位({a:.1f})")
        else:
            a = 0.0; notes.append(f"{key}处 {p}% 中低分位(0)")
        adj += a
    if not notes:
        return val_s, None
    new = round(clamp(val_s + adj), 1)
    return new, f"绝对估值评分 {val_s} → 经近一年历史分位修正为 {new}（{'、'.join(notes)}）"


def score_val(t, f, is_bank, div_yield=None):
    s = 3.0
    close = t.get("close"); eps = None; bvps = None
    annual = f.get("annual") or []
    if annual:
        cur = annual[0]
        try: eps = float(str(cur.get("基本每股收益", "")).replace("—", ""))
        except: eps = None
        try: bvps = float(str(cur.get("每股净资产", "")).replace("—", ""))
        except: bvps = None
    pe = (close / eps) if (close and eps) else None
    pb = (close / bvps) if (close and bvps) else None
    if pe:
        s += 0.5 if pe < 20 else (-0.5 if pe > 60 else 0)
    if pb:
        if is_bank:
            s += 1.0 if pb < 1 else (0.3 if pb < 1.5 else -0.2)   # 破净=深度价值
        else:
            s += 1.0 if pb < 1 else (0 if pb < 3 else -0.3)
    if div_yield:
        s += 0.5 if div_yield > 0.04 else (0.2 if div_yield > 0.02 else 0)
    return round(clamp(s), 1), pe, pb


# ============================================================
# 3) 信号抽取 + 自动矛盾检测（模块③数据内部分）
# ============================================================
def derive_signals(t, f, is_bank, tech_s, fund_s, val_s, pe, pb):
    sig = []
    if t.get("bull_arrangement"):
        sig.append(("bull", "多头排列，价格站上全部均线"))
    elif all(t.get("close") < t.get(k) for k in ("ma5","ma10","ma20","ma60") if t.get(k)):
        sig.append(("bear", "价格跌破全部均线，空头排列"))
    if (t.get("macd") or {}).get("bar", 0) > 0:
        sig.append(("bull", "MACD 红柱为正，多头动能延续"))
    else:
        sig.append(("bear", "MACD 柱为负，动能偏弱"))
    annual = f.get("annual") or []
    if annual and len(annual) > 1:
        try:
            g = (float(str(annual[0]["净利润"]).replace("亿","")) - float(str(annual[1]["净利润"]).replace("亿",""))) / abs(float(str(annual[1]["净利润"]).replace("亿","")))
            if g > 0.05: sig.append(("bull", f"净利增速 {g*100:.1f}%，成长性佳"))
            elif g < 0: sig.append(("bear", f"净利同比 {g*100:.1f}%，增长承压"))
        except: pass
    if pb and pb < 1:
        sig.append(("bull", f"破净 PB={pb:.2f}x，深度价值区间"))
    elif pb and pb > 5:
        sig.append(("bear", f"PB={pb:.2f}x 偏高，估值透支"))
    if pe and pe < 12:
        sig.append(("bull", f"PE={pe:.1f}x 处低位"))
    return sig

def detect_contradictions(t, f, is_bank, tech_s, fund_s):
    cons = []
    # 技术强 vs 基本面弱
    if tech_s >= 4.0 and fund_s <= 3.0:
        cons.append(("矛盾·抢跑", "技术面走强(多头/近高) 但 基本面增速弱/ROE下行 —— '股价抢跑、业绩滞后'的修复预期交易，需业绩兑现验证。"))
    # 基本面强 vs 技术弱
    if fund_s >= 4.0 and tech_s <= 2.5:
        cons.append(("矛盾·好公司弱股价", "基本面优质 但 技术面空头/破位 —— 典型'好公司弱股价'背离，宜逢低而非追高。"))
    # 估值低 vs 基本面弱
    if (t.get("from_hi52_pct") or 0) < -15 and fund_s <= 3.0:
        cons.append(("风险·价值陷阱", "股价深度回撤+基本面走弱，需警惕'低估值陷阱'与估值中枢下移。"))
    # 破净但优质
    if is_bank and (t.get("close")):
        pass
    return cons


# ============================================================
# 4) 四幕僚自动起草（模块④）
# ============================================================
def build_counselors(signals, contradictions, tech_s, fund_s, val_s, t, f, is_bank):
    bulls = [s for k, s in signals if k == "bull"]
    bears = [s for k, s in signals if k == "bear"]
    sec = []
    sec.append({"type":"heading","level":3,"text":"看多队长（Bull Captain）· 偏多"})
    sec.append({"type":"list","items":(
        [f"核心论据{i+1}：{b}" for i, b in enumerate(bulls[:3])] +
        [f"最担忧反例：{bears[0] if bears else '估值修复不及预期，资金持续撤离'}",
         "立场变更条件：若技术面转弱(跌破MA20)或基本面恶化(净利转负)，则减仓。"]
    )})
    sec.append({"type":"heading","level":3,"text":"看空队长（Bear Captain）· 偏空"})
    sec.append({"type":"list","items":(
        [f"核心论据{i+1}：{b}" for i, b in enumerate(bears[:3])] +
        [f"最担忧反例：{bulls[0] if bulls else '估值修复超预期、长线资金持续增持'}",
         "立场变更条件：若放量突破前高且资金转净流入，则回补。"]
    )})
    sec.append({"type":"heading","level":3,"text":"行业老炮（Industry Veteran）· 中立偏务实"})
    sec.append({"type":"list","items":[
        f"核心论据1：{'银行属强监管强周期，破净多为系统性折价而非个体失败' if is_bank else '生意模式与行业格局决定长期价值，短期波动不改本质'}。",
        f"核心论据2：{'零售/财富管理护城河与资产质量(不良/拨备)是招行α来源' if is_bank else '护城河与现金流质量比单一增速更重要'}。",
        "核心论据3：分红(股息率)提供'类债'安全垫，适合与久期匹配的配置资金。",
        "最担忧反例：宏观与行业β(利率/地产/消费)可能系统性压低估值锚。",
        "立场变更条件：观察未来2-3个季度核心指标趋势是否延续。"
    ]})
    sec.append({"type":"heading","level":3,"text":"数据审计（Data Auditor）· 中立偏严谨"})
    sec.append({"type":"list","items":[
        "核心论据1：技术/财务数据自洽，均取自 akshare 本地数据层实拉。",
        f"核心论据2：{'银行无毛利率口径(已置空)、负债率~90%为正常结构' if is_bank else '毛利率/ROE/负债率口径一致，盈利质量可验证'}。",
        "核心论据3：最新单季指标日期早于年报，跨期比较需注意口径。",
        "最担忧反例：资金面(北向/公募/主力)数据需联网核验，本报告暂以中性默认。",
        "立场变更条件：联网交叉验证后回填资金面与最新研报目标价。"
    ]})
    # 主席汇总
    consensus = []
    if tech_s >= 3.5: consensus.append("技术面偏强/多头")
    if fund_s >= 3.5: consensus.append("基本面质量稳健")
    if val_s >= 4.0: consensus.append("估值处低位/破净")
    sec.append({"type":"heading","level":3,"text":"主席汇总（Moderator）"})
    sec.append({"type":"list","items":[
        f"共识(≥3位)：{('、'.join(consensus) if consensus else '数据自洽、无重大矛盾')}。",
        "关键分歧：看多 vs 看空 —— 争点是'当前信号能否延续为趋势'。",
        "盲点：①资金面(模块③联网部分)尚未回填；②宏观/政策与行业β未充分定价；③本报告四幕僚为规则引擎自动起草草案，须经 AI/人工复核。",
        f"结论：综合立场 {'偏积极' if (tech_s+fund_s+val_s)/3 >= 3.7 else ('中性' if (tech_s+fund_s+val_s)/3 >= 3.2 else '偏谨慎')}。"
    ]})
    return sec


# ============================================================
# 5) 自适应评分（profile）
# ============================================================
DIMS = ("技术面", "基本面", "估值", "资金面")

def adaptive(base_scores, profile):
    risk = (profile.get("risk") or "平衡")
    horizon = (profile.get("horizon") or "中线")
    if risk == "自定义":
        custom = profile.get("weights") or {}
        w = {d: float(custom.get(d, 0.25)) for d in DIMS}
    else:
        w = {"技术面":0.25,"基本面":0.25,"估值":0.25,"资金面":0.25}
        if risk == "保守":
            w = {"技术面":0.15,"基本面":0.30,"估值":0.35,"资金面":0.20}
        elif risk == "激进":
            w = {"技术面":0.35,"基本面":0.25,"估值":0.20,"资金面":0.20}
    if horizon == "短线":
        w["技术面"] += 0.05; w["估值"] -= 0.05
    elif horizon == "长线":
        w["基本面"] += 0.05; w["估值"] += 0.05; w["技术面"] -= 0.10
    # 归一
    tot = sum(w.values()); w = {k: v/tot for k, v in w.items()}
    adj = sum(base_scores[k]*w[k] for k in w)
    return round(adj, 2), w

def _norm_max_position(v):
    """把前端可能传来的 '15'/15/0.15/'15%' 统一归一化为 0~1 小数。"""
    try:
        fv = float(str(v).replace("%", ""))
        cap = fv / 100 if fv > 1 else fv
    except (TypeError, ValueError):
        cap = 0.15
    return min(max(cap, 0.0), 1.0)

def position_note(adj_score, profile):
    cap = _norm_max_position(profile.get("max_position"))
    size = round(min(cap, cap * (adj_score/5.0)), 3)
    if adj_score >= 4.0:
        stance = "可积极参与"
    elif adj_score >= 3.3:
        stance = "分批建仓"
    elif adj_score >= 3.0:
        stance = "小仓试探/观望"
    else:
        stance = "暂不参与"
    return stance, size


# ============================================================
# 6) 组装报告
# ============================================================
def assemble(code, name, probe, computed, profile, div_yield=None,
             m3=None, m4=None, evidence=None):
    t = computed.get("tech", {})
    f = computed.get("fund", {})
    is_bank = "银行" in (name or "")
    tech_s = score_tech(t)
    fund_s = score_fund(f, is_bank)
    val_s, pe, pb = score_val(t, f, is_bank, div_yield)
    # 证据反哺：用估值历史分位修正绝对估值评分
    val_s, val_note = refine_val_with_percentile(val_s, evidence)
    # 资金面：有 LLM 交叉验证则用其独立评分，否则中性默认
    flow_s = float(m3.get("flow_score")) if (m3 and m3.get("flow_score")) else 3.0
    base = {"技术面":tech_s,"基本面":fund_s,"估值":val_s,"资金面":flow_s}
    adj_score, weights = adaptive(base, profile)
    base_mean = round((tech_s+fund_s+val_s+flow_s)/4, 2)
    stance, size = position_note(adj_score, profile)
    signals = derive_signals(t, f, is_bank, tech_s, fund_s, val_s, pe, pb)
    contradictions = detect_contradictions(t, f, is_bank, tech_s, fund_s)

    close = t.get("close")
    annual = f.get("annual") or []
    cur = annual[0] if annual else {}
    def num(x):
        try: return float(str(x).replace("亿","").replace("%",""))
        except: return None
    np_cur = cur.get("净利润"); rev_cur = cur.get("营业总收入")
    roe_cur = cur.get("净资产收益率"); bvps = cur.get("每股净资产")

    # ---- KPI ----
    kpis = [
        {"label":"最新收盘","value":str(close),"sub":t.get("date_last",""),"trend":"up" if (t.get("chg_pct") or 0)>=0 else "down"},
        {"label":"归母净利润(最新年)","value":str(np_cur),"sub":f"营收{rev_cur}","trend":"up" if (num(np_cur) or 0)>=0 else "down"},
        {"label":"ROE","value":str(roe_cur),"sub":"最新年度","trend":"down" if (num(roe_cur) or 99)<13 else "flat"},
    ]
    if is_bank:
        kpis.append({"label":"资产负债率","value":str((f.get('latest') or {}).get('资产负债率')),"sub":"银行正常结构","trend":"down"})
    else:
        gm = cur.get("销售毛利率")
        kpis.append({"label":"销售毛利率","value":str(gm),"sub":"最新年度","trend":"up" if (num(gm) or 0)>=40 else "flat"})
    kpis.append({"label":"静态PE" if not pe else "PE","value":(f"{pe:.1f}x" if pe else "—"),"sub":"EPS "+str(cur.get("基本每股收益")),"trend":"flat"})
    kpis.append({"label":"PB","value":(f"{pb:.2f}x" if pb else "—"),"sub":("破净" if (pb and pb<1) else "—"),"trend":"down" if (pb and pb<1) else "flat"})

    # ---- 技术面 section ----
    tech_blocks = [
        {"type":"paragraph","text":f"截至{t.get('date_last')}收盘{close}元，近一年区间{t.get('lo52')}–{t.get('hi52')}元，较52周高{t.get('from_hi52_pct')}%、较52周低{t.get('from_lo52_pct')}%。日涨跌{t.get('chg_pct')}%，量比{t.get('vol_ratio')}。"},
        {"type":"table","caption":"关键技术指标","headers":["指标","数值","解读"],
         "rows":[
            ["MA5/MA10/MA20/MA60", f"{t.get('ma5')} / {t.get('ma10')} / {t.get('ma20')} / {t.get('ma60')}", "价格%s全部均线" % ("站上" if t.get("bull_arrangement") else "低于")],
            ["RSI(14)", str(t.get("rsi14")), "偏强区" if (t.get("rsi14") or 50)>=50 else "偏弱区"],
            ["MACD(柱)", str((t.get("macd") or {}).get("bar")), "红柱为正·多头" if (t.get("macd") or {}).get("bar",0)>0 else "绿柱·空头"],
            ["52周区间", f"{t.get('lo52')}–{t.get('hi52')}", f"现价距高{t.get('from_hi52_pct')}%"],
         ]},
        {"type":"callout","variant":("success" if t.get("bull_arrangement") else "info"),"title":"形态判定","text":("多头排列确认，短→中长期均线向上发散，动能健康。" if t.get("bull_arrangement") else "均线系统偏空/收敛，缺乏明确方向，等待方向选择。")},
        {"type":"tags","items":([
            {"label":"多头排列","kind":"bull"} if t.get("bull_arrangement") else {"label":"非多头","kind":"bear"},
            {"label":"MACD红柱" if (t.get("macd") or {}).get("bar",0)>0 else "MACD绿柱","kind":"bull" if (t.get("macd") or {}).get("bar",0)>0 else "bear"},
            {"label":f"RSI{t.get('rsi14')}","kind":"neutral"},
        ])},
        {"type":"paragraph","text":f"技术面评分 {tech_s}/5。"},
    ]

    # ---- 基本面 section ----
    annual_rows = []
    for r in annual[:5]:
        annual_rows.append([str(r.get("报告期")), str(r.get("营业总收入")), str(r.get("净利润")),
                            str(r.get("基本每股收益")), str(r.get("净资产收益率")), str(r.get("每股净资产"))])
    fund_blocks = [
        {"type":"paragraph","text":f"最新年度营收{rev_cur}、归母净利{np_cur}、EPS{cur.get('基本每股收益')}、ROE{cur.get('净资产收益率')}。{'银行无毛利率口径。' if is_bank else ''}"},
        {"type":"table","caption":"年度核心财务趋势（同花顺）","headers":["年度","营收","归母净利","EPS","ROE","每股净资产"],"rows":annual_rows},
        {"type":"callout","variant":"success","title":"杜邦/质量","text":(f"银行ROE≈净息差×杠杆×中收；资产质量看不良率/拨备(待联网补)。负债率{(f.get('latest') or {}).get('资产负债率')}为银行正常结构。" if is_bank else f"ROE{cur.get('净资产收益率')}、毛利率{cur.get('销售毛利率')}；负债率{(f.get('latest') or {}).get('资产负债率')}。盈利质量与杠杆结构见上表。")},
        {"type":"tags","items":([
            {"label":f"ROE{cur.get('净资产收益率')}","kind":"bull" if (num(str(cur.get('净资产收益率'))) or 0)>=13 else "neutral"},
            {"label":"破净" if (pb and pb<1) else "PB"+("%.2f"%pb if pb else ""),"kind":"bull" if (pb and pb<1) else "neutral"},
        ])},
        {"type":"paragraph","text":f"基本面评分 {fund_s}/5。"},
    ]

    # ---- 估值 section ----
    val_blocks = [
        {"type":"paragraph","text":f"基于收盘价{close}元、EPS{cur.get('基本每股收益')}、每股净资产{bvps}：PE{'%.1f'%pe if pe else '—'}x、PB{'%.2f'%pb if pb else '—'}x{'、股息率约%.1f%%'%(div_yield*100) if div_yield else ''}。"},
        {"type":"callout","variant":"info","title":"口径声明（避免 PE 混用误读）",
         "text":(f"本节 PE {('%.1fx'%pe) if pe else '—'} 为**静态 PE**，分母是最新年报（{cur.get('报告期','')}）EPS {cur.get('基本每股收益')} 元；"
                 "下方分位表中的「市盈率(TTM)」为滚动近四季口径，两者数值必然不同，不可直接比较。"
                 "PB 分母为最新年报每股净资产，未含年内利润留存，实际动态 PB 略低于此值。")},
        {"type":"table","caption":"估值参照","headers":["维度","当前","结论"],"rows":[
            ["PE", (f"{pe:.1f}x" if pe else "—"), "低位" if (pe and pe<20) else ("偏高" if (pe and pe>40) else "中性")],
            ["PB", (f"{pb:.2f}x" if pb else "—"), "破净·深度价值" if (pb and pb<1) else "中性"],
            ["股息率", (f"{div_yield*100:.1f}%" if div_yield else "待联网补"), "高股息" if (div_yield and div_yield>0.04) else "—"],
        ]},
        {"type":"callout","variant":"info","title":"估值反脆弱","text":("破净+股息构成安全垫；上行弹性来自盈利预期修复与资金回流。" if (pb and pb<1) else "估值需结合增速与行业中枢判断安全边际。")},
    ]
    # 估值历史分位（来自证据层）
    vperc = (evidence or {}).get("valuation") or {}
    if vperc:
        val_blocks.insert(2, {"type":"table","caption":"估值历史分位（近一年，百度股市通）",
            "headers":["指标","当前","近一年分位","区间(最低~最高)","中位"],
            "rows":[[k, str(d["current"]), f"{d['percentile']}%",
                     f"{d['min']}~{d['max']}", str(d["median"])] for k, d in vperc.items()]})
    if val_note:
        val_blocks.append({"type":"callout","variant":"warn","title":"分位修正（证据反哺评分）","text":val_note})
    val_blocks.append({"type":"paragraph","text":f"估值评分 {val_s}/5。"})

    # ---- 交叉验证 section（LLM 优先，规则引擎回落）----
    if m3:
        from invest.engine.llm_modules import blocks_from_module3
        cv_blocks = blocks_from_module3(m3, name, code)
        # 追加证据源清单，保证可追溯
        ev_rows = []
        r = (evidence or {}).get("research") or {}
        if r:
            ev_rows.append(["A 机构研报", f"{r.get('count')} 篇（{r.get('window')}），评级分布 {r.get('ratings')}",
                            "akshare stock_research_report_em"])
        v = (evidence or {}).get("valuation") or {}
        for k, d in list(v.items())[:4]:
            ev_rows.append([f"B {k}", f"当前 {d['current']}，近一年 {d['percentile']}% 分位（{d['min']}~{d['max']}）",
                            "akshare stock_zh_valuation_baidu"])
        ns = (evidence or {}).get("news") or []
        if ns:
            ev_rows.append(["C 个股新闻", f"{len(ns)} 条，最新 {ns[0].get('time','')}｜{ns[0].get('title','')[:40]}",
                            "akshare stock_news_em"])
        h = (evidence or {}).get("holders") or {}
        if h.get("latest"):
            la = h["latest"]
            ev_rows.append(["D 股东户数", f"{la.get('date')} {la.get('holders'):,} 户（{la.get('chg_pct'):+.2f}%），"
                                          f"户均市值 {la.get('avg_hold_value')} 万元 → {h.get('trend')}",
                            "akshare stock_zh_a_gdhs_detail_em"])
        ff = (evidence or {}).get("fund_flow") or {}
        if ff:
            ev_rows.append(["D 即时资金流", f"净额 {ff.get('net')}（流入 {ff.get('inflow')}/流出 {ff.get('outflow')}），"
                                            f"换手 {ff.get('turnover')}，榜内排名 {ff.get('rank')}/{ff.get('total')}",
                            "akshare stock_fund_flow_individual"])
        sr = (evidence or {}).get("search") or {}
        nsr = sum(len(x) for x in sr.values()) if sr else 0
        if nsr:
            # 真实使用的引擎与置信档由证据自身决定，不能写死
            engines = {it.get("engine") for items in sr.values() for it in (items or [])}
            tiers = [it.get("tier", "C") for items in sr.values() for it in (items or [])]
            n_a = tiers.count("A")
            dims = [d for d, v in sr.items() if v]
            if "eastmoney" in engines:
                iface = "东方财富 search-api-web（语义检索）"
                if len(engines) > 1:
                    iface += " + Bing/DDG 兜底"
                desc = (f"{nsr} 条主题证据（{n_a} 条 A 档财经媒体正文），"
                        f"覆盖 {len(dims)} 个维度：{'、'.join(dims)}")
            else:
                iface = " / ".join(sorted(e for e in engines if e)) or "Bing / DuckDuckGo"
                desc = f"{nsr} 条检索线索（C 档低置信，仅作补充），覆盖：{'、'.join(dims)}"
            ev_rows.append(["E 主题检索", desc, iface])
        # F~I 硬数据层
        nb = (evidence or {}).get("northbound") or {}
        if nb.get("flow") or nb.get("in_ranking"):
            flows = (nb.get("flow") or [])
            if flows and all(x["net_inflow"] == 0 and x["net_buy"] == 0 for x in flows):
                s = f"当日净流入未更新（最新交易日 {flows[0]['date']}，盘后发布）"
            else:
                s = "；".join(f"{x['board']}净流入{x['net_inflow']:.0f}亿" for x in flows)
            if nb.get("in_ranking"):
                s += f"；位列{nb.get('rank_market')}外资增持榜"
            ev_rows.append(["F 北向资金", s or "（已获取）", "东方财富 stock_hsgt_fund_flow_summary_em / hold_stock_em"])
        fin = (evidence or {}).get("financials") or {}
        if fin.get("items"):
            s = "；".join(f"{k}{v['latest']}{v['unit']}"
                          + (f"(同比{v['yoy_pct']:+.1f}%)" if v.get("yoy_pct") is not None else "")
                          for k, v in fin["items"].items() if v["latest"] is not None)
            ev_rows.append([f"G 硬财务({fin.get('latest_period')})", s, "akshare stock_financial_abstract"])
        rt = (evidence or {}).get("rates") or {}
        if rt.get("lpr") or rt.get("cgb"):
            parts = []
            if rt.get("lpr"):
                parts.append(f"LPR1Y {rt['lpr']['lpr_1y']}% / 5Y {rt['lpr']['lpr_5y']}%")
            if rt.get("cgb"):
                parts.append("国债 " + "，".join(f"{k} {v}%" for k, v in rt["cgb"].items()))
            ev_rows.append(["H 利率环境", "；".join(parts), "akshare macro_china_lpr / bond_zh_us_rate"])
        mf = (evidence or {}).get("margin_funds") or {}
        if mf.get("funds"):
            f = mf["funds"]
            ev_rows.append([f"I 基金重仓({f['count']}只)",
                            f"Top8 合计占流通股 {f['top_pct_sum']}%；两融个股数据 akshare 暂不可得"
                            f"（仅市场级汇总且过期），机构行为以基金重仓代理",
                            "akshare stock_fund_stock_holder"])
        if ev_rows:
            cv_blocks.append({"type": "table", "caption": "外部证据源清单（可追溯）",
                              "headers": ["证据类别", "内容摘要", "数据接口"], "rows": ev_rows})
        # 规则引擎的自动矛盾检测作为交叉校验保留
        if contradictions:
            cv_blocks.append({"type": "callout", "variant": "info", "title": "规则引擎独立复核（与 LLM 交叉校验）",
                              "text": "；".join(f"{a}：{b}" for a, b in contradictions)})
    else:
        contra_rows = [["一致·数据自洽","技术/财务均取自本地数据层，口径一致","自动"]]
        for tag, txt in contradictions:
            contra_rows.append([tag, txt, "自动检测"])
        cv_blocks = [
            {"type":"paragraph","text":"本模块分两层：①数据内自动矛盾检测（下方）；②联网交叉验证（4 条建议查询，待 AI/人工在会话中执行后回填）。"},
            {"type":"table","caption":"信号分类（数据内自动）","headers":["类型","内容","来源"],"rows":contra_rows},
            {"type":"callout","variant":"warn","title":"联网核验建议（待补）","text":"请在 AI 会话中执行以下 4 组检索并回填模块③、④资金面：\n"
                f"1) {name}({code}) 最新研报 评级 目标价 2026\n"
                f"2) {name} 最新季报/年报 业绩 增速 {'净息差' if is_bank else '毛利率'}\n"
                f"3) {name} 估值 PE PB 股息率 历史分位\n"
                f"4) {name} 北向资金 机构持仓 主力资金 近期"},
            {"type":"tags","items":([{"label":c[0].split('·')[-1],"kind":"bear"} for c in contradictions] or [{"label":"暂无显著矛盾","kind":"bull"}])},
        ]

    # ---- 私董会 section（LLM 优先，规则引擎回落）----
    if m4:
        from invest.engine.llm_modules import blocks_from_module4
        counsel = blocks_from_module4(m4)
    else:
        counsel = build_counselors(signals, contradictions, tech_s, fund_s, val_s, t, f, is_bank)

    # ---- monitor ----
    monitor = [
        {"trigger":"当 技术面转弱(跌破MA20) 或 基本面恶化(净利转负)","action":"逻辑证伪，复核/降低仓位"},
        {"trigger":"当 估值修复(PB/PE 回升至行业中枢上方) 且 资金转净流入","action":"确认右侧，上修仓位"},
        {"trigger":"当 宏观/行业重大负面(利率/地产/政策)落地","action":"重新评估估值锚"},
    ]

    # ---- verdict（规则引擎评分 × LLM 私董会立场 双向校准）----
    def sig_of(score):
        return ("🟢 积极（价值修复/成长）" if score >= 4.0 else
                ("🟡 谨慎看好/中性" if score >= 3.3 else "🔴 谨慎/观望"))

    verdict_signal = sig_of(adj_score)
    stance_word = ((m4 or {}).get("moderator") or {}).get("stance_word") or ""
    calib_note = None
    if stance_word:
        # 取更保守的一方：LLM 私董会偏谨慎时不给「积极」标签，避免机器乐观误导
        if "谨慎" in stance_word and adj_score >= 4.0:
            verdict_signal = "🟡 谨慎看好/中性"
            calib_note = (f"规则引擎综合分 {adj_score} 达「积极」档，但 LLM 私董会主席立场为「{stance_word}」，"
                          f"按「取更保守一方」原则下调至谨慎看好。分歧本身即风险提示：量化信号强于叙事逻辑。")
        elif "中性" in stance_word and adj_score >= 4.0:
            verdict_signal = "🟡 积极偏中性（信号强·叙事中性）"
            calib_note = (f"规则引擎综合分 {adj_score}（技术/估值分项强）判「积极」，而 LLM 私董会主席立场为「中性」——"
                          f"量化信号与基本面叙事不同步，通常意味着「价格先行、业绩待验证」，宜分批而非满仓。")
        elif "积极" in stance_word and adj_score < 3.3:
            verdict_signal = "🔴 谨慎/观望（LLM 偏积极但量化不支持）"
            calib_note = (f"LLM 私董会主席立场「{stance_word}」，但规则引擎综合分仅 {adj_score}，"
                          f"量化分项不支持进攻。以量化为准保持观望，等待信号确认。")
        else:
            calib_note = f"规则引擎综合分 {adj_score} 与 LLM 私董会立场「{stance_word}」方向一致，结论置信度较高。"
    llm_on = bool(m3 or m4)
    flow_label = "资金面" if m3 else "资金面(待核验)"

    # 把双向校准结论披露在私董会末尾
    if calib_note:
        counsel.append({"type": "callout",
                        "variant": ("info" if "一致" in calib_note else "warn"),
                        "title": "结论校准：规则引擎 × LLM 私董会", "text": calib_note})

    # ---- references ----
    refs = [
        f"akshare-sina stock_zh_a_daily ({code}) · 取数 {probe.get('daily_count')} 条 source={probe.get('daily_source')}",
        f"akshare-ths financial_abstract/indicator ({code}) · fin={probe.get('fin_count')} ind={probe.get('ind_count')}",
    ]
    if llm_on:
        ev = evidence or {}
        r = ev.get("research") or {}
        if r:
            refs.append(f"akshare stock_research_report_em ({code}) · 研报 {r.get('count')} 篇 "
                        f"{r.get('window')} 评级 {r.get('ratings')}")
        if ev.get("valuation"):
            refs.append("akshare stock_zh_valuation_baidu · 近一年 PE(TTM)/PE(静)/PB/PCF 分位数（n≈365）")
        if ev.get("news"):
            refs.append(f"akshare stock_news_em ({code}) · 个股新闻 {len(ev['news'])} 条（含正文摘要）")
        if ev.get("holders"):
            refs.append(f"akshare stock_zh_a_gdhs_detail_em ({code}) · 股东户数近 "
                        f"{len(ev['holders'].get('series') or [])} 期（筹码集中度代理）")
        if ev.get("fund_flow"):
            refs.append("akshare stock_fund_flow_individual · 即时资金流快照（单日，低频代理）")
        _sr = ev.get("search") or {}
        nsr = sum(len(x) for x in _sr.values())
        if nsr:
            _eng = {it.get("engine") for items in _sr.values() for it in (items or [])}
            _na = sum(1 for items in _sr.values() for it in (items or [])
                      if it.get("tier") == "A")
            if "eastmoney" in _eng:
                refs.append(f"东方财富 search-api-web（cmsArticle 语义检索 / cmsArticleWebOld / notice）"
                            f" · {nsr} 条主题证据，其中 A 档财经媒体正文 {_na} 条"
                            + ("；Bing/DDG 兜底" if len(_eng) > 1 else ""))
            else:
                refs.append(f"Bing / DuckDuckGo 网页检索 · {nsr} 条舆情线索（C 档低置信补充）")
        try:
            from invest.engine.llm_client import load_cfg
            refs.append(f"LLM 模块③④生成 · model={load_cfg().get('model')}（密钥本地存储，不入报告）")
        except Exception:
            refs.append("LLM 模块③④生成")
    else:
        refs.append("联网交叉验证(模块③)：4 组检索建议见报告内'联网核验建议'块，待 AI/人工回填")
    refs.append(f"自适应评分：风险={profile.get('risk')} 周期={profile.get('horizon')} "
                f"仓位上限={profile.get('max_position')} 权重={ {k: round(v,3) for k,v in weights.items()} }")

    report = {
      "meta":{
        "title":"投资者尽职调查报告" + ("（LLM 增强 · 全自动）" if llm_on else "（一键自动草案）"),
        "stock_name":name, "stock_code":code, "report_date":DATE,
        "analyst":("stock-advisor 流水线 · run_report.py（模块③④由 LLM 生成）" if llm_on
                   else "stock-advisor 流水线 · 自动编排器 run_report.py"),
        "source":("akshare 本地数据层(实拉) + 研报/估值分位/新闻/股东户数 证据层 + LLM 交叉验证与私董会" if llm_on
                  else "akshare 本地数据层(实拉) + 规则引擎自动合成 + 联网待补"),
        "badge":("本地 HTML · LLM 全自动五模块" if llm_on else "本地 HTML · 自动草案(需AI/人工复核)"),
        "disclaimer":(("本报告全流程由 run_report.py 自动生成：模块①②⑤基于 akshare 实拉数据渲染；模块③多维交叉验证与模块④私董会四幕僚"
                       "由 LLM 基于本地指标与外部证据（机构研报共识、估值历史分位、个股新闻、股东户数变化）生成，已强制要求矛盾标注与来源标注。"
                       "LLM 输出可能存在推理偏差，重要决策请人工复核原始数据。仅供研究参考，不构成投资建议。投资有风险，决策须谨慎。") if llm_on
                      else ("本报告由 run_report.py 一键编排生成：模块①②⑤为真实数据自动渲染，模块③含自动矛盾检测+联网建议(待补)，"
                            "模块④四幕僚为规则引擎自动起草草案。仅供研究参考，不构成投资建议。投资有风险，决策须谨慎。"))
      },
      "verdict":{
        "signal":verdict_signal,
        "rating":f"综合分 {adj_score}（基础均分 {base_mean}）· 自适应权重 {profile.get('risk')}/{profile.get('horizon')}",
        "oneliner":(f"技术{tech_s}/基本{fund_s}/估值{val_s}/资金{flow_s}。"
                    f"建议仓位：{stance}，单一标的建议上限 {int(size*100)}%（画像上限 {int(float(profile.get('max_position') or 0.15)*100)}%）。"),
        "score":adj_score,
        "score_breakdown":[["技术面",tech_s],["基本面",fund_s],["估值",val_s],[flow_label,flow_s]]
      },
      "kpis":kpis,
      "sections":[
        {"id":"01","title":"技术面速读（模块①·自动）","blocks":tech_blocks},
        {"id":"02","title":"基本面：财务质量（模块②·自动）","blocks":fund_blocks},
        {"id":"03","title":"多维交叉验证（模块③·" + ("LLM + 联网证据" if m3 else "自动+待联网") + "）","blocks":cv_blocks},
        {"id":"04","title":"估值仪表盘（模块③·自动）","blocks":val_blocks},
        {"id":"05","title":"私董会纪要（模块④·" + ("LLM 四幕僚辩论" if m4 else "规则起草草案") + "）","blocks":counsel},
      ],
      "monitor":monitor,
      "references":refs
    }
    return report, adj_score, weights, stance, size


# ============================================================
# 单只流水线（原 main() CLI，已重构为可调函数 run_single）
# ============================================================
STAGES = ["取数", "计算指标", "采集外部证据", "LLM 生成模块③④", "组装评分", "渲染报告"]


def run_single(code, name, profile=None, start=None, div=None,
               use_llm=True, use_web=True, use_search=True, fresh=False,
               suffix="", on_stage=None, on_progress=None, akshare_lock=None):
    """跑通单只股票的完整尽调流水线，返回结果 dict（供 Web 任务与批量复用）。

    参数：
      profile   —— 投资者画像 dict（risk/horizon/max_position/dividend_focus）
      use_llm   —— False 时模块③④直接走规则引擎，不调 LLM
      use_web   —— False 时不联网采集外部证据
      fresh     —— True 时忽略证据缓存
      on_stage(i, total, label) —— 阶段进度回调
      on_progress(msg)          —— 细粒度日志回调

    返回 dict：code/name/html_path/report/adj_score/weights/stance/position/
              score_breakdown/m3_source/evidence_used/error
    """
    paths.ensure_dirs()

    def _stage(i, label):
        if on_stage:
            try:
                on_stage(i, len(STAGES), label)
            except Exception:
                pass

    def _p(msg):
        if on_progress:
            try:
                on_progress(msg)
            except Exception:
                pass

    profile = dict(profile or {})
    profile.setdefault("risk", "平衡")
    profile.setdefault("horizon", "中线")
    profile.setdefault("max_position", 0.15)

    # 名称缺省时自动解析（A 股代码走 akshare_ds.resolve_name）
    if not name:
        try:
            from invest.engine.akshare_ds import resolve_name as _rn
            name = _rn(code) or code
        except Exception:
            name = code

    # ---------- 1) 取数 ----------
    # akshare 取数与计算是限流密集段，外部可传入全局锁串行化（invest/jobs.py）
    _import_contextlib = __import__("contextlib")
    _ctx = akshare_lock if akshare_lock is not None else _import_contextlib.nullcontext()
    with _ctx:
        _stage(1, STAGES[0])
        probe = run_probe(code, start=start, on_progress=on_progress)

        # ---------- 2) 计算 ----------
        _stage(2, STAGES[1])
        computed = run_compute(code, probe_data=probe, on_progress=on_progress)

    div_yield = None
    if div:
        try:
            div_yield = div / float(computed["tech"]["close"])
        except Exception:
            div_yield = None

    # ---------- 3~4) 证据采集 + LLM 生成（失败自动回落规则引擎）----------
    evidence, m3, m4 = None, None, None
    m3_source = "rule"

    if use_llm:
        t = computed.get("tech", {}) or {}
        f = computed.get("fund", {}) or {}
        is_bank = "银行" in (name or "")
        tech_s = score_tech(t)
        fund_s = score_fund(f, is_bank)
        val_s, pe, pb = score_val(t, f, is_bank, div_yield)
        try:
            from invest.engine.llm_client import available
            if not available():
                raise RuntimeError("未配置 LLM（invest/config/llm_config.json 或环境变量）")
            from invest.engine.llm_modules import metrics_to_text, gen_module3, gen_module4
            mtext = metrics_to_text(name, code, computed, is_bank,
                                    pe=pe, pb=pb, div_yield=div_yield,
                                    tech_s=tech_s, fund_s=fund_s, val_s=val_s)
            ev_text = ""
            if use_web:
                _stage(3, STAGES[2])
                from invest.engine.web_evidence import collect_evidence, evidence_to_text
                evidence = collect_evidence(code, name, is_bank,
                                            close=t.get("close"),
                                            use_cache=not fresh,
                                            with_search=use_search,
                                            on_progress=on_progress)
                ev_text = evidence_to_text(evidence)
            _stage(4, STAGES[3])
            _p("LLM 生成模块③ 多维交叉验证 …")
            m3 = gen_module3(name, code, is_bank, mtext, ev_text)
            _p(f"  模块③：一致{len(m3.get('consistent') or [])} "
               f"矛盾{len(m3.get('contradictions') or [])} "
               f"风险{len(m3.get('risks') or [])} 资金面评分{m3.get('flow_score')}")
            _p("LLM 生成模块④ 四幕僚辩论 …")
            m4 = gen_module4(name, code, is_bank, mtext, m3)
            _p(f"  模块④：四幕僚 + 主席汇总（立场 "
               f"{(m4.get('moderator') or {}).get('stance_word')}）")
            m3_source = "llm"
        except Exception as e:
            _p(f"LLM 模块生成失败，回落规则引擎：{str(e)[:200]}")
            m3, m4 = None, None
            m3_source = "rule"
    else:
        _p("已关闭 LLM，模块③④ 走规则引擎")

    # ---------- 5) 组装评分 ----------
    _stage(5, STAGES[4])
    report, adj, weights, stance, size = assemble(
        code, name, probe, computed, profile, div_yield, m3=m3, m4=m4, evidence=evidence)

    # ---------- 6) 渲染 HTML ----------
    _stage(6, STAGES[5])
    from invest.engine.build_report import render_report
    html_str = render_report(report)
    safe = paths.safe_name(name)
    html_path = paths.report_path(code, safe, DATE, suffix)
    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(html_str)
    _p(f"报告已生成：{os.path.basename(html_path)}（{len(html_str)} bytes）")

    sb = report["verdict"]["score_breakdown"]
    _p(f"综合分 {adj} · 建议 {stance} · 仓位上限 {int(size * 100)}%")

    return {
        "code": code,
        "name": name,
        "date": DATE,
        "html_path": html_path,
        "html_rel": paths.rel_to_root(html_path),
        "html_size": len(html_str),
        "report": report,
        "adj_score": adj,
        "weights": weights,
        "stance": stance,
        "position": size,
        "score_breakdown": dict(sb),
        "base_avg": round(sum(v for _, v in sb) / len(sb), 2) if sb else None,
        "m3_source": m3_source,
        "evidence_used": bool(evidence),
        "close": (computed.get("tech") or {}).get("close"),
        "error": None,
    }


def main():
    """保留 CLI 入口用于本地自测；Web 与批量均直接调 run_single()。"""
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--profile", default=str(paths.CONFIG_DIR / "invest_profile.json"))
    ap.add_argument("--start", default=None)
    ap.add_argument("--div", type=float, default=None, help="每股分红(元)，用于估算股息率")
    ap.add_argument("--no-llm", action="store_true", help="禁用 LLM，模块③④回落规则引擎")
    ap.add_argument("--no-web", action="store_true", help="禁用外部证据采集（LLM 仅凭本地指标分析）")
    ap.add_argument("--no-search", action="store_true", help="证据层跳过搜索引擎（只用结构化接口）")
    ap.add_argument("--fresh", action="store_true", help="忽略证据缓存，强制重新采集")
    ap.add_argument("--suffix", default="", help="输出文件名后缀，避免覆盖已有报告，如 --suffix llm")
    args = ap.parse_args()

    profile = {}
    if args.profile and os.path.exists(args.profile):
        profile = json.load(open(args.profile, encoding="utf-8"))

    def _stage(i, total, label):
        print(f"[{i}/{total}] {label}", file=sys.stderr)

    r = run_single(args.code, args.name, profile=profile, start=args.start, div=args.div,
                   use_llm=not args.no_llm, use_web=not args.no_web,
                   use_search=not args.no_search, fresh=args.fresh,
                   suffix=args.suffix, on_stage=_stage,
                   on_progress=lambda m: print("   " + m, file=sys.stderr))

    print(f"\n=== 完成 {r['code']} {r['name']} ===", file=sys.stderr)
    print(f"  模块③④来源：{'LLM 生成' if r['m3_source'] == 'llm' else '规则引擎回落'}"
          f"{'（含外部证据）' if r['evidence_used'] else ''}", file=sys.stderr)
    print(f"  分项：{'  '.join(f'{k}={v}' for k, v in r['score_breakdown'].items())}", file=sys.stderr)
    print(f"  基础均分={r['base_avg']}", file=sys.stderr)
    print(f"  自适应综合分={r['adj_score']}  权重={ {k: round(v,3) for k,v in r['weights'].items()} }",
          file=sys.stderr)
    print(f"  建议：{r['stance']}  仓位上限 {int(r['position']*100)}%", file=sys.stderr)
    print(f"  HTML：{r['html_path']}", file=sys.stderr)

if __name__ == "__main__":
    main()
