#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""联网证据采集层（模块③的"眼睛"）。

五类证据，优先结构化数据源，通用搜索引擎降为最后兜底：
  A. 机构研报共识  akshare stock_research_report_em  → 机构/评级分布 + 2026~2028 EPS·PE 预测 + 隐含目标价
  B. 估值历史分位  akshare stock_zh_valuation_baidu  → PE(TTM)/PE(静)/PB/PCF 近一年分位数
  C. 个股新闻      akshare stock_news_em             → 最新新闻标题+正文摘要
  D. 资金面代理    股东户数 + 即时资金流              → 筹码集中/分散趋势
  E. 主题检索      东方财富语义检索 → Bing → DDG      → 行业趋势/政策/同业对比等主题证据
  F. 北向资金      沪深港通北向净流入 + 外资增持榜    → 外资机构行为（东方财富）
  G. 硬财务数据    akshare stock_financial_abstract   → 多期归母净利润/营收/ROE/EPS/毛利率时间序列
  H. 利率环境      akshare macro_china_lpr + bond_zh_us_rate(中国国债收益率) → 银行净息差核心驱动
  I. 基金重仓      akshare stock_fund_stock_holder → 机构季度持仓占比（机构行为代理）
                     ⚠️ 两融（个股）：akshare 的 stock_margin_sse/szse 返回的是**市场级汇总**
                     （沪市止于 2023-09 历史序列、深市仅 1 行全市场快照且无日期），并非个股
                     融资余额，无法用于个股分析，故不再采集。机构行为以「基金重仓」代理。

## 检索层为什么不用 Google（2026-09-01 实测结论，勿再重试）

| 引擎 | HTTP | 可解析结果 | 结论 |
|------|------|-----------|------|
| Google（常规/gbv=1） | 200，92KB | **0 条** | 纯 JS 空壳 + 反爬，urllib 拿不到结果；要抓须上无头浏览器，不值得 |
| Bing cn | 200 | 10 条 | 能解析，但面对"600036 净息差"返回官网/百科 —— 通用搜索排序目标是"找网站"不是"找事实" |
| DuckDuckGo | 202，14KB | 0 条 | 反爬空页 |
| **东财 search-api-web** | 200 | **JSON，命中数千条** | ✅ 财经垂类 + BGE 语义重排，首条即当日相关新闻 |

结论：**换通用引擎解决不了问题，要换检索层次** —— 从"通用搜索抓摘要"改为"财经垂类语义检索"。
东财接口自带 `bgeReRankScore` 语义相关性打分，与关键词匹配不是一个维度。

## 东财检索类型分工（实测）
  cmsArticle       语义检索资讯（BGE 重排）→ **主力**，相关性最高，无 url 需拼
  cmsArticleWebOld 关键词检索资讯（带 url）→ 主力，可溯源
  notice           公告/基金中报        → 内含机构对行业的中长期观点，弱相关但视角独特
  researchReport   研报标题            → ⚠️ 关键词会跑偏到同业（搜"招行净息差"返回上海银行研报），
                                         个股研报务必用 akshare 的 fetch_research() 按代码精确取

输出：evidence_{code}.json（6 小时缓存），以及 evidence_to_text() 供 LLM 消费。

用法：
  python web_evidence.py --code 600036 --name 招商银行
  python web_evidence.py --code 600036 --name 招商银行 --engine-test   # 只测检索层
  from web_evidence import collect_evidence, evidence_to_text
"""
import os, sys, json, re, time, html as _html
import urllib.request, urllib.parse, ssl
import warnings
warnings.filterwarnings("ignore")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from invest import paths

HERE = os.path.dirname(os.path.abspath(__file__))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
_TAG = re.compile(r"<[^>]+>")

CACHE_TTL = 6 * 3600


def _clean(s):
    s = _TAG.sub("", s or "")
    s = _html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
        return r.read().decode("utf-8", "replace")


# ============================================================
# E. 主题检索层：东财语义检索（主）→ Bing → DDG（兜底）
# ============================================================
EM_SEARCH_API = "https://search-api-web.eastmoney.com/search/jsonp"

# 类型 → (中文名, 置信档, 是否自带 url)
EM_KINDS = {
    "cmsArticle":       ("财经资讯·语义检索", "A", False),
    "cmsArticleWebOld": ("财经资讯·关键词",   "A", True),
    "notice":           ("公告/基金报告",     "B", False),
    "researchReport":   ("研报标题(可能含同业)", "B", False),
}


def _em_url(kind, code):
    """按类型拼东财详情页 URL。"""
    if not code:
        return ""
    if kind in ("cmsArticle", "cmsArticleWebOld"):
        return f"http://finance.eastmoney.com/a/{code}.html"
    if kind == "researchReport":
        return f"https://data.eastmoney.com/report/info/{code}.html"
    if kind == "notice":
        return f"https://data.eastmoney.com/notices/detail/{code}.html"
    return ""


def search_eastmoney(query, topk=6, kinds=("cmsArticle", "cmsArticleWebOld"),
                     timeout=20):
    """东方财富统一检索（JSONP，返回 JSON）。

    cmsArticle 走 BGE 语义重排，对"净息差趋势""产能过剩"这类概念性提问
    远优于通用搜索引擎的关键词匹配。
    """
    kinds = [k for k in kinds if k in EM_KINDS]
    if not kinds:
        return []
    param = {
        "uid": "", "keyword": query, "type": list(kinds),
        "client": "web", "clientType": "web", "clientVersion": "curr",
        "param": {k: {"searchScope": "default", "sort": "default",
                      "pageIndex": 1, "pageSize": max(3, topk),
                      "preTag": "", "postTag": ""} for k in kinds},
    }
    url = EM_SEARCH_API + "?cb=cb&param=" + urllib.parse.quote(
        json.dumps(param, ensure_ascii=False))
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://so.eastmoney.com/",
    })
    with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
        body = r.read().decode("utf-8", "replace")
    m = re.match(r"^\s*cb\((.*)\)\s*;?\s*$", body, re.S)
    if not m:
        return []
    data = json.loads(m.group(1))
    if data.get("code") != 0:
        return []

    out, seen = [], set()
    result = data.get("result") or {}
    for kind in kinds:
        label, tier, has_url = EM_KINDS[kind]
        for it in (result.get(kind) or []):
            title = _clean(it.get("title") or it.get("securityShortName") or "")
            snippet = _clean(it.get("content") or "")
            if not title:
                continue
            key = re.sub(r"\W+", "", title)[:40]
            if key in seen:            # 语义/关键词两路会重叠，按标题去重
                seen.add(key)
                continue
            seen.add(key)
            src = (it.get("mediaName") or it.get("source")
                   or it.get("securityShortName") or "东方财富")
            out.append({
                "title": title,
                "snippet": snippet,
                "url": it.get("url") or _em_url(kind, it.get("code")),
                "date": (it.get("date") or "")[:16],
                "source": src,
                "engine": "eastmoney",
                "kind": label,
                "tier": tier,
                "stock": it.get("stockName") or "",
            })
    # 语义相关但无正文的条目排后面
    out.sort(key=lambda x: (0 if len(x["snippet"]) > 20 else 1, x["tier"]))
    return out[:topk]


_B_LINK = re.compile(r'<h2[^>]*>\s*<a[^>]+href="(http[^"]+)"[^>]*>(.*?)</a>', re.S)
_B_SNIP = re.compile(r'<p class="b_lineclamp\d*"[^>]*>(.*?)</p>', re.S)
_B_SNIP2 = re.compile(r'<div class="b_caption".*?<p[^>]*>(.*?)</p>', re.S)


def search_bing(query, topk=6):
    doc = _fetch("https://cn.bing.com/search?q=" + urllib.parse.quote(query) + "&ensearch=0")
    out = []
    for blk in re.split(r'<li class="b_algo"', doc)[1:]:
        lk = _B_LINK.search(blk)
        if not lk:
            continue
        href, title = lk.group(1), _clean(lk.group(2))
        sn = _B_SNIP.search(blk) or _B_SNIP2.search(blk)
        snippet = _clean(sn.group(1)) if sn else ""
        snippet = re.sub(r"^(网页|Web)\s*", "", snippet)
        snippet = re.sub(r"^\d+\s*(天|小时|分钟|周|个月)前\s*·?\s*", "", snippet)
        if title and len(snippet) > 10:
            out.append({"title": title, "snippet": snippet, "url": href, "engine": "bing"})
        if len(out) >= topk:
            break
    return out


_D_LINK = re.compile(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_D_SNIP = re.compile(r'class="result__snippet"[^>]*>(.*?)</a>', re.S)


def search_ddg(query, topk=6):
    doc = _fetch("https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query))
    out = []
    for blk in re.split(r'class="result__body"', doc)[1:]:
        lk = _D_LINK.search(blk)
        if not lk:
            continue
        href, title = lk.group(1), _clean(lk.group(2))
        if "uddg=" in href:
            m = re.search(r"uddg=([^&]+)", href)
            if m:
                href = urllib.parse.unquote(m.group(1))
        sn = _D_SNIP.search(blk)
        snippet = _clean(sn.group(1)) if sn else ""
        if title and len(snippet) > 10:
            out.append({"title": title, "snippet": snippet, "url": href, "engine": "ddg"})
        if len(out) >= topk:
            break
    return out


def search(query, topk=6, verbose=True, kinds=None):
    """三级降级检索：东财语义（财经垂类，高置信）→ Bing → DDG（通用，低置信）。

    注：Google 已实测不可解析（JS 空壳 + 反爬），不纳入链路，详见模块头注释。
    """
    def _em(q, k):
        return search_eastmoney(q, k, kinds=kinds or ("cmsArticle", "cmsArticleWebOld"))

    chain = (("东财语义检索", _em, "A"),
             ("bing", lambda q, k: search_bing(q, k), "C"),
             ("ddg", lambda q, k: search_ddg(q, k), "C"))

    for name, fn, tier in chain:
        try:
            rs = fn(query, topk)
            if rs:
                if verbose:
                    print(f"[web] {name} '{query[:26]}…' → {len(rs)} 条", file=sys.stderr)
                for r in rs:
                    r["query"] = query
                    r.setdefault("tier", tier)
                    r.setdefault("date", "")
                    r.setdefault("kind", "网页")
                    try:
                        r["host"] = urllib.parse.urlparse(r["url"]).netloc
                    except Exception:
                        r["host"] = ""
                    r.setdefault("source", r.get("host") or "")
                return rs
            if verbose:
                print(f"[web] {name} 无结果，降级 …", file=sys.stderr)
        except Exception as e:
            if verbose:
                print(f"[web] {name} 失败({type(e).__name__})，降级 …", file=sys.stderr)
    return []


# ============================================================
# A. 机构研报共识
# ============================================================
def fetch_research(code, months=6, close=None, verbose=True):
    """近 N 月研报：机构/评级分布 + 盈利预测均值 + 隐含目标价（按预测PE反推）。"""
    try:
        import akshare as ak, pandas as pd
        d = ak.stock_research_report_em(symbol=code)
        if d is None or not len(d):
            return {}
        d["日期"] = pd.to_datetime(d["日期"], errors="coerce")
        cutoff = pd.Timestamp.today() - pd.DateOffset(months=months)
        r = d[d["日期"] >= cutoff].copy()
        # 近 N 月无研报则放宽到最近 12 条
        widened = False
        if not len(r):
            r = d.sort_values("日期", ascending=False).head(12).copy()
            widened = True
        ratings = r["东财评级"].dropna().value_counts().to_dict() if "东财评级" in r else {}
        insts = r["机构"].dropna().unique().tolist() if "机构" in r else []
        # 盈利预测（取最近年份列）
        fc = {}
        for col in r.columns:
            m = re.match(r"(\d{4})-盈利预测-(收益|市盈率)", str(col))
            if not m:
                continue
            s = pd.to_numeric(r[col], errors="coerce").dropna()
            if len(s):
                fc.setdefault(m.group(1), {})[m.group(2)] = round(float(s.mean()), 2)
        items = []
        for _, row in r.sort_values("日期", ascending=False).head(10).iterrows():
            items.append({
                "date": str(row["日期"].date()) if pd.notna(row["日期"]) else "",
                "inst": str(row.get("机构", "")),
                "rating": str(row.get("东财评级", "")),
                "title": str(row.get("报告名称", "")),
            })
        out = {
            "window": (f"最近 {len(r)} 篇（近{months}月无新研报，已放宽）" if widened else f"近{months}个月"),
            "count": int(len(r)),
            "ratings": ratings,
            "institutions": insts[:20],
            "forecast": fc,
            "recent": items,
        }
        # 隐含目标价：用最近年份 EPS 均值 × 当前 PE 或预测 PE
        try:
            yrs = sorted(fc.keys())
            if yrs and close:
                y = yrs[0]
                eps = fc[y].get("收益")
                pe_f = fc[y].get("市盈率")
                if eps and pe_f:
                    out["implied"] = {
                        "year": y, "eps_avg": eps, "pe_forecast": pe_f,
                        "note": f"机构 {y} 年 EPS 一致预期 {eps} 元、对应预测 PE {pe_f}x（按报告发布时股价）",
                        "fair_at_current_pe": round(eps * (close / eps) if eps else 0, 2),
                    }
        except Exception:
            pass
        if verbose:
            print(f"[ev] 研报 {out['count']} 篇（{out['window']}）评级={ratings}", file=sys.stderr)
        return out
    except Exception as e:
        if verbose:
            print(f"[ev] 研报获取失败: {type(e).__name__} {str(e)[:70]}", file=sys.stderr)
        return {}


# ============================================================
# B. 估值历史分位
# ============================================================
VAL_INDICATORS = ["市盈率(TTM)", "市盈率(静)", "市净率", "市现率"]


def fetch_valuation_percentile(code, period="近一年", verbose=True):
    out = {}
    try:
        import akshare as ak
    except Exception:
        return out
    for ind in VAL_INDICATORS:
        try:
            d = ak.stock_zh_valuation_baidu(symbol=code, indicator=ind, period=period)
            if d is None or not len(d):
                continue
            s = d["value"].dropna()
            if not len(s):
                continue
            cur = float(s.iloc[-1])
            pct = float((s <= cur).mean() * 100)
            out[ind] = {
                "current": round(cur, 2),
                "percentile": round(pct, 1),
                "min": round(float(s.min()), 2),
                "max": round(float(s.max()), 2),
                "median": round(float(s.median()), 2),
                "n": int(len(s)), "period": period,
            }
        except Exception:
            continue
    if verbose and out:
        brief = {k: f"{v['current']}({v['percentile']}%)" for k, v in out.items()}
        print(f"[ev] 估值分位 {brief}", file=sys.stderr)
    return out


# ============================================================
# C. 个股新闻
# ============================================================
def fetch_news(code, topk=10, body_chars=380, verbose=True):
    try:
        import akshare as ak
        d = ak.stock_news_em(symbol=code)
        if d is None or not len(d):
            return []
        out = []
        for _, r in d.head(topk).iterrows():
            out.append({
                "title": str(r.get("新闻标题", ""))[:120],
                "time": str(r.get("发布时间", "")),
                "source": str(r.get("文章来源", "")),
                "body": _clean(str(r.get("新闻内容", "")))[:body_chars],
                "url": str(r.get("新闻链接", "")),
            })
        if verbose:
            print(f"[ev] 新闻 {len(out)} 条", file=sys.stderr)
        return out
    except Exception as e:
        if verbose:
            print(f"[ev] 新闻获取失败: {type(e).__name__} {str(e)[:60]}", file=sys.stderr)
        return []


# ============================================================
# E. 资金面代理：股东户数（筹码集中度）+ 即时资金流
# ============================================================
def fetch_holder_count(code, periods=5, verbose=True):
    """股东户数近 N 期变化：户数减少=筹码集中(机构吸筹)，增加=散户化。"""
    try:
        import akshare as ak
        d = ak.stock_zh_a_gdhs_detail_em(symbol=code)
        if d is None or not len(d):
            return {}
        d = d.sort_values("股东户数统计截止日").tail(periods)
        rows = []
        for _, r in d.iterrows():
            rows.append({
                "date": str(r.get("股东户数统计截止日")),
                "holders": int(r.get("股东户数-本次") or 0),
                "chg_pct": round(float(r.get("股东户数-增减比例") or 0), 2),
                "avg_hold_value": round(float(r.get("户均持股市值") or 0) / 1e4, 1),  # 万元
            })
        last = rows[-1] if rows else {}
        trend = "筹码集中（户数下降）" if (last.get("chg_pct") or 0) < 0 else "筹码分散（户数上升）"
        if verbose:
            print(f"[ev] 股东户数 {last.get('date')} {last.get('holders')} 户 "
                  f"({last.get('chg_pct')}%) → {trend}", file=sys.stderr)
        return {"series": rows, "latest": last, "trend": trend}
    except Exception as e:
        if verbose:
            print(f"[ev] 股东户数获取失败: {type(e).__name__} {str(e)[:60]}", file=sys.stderr)
        return {}


def fetch_fund_flow(code, verbose=True):
    """从全市场即时资金流排行中筛出目标股（不在榜则为空）。"""
    try:
        import akshare as ak
        d = ak.stock_fund_flow_individual(symbol="即时")
        if d is None or not len(d):
            return {}
        d["股票代码"] = d["股票代码"].astype(str).str.zfill(6)
        r = d[d["股票代码"] == str(code).zfill(6)]
        if not len(r):
            return {}
        row = r.head(1).to_dict("records")[0]
        out = {
            "price": row.get("最新价"), "chg": row.get("涨跌幅"),
            "turnover": row.get("换手率"), "inflow": row.get("流入资金"),
            "outflow": row.get("流出资金"), "net": row.get("净额"),
            "rank": int(row.get("序号") or 0), "total": int(len(d)),
        }
        if verbose:
            print(f"[ev] 即时资金流 净额={out['net']} 排名={out['rank']}/{out['total']}", file=sys.stderr)
        return out
    except Exception as e:
        if verbose:
            print(f"[ev] 资金流获取失败: {type(e).__name__} {str(e)[:60]}", file=sys.stderr)
        return {}


def _ak_symbol(code):
    """把 6 位代码转成 akshare 需要的 SH/SZ 前缀。"""
    c = str(code)
    if c.startswith(("6", "9")):
        return "SH" + c
    if c.startswith(("0", "2", "3")):
        return "SZ" + c
    return c


# ============================================================
# F. 北向资金（沪深港通）
# ============================================================
def fetch_northbound(code, verbose=True):
    """北向资金：沪股通/深股通 北向当日净流入（市场级权威信号）+ 个股是否在增持排行榜。"""
    out = {}
    try:
        import akshare as ak
        # 1) 市场级北向净流入
        try:
            df = ak.stock_hsgt_fund_flow_summary_em()
            nb = df[df.get("资金方向") == "北向"] if "资金方向" in df else df
            rows = []
            for _, r in nb.iterrows():
                try:
                    rows.append({
                        "board": str(r.get("板块", "")),
                        "net_inflow": float(r.get("资金净流入") or 0),
                        "net_buy": float(r.get("成交净买额") or 0),
                        "date": str(r.get("交易日", "")),
                    })
                except Exception:
                    continue
            if rows:
                out["flow"] = rows
        except Exception as e:
            if verbose:
                print(f"[ev] 北向净流入获取失败: {type(e).__name__}", file=sys.stderr)
        # 2) 个股是否在外资增持榜
        try:
            mkt = "沪股通" if str(code).startswith(("6", "9")) else "深股通"
            rk = ak.stock_hsgt_hold_stock_em(market=mkt, indicator="1个月排行")
            if rk is not None and len(rk):
                mask = rk.astype(str).apply(
                    lambda c: c.str.contains(str(code).zfill(6), na=False)).any(axis=1)
                hit = rk[mask]
                if len(hit):
                    out["in_ranking"] = hit.head(3).to_dict("records")
                    out["rank_market"] = mkt
        except Exception:
            pass
        if verbose and out:
            fl = out.get("flow") or []
            s = "，".join(f"{x['board']}净流入{x['net_inflow']:.0f}亿" for x in fl)
            print(f"[ev] 北向资金 {s}", file=sys.stderr)
        return out
    except Exception as e:
        if verbose:
            print(f"[ev] 北向资金整体失败: {type(e).__name__}", file=sys.stderr)
        return out


# ============================================================
# G. 硬财务数据（财务摘要时间序列）
# ============================================================
_FIN_NAME = {
    "归母净利润": "归母净利润",
    "营业总收入": "营业收入", "营业收入": "营业收入",
    "净资产收益率": "ROE",
    "每股收益": "EPS", "基本每股收益": "EPS",
    "销售毛利率": "销售毛利率", "毛利率": "销售毛利率",
    "资产负债率": "资产负债率",
}
_FIN_UNIT = {
    "归母净利润": ("亿元", 1e-8), "营业收入": ("亿元", 1e-8),
    "ROE": ("%", 1.0), "EPS": ("元", 1.0),
    "销售毛利率": ("%", 1.0), "资产负债率": ("%", 1.0),
}


def fetch_financials(symbol_ak, verbose=True):
    """硬财务数据：多期 归母净利润/营业收入/ROE/EPS/毛利率，含同比。"""
    try:
        import akshare as ak
        df = ak.stock_financial_abstract(symbol=symbol_ak)
        if df is None or not len(df):
            return {}
        date_cols = sorted(c for c in df.columns if re.match(r"^\d{8}$", str(c)))
        if not date_cols:
            return {}
        latest = date_cols[-1]
        ly = None
        for c in date_cols:
            if str(c)[:4] == str(int(str(latest)[:4]) - 1) and str(c)[4:] == str(latest)[4:]:
                ly = c
                break
        items = {}
        for _, row in df.iterrows():
            key = _FIN_NAME.get(str(row.get("指标", "")))
            if not key:
                continue
            try:
                cur = float(row.get(latest))
            except Exception:
                cur = None
            prev = row.get(ly) if ly else None
            try:
                prev = float(prev)
            except Exception:
                prev = None
            yoy = None
            if cur is not None and prev not in (None, 0):
                yoy = round((cur / prev - 1) * 100, 1)
            unit, scale = _FIN_UNIT[key]
            items[key] = {
                "latest": round(cur * scale, 2) if cur is not None else None,
                "yoy_pct": yoy, "unit": unit,
            }
        out = {"latest_period": latest, "yoy_period": ly, "items": items}
        if verbose and items:
            print(f"[ev] 财务摘要 {latest}：" + "，".join(
                f"{k}={v['latest']}{v['unit']}" for k, v in items.items()), file=sys.stderr)
        return out
    except Exception as e:
        if verbose:
            print(f"[ev] 财务摘要获取失败: {type(e).__name__} {str(e)[:60]}", file=sys.stderr)
        return {}


# ============================================================
# H. 利率环境（银行净息差核心驱动）
# ============================================================
def fetch_rates(verbose=True):
    """利率环境：LPR(1Y/5Y) + 中国国债收益率(2/5/10/30年) + 10Y-2Y 利差(曲线斜率)。"""
    out = {}
    try:
        import akshare as ak, pandas as pd
        try:
            lp = ak.macro_china_lpr()
            lp = lp.dropna(subset=["LPR1Y", "LPR5Y"], how="any")
            if len(lp):
                last = lp.iloc[-1]
                out["lpr"] = {"date": str(last.get("TRADE_DATE")),
                              "lpr_1y": float(last.get("LPR1Y")),
                              "lpr_5y": float(last.get("LPR5Y"))}
        except Exception as e:
            if verbose:
                print(f"[ev] LPR 获取失败: {type(e).__name__}", file=sys.stderr)
        try:
            b = ak.bond_zh_us_rate()
            cols = [c for c in b.columns if str(c).startswith("中国国债收益率")]
            if cols:
                bb = b.dropna(subset=cols, how="all")
                if len(bb):
                    last = bb.iloc[-1]
                    out["cgb"] = {str(c).replace("中国国债收益率", ""): round(float(last[c]), 3)
                                  for c in cols if pd.notna(last[c])}
        except Exception as e:
            if verbose:
                print(f"[ev] 国债收益率获取失败: {type(e).__name__}", file=sys.stderr)
        if verbose and out:
            print(f"[ev] 利率 LPR1Y={out.get('lpr', {}).get('lpr_1y')} "
                  f"10Y国债={out.get('cgb', {}).get('10年')}", file=sys.stderr)
        return out
    except Exception as e:
        if verbose:
            print(f"[ev] 利率整体失败: {type(e).__name__}", file=sys.stderr)
        return out


# ============================================================
# I. 两融余额与基金重仓（机构行为）
# ============================================================
def fetch_margin_funds(code, verbose=True):
    """基金重仓（机构季度持仓）—— 两融个股数据 akshare 暂不可得。

    说明：akshare 的 stock_margin_sse/szse 返回的是**市场级汇总**
    （沪市为 2023 年历史序列，深市仅 1 行全市场快照且无日期），
    并非个股融资余额，无法用于个股分析，故不再采集。
    机构行为以「基金重仓持仓占比」代理。
    """
    out = {}
    try:
        import akshare as ak
        try:
            fd = ak.stock_fund_stock_holder(symbol=code)
            if fd is not None and len(fd):
                funds, tot = [], 0.0
                for _, r in fd.head(15).iterrows():
                    pct = float(r.get("占流通股比例") or 0)
                    tot += pct
                    funds.append({"name": str(r.get("基金名称", "")), "pct": pct,
                                  "value": float(r.get("持股市值") or 0),
                                  "date": str(r.get("截止日期", ""))})
                out["funds"] = {"count": int(len(fd)), "top_pct_sum": round(tot, 2),
                               "top": sorted(funds, key=lambda x: -x["pct"])[:8]}
        except Exception as e:
            if verbose:
                print(f"[ev] 基金重仓获取失败: {type(e).__name__}", file=sys.stderr)
        if verbose and out.get("funds"):
            f = out["funds"]
            print(f"[ev] 基金重仓 {f['count']} 只（两融个股数据暂不可得，以基金重仓代理）",
                  file=sys.stderr)
        return out
    except Exception as e:
        if verbose:
            print(f"[ev] 基金重仓整体失败: {type(e).__name__}", file=sys.stderr)
        return out


# ============================================================
# 汇总
# ============================================================
_SEM = ("cmsArticle", "cmsArticleWebOld")
_SEM_N = ("cmsArticle", "cmsArticleWebOld", "notice")


def build_queries(name, code, is_bank, ev=None):
    """构造主题检索查询集。

    要点：写成**概念性提问**而非"公司名 + 代码"。语义检索对
    "净息差何时企稳""产能过剩何时出清"这类问法命中率远高于关键词堆砌；
    反过来，"招商银行 600036" 这种品牌导航型查询在任何通用引擎都只会返回官网。

    最后一路「风险与争议」是刻意设计的**反证检索** —— 直接服务模块③
    "至少 2 条矛盾信号"的硬约束，避免 LLM 只顺着多头叙事写。
    """
    ev = ev or {}
    qs = []

    if is_bank:
        qs += [
            ("业绩与经营质量", f"{name} 净息差 中收 资产质量 不良率 拨备 最新表态", _SEM),
            ("行业趋势与政策", "银行业 净息差 企稳 存款利率下调 息差拐点 2026 上半年", _SEM_N),
            ("同业对比", "上市银行 2026 中报 净息差 分化 零售 不良 对比 股份行", _SEM),
        ]
    else:
        qs += [
            ("业绩与经营质量", f"{name} 营收 净利润 毛利率 增速 最新 业绩说明会", _SEM),
            ("行业趋势与政策", f"{name} 所在行业 需求 供给 价格 产能 竞争格局 2026", _SEM_N),
            ("同业对比", f"{name} 竞争对手 市占率 对比 同行 龙头", _SEM),
        ]

    # 资金面无可靠结构化源（东财 push2his 被沙箱代理封），必检
    qs.append(("资金与机构观点", f"{name} 机构持仓 北向资金 增持 减持 基金 重仓 2026", _SEM))
    # 反证检索：强制找空头视角
    qs.append(("风险与争议(反证)", f"{name} 风险 隐忧 质疑 减持 下滑 承压 估值过高 看空", _SEM_N))

    # 结构化研报缺失时补一路
    if not (ev.get("research") or {}):
        qs.append(("研报与目标价", f"{name} 研报 评级 目标价 上调 下调 2026", _SEM))
    return qs


def collect_evidence(code, name, is_bank=False, close=None, use_cache=True,
                     with_search=True, verbose=True, on_progress=None):
    """采集外部证据。on_progress(msg) 用于把阶段性进度回传给调用方（Web 任务日志）。"""
    def _p(msg):
        if on_progress:
            try:
                on_progress(msg)
            except Exception:
                pass

    # 缓存改落 invest/data/evidence/，不再污染引擎代码目录
    paths.ensure_dirs()
    cache = paths.evidence_path(code)
    if use_cache and os.path.exists(cache):
        age = time.time() - os.path.getmtime(cache)
        if age < CACHE_TTL:
            try:
                d = json.load(open(cache, encoding="utf-8"))
                if verbose:
                    print(f"[ev] 命中缓存 evidence_{code}.json（{age/60:.0f} 分钟前）", file=sys.stderr)
                _p(f"证据命中缓存（{age/60:.0f} 分钟前），跳过联网采集")
                return d
            except Exception:
                pass

    ev = {"code": code, "name": name, "ts": time.time()}
    # 每一步都上报进度，长任务下前端能看到卡在哪一环
    steps = [
        ("research",   "机构研报共识",   lambda: fetch_research(code, close=close, verbose=verbose)),
        ("valuation",  "估值历史分位",   lambda: fetch_valuation_percentile(code, verbose=verbose)),
        ("news",       "个股新闻",       lambda: fetch_news(code, verbose=verbose)),
        ("holders",    "股东户数",       lambda: fetch_holder_count(code, verbose=verbose)),
        ("fund_flow",  "即时资金流",     lambda: fetch_fund_flow(code, verbose=verbose)),
        ("northbound", "北向资金",       lambda: fetch_northbound(code, verbose=verbose)),
        ("financials", "硬财务数据",     lambda: fetch_financials(_ak_symbol(code), verbose=verbose)),
        ("rates",      "利率环境",       lambda: fetch_rates(verbose=verbose)),
        ("margin_funds", "两融余额",     lambda: fetch_margin_funds(code, verbose=verbose)),
    ]
    for i, (key, label, fn) in enumerate(steps, 1):
        _p(f"采集证据 {i}/{len(steps)}：{label}")
        try:
            ev[key] = fn()
        except Exception as e:
            ev[key] = None
            if verbose:
                print(f"[ev] {key} 采集失败: {e}", file=sys.stderr)

    # E. 主题检索：东财语义检索质量足够高，改为主动做「概念主题」取证，
    #    而非旧版的品牌导航型查询（那种查询在通用引擎只会返回官网/百科）。
    ev["search"] = {}
    if with_search:
        queries = build_queries(name, code, is_bank, ev)
        for i, (dim, q, kinds) in enumerate(queries, 1):
            _p(f"语义检索 {i}/{len(queries)}：{dim}")
            ev["search"][dim] = search(q, topk=6, verbose=verbose, kinds=kinds)
            time.sleep(0.6)

    try:
        json.dump(ev, open(cache, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        _p(f"证据已缓存：{os.path.basename(cache)}")
    except Exception:
        pass
    return ev


def evidence_to_text(ev, max_news=8):
    """压成 LLM 可读文本。"""
    L = []
    r = ev.get("research") or {}
    if r:
        L.append(f"## A. 机构研报共识（{r.get('window')}，{r.get('count')} 篇）")
        L.append(f"- 评级分布：{r.get('ratings')}")
        L.append(f"- 覆盖机构：{'、'.join((r.get('institutions') or [])[:12])}")
        fc = r.get("forecast") or {}
        for y in sorted(fc):
            L.append(f"- {y} 年一致预期：EPS {fc[y].get('收益')} 元，预测 PE {fc[y].get('市盈率')}x")
        for it in (r.get("recent") or [])[:6]:
            L.append(f"- [{it['date']}] {it['inst']} · {it['rating']} · {it['title']}")
    else:
        L.append("## A. 机构研报共识\n- （未取到结构化研报数据）")

    v = ev.get("valuation") or {}
    if v:
        L.append("\n## B. 估值历史分位（近一年）")
        for k, d in v.items():
            L.append(f"- {k}：当前 {d['current']}，处近一年 {d['percentile']}% 分位"
                     f"（区间 {d['min']}~{d['max']}，中位 {d['median']}）")
    else:
        L.append("\n## B. 估值历史分位\n- （未取到）")

    ns = ev.get("news") or []
    if ns:
        L.append(f"\n## C. 个股新闻（最新 {min(len(ns), max_news)} 条）")
        for n in ns[:max_news]:
            L.append(f"- [{n['time']}] {n['title']}（{n['source']}）\n    {n['body'][:260]}")

    h = ev.get("holders") or {}
    if h:
        L.append("\n## D. 资金面代理（股东户数 / 筹码集中度）")
        for r in (h.get("series") or []):
            L.append(f"- {r['date']}：{r['holders']:,} 户（{r['chg_pct']:+.2f}%），户均持股市值 {r['avg_hold_value']} 万元")
        L.append(f"- 最新趋势判定：{h.get('trend')}")
    ff = ev.get("fund_flow") or {}
    if ff:
        L.append(f"- 即时资金流：净额 {ff.get('net')}（流入 {ff.get('inflow')} / 流出 {ff.get('outflow')}），"
                 f"换手率 {ff.get('turnover')}，全市场资金榜排名 {ff.get('rank')}/{ff.get('total')}")

    # F. 北向资金
    nb = ev.get("northbound") or {}
    if nb:
        L.append("\n## F. 北向资金（沪深港通，东方财富）")
        for x in (nb.get("flow") or []):
            if x["net_inflow"] == 0 and x["net_buy"] == 0:
                L.append(f"- {x['board']} 北向资金当日净流入：数据未更新（最新交易日 {x['date']}，"
                         f"通常盘后 1~2 小时发布，盘中/休市显示为 0）")
            else:
                L.append(f"- {x['board']} 北向资金净流入：{x['net_inflow']:.0f} 亿元"
                         f"（成交净买额 {x['net_buy']:.0f} 亿元，交易日 {x['date']}）")
        if nb.get("in_ranking"):
            L.append(f"- 个股位列 {nb.get('rank_market')} 外资增持排行榜"
                     f"（前 {len(nb['in_ranking'])} 条命中，外资主动加仓信号）")

    # G. 硬财务数据
    fin = ev.get("financials") or {}
    if fin.get("items"):
        yoy = f"，同比期 {fin.get('yoy_period')}" if fin.get("yoy_period") else ""
        L.append(f"\n## G. 硬财务数据（财务摘要，最新期 {fin.get('latest_period')}{yoy}）")
        for k, v in fin["items"].items():
            if v["latest"] is None:
                continue
            line = f"- {k}：{v['latest']} {v['unit']}"
            if v.get("yoy_pct") is not None:
                line += f"（同比 {v['yoy_pct']:+.1f}%）"
            L.append(line)
        L.append("- （注：银行专项指标—不良率/拨备覆盖率/NIM—以公告原文与新闻为准，本层取硬财务骨架）")

    # H. 利率环境
    rt = ev.get("rates") or {}
    if rt:
        L.append("\n## H. 利率环境（净息差核心驱动）")
        if rt.get("lpr"):
            l = rt["lpr"]
            L.append(f"- LPR：1 年期 {l['lpr_1y']}%，5 年期以上 {l['lpr_5y']}%（{l['date']}）")
        if rt.get("cgb"):
            c = rt["cgb"]
            L.append("- 中国国债收益率：" + "，".join(f"{k} {v}%" for k, v in c.items()))
            if "10年" in c and "2年" in c and c["10年"] and c["2年"]:
                slope = round(c["10年"] - c["2年"], 2)
                L.append(f"- 收益率曲线斜率（10Y-2Y）：{slope}%"
                         f"（{'陡峭化，利于银行净息差修复' if slope > 0 else '平坦/倒挂，压制净息差'}）")

    # I. 基金重仓（机构行为代理）
    mf = ev.get("margin_funds") or {}
    f = mf.get("funds")
    if f:
        L.append("\n## I. 基金重仓（机构行为代理）")
        L.append(f"- 共 {f['count']} 只基金持有，Top{len(f['top'])} 合计占流通股 {f['top_pct_sum']}%")
        for x in f["top"][:5]:
            L.append(f"    · {x['name']} 占流通股 {x['pct']}%，持股市值 {x['value']/1e8:.2f} 亿元（{x['date']}）")
        L.append("- 注：两融个股数据 akshare 暂不可得（仅提供市场级汇总且过期），机构行为以基金重仓代理。")

    sr = ev.get("search") or {}
    if any(sr.values()):
        eng = {it.get("engine") for items in sr.values() for it in (items or [])}
        if "eastmoney" in eng:
            L.append("\n## E. 主题检索证据（东方财富财经语义检索，中高置信）")
            L.append("> 置信档 A=财经媒体正文/资讯，B=公告或可能含同业的研报标题；"
                     "引用时请连同日期与来源一起标注。")
        else:
            L.append("\n## E. 主题检索证据（通用搜索引擎兜底，低置信，仅供线索）")
        for dim, items in sr.items():
            if not items:
                continue
            L.append(f"\n### {dim}")
            for it in items[:5]:
                tier = it.get("tier", "C")
                date = it.get("date") or "日期不详"
                src = it.get("source") or it.get("host") or ""
                kind = it.get("kind", "")
                stock = it.get("stock")
                tag = f"[{tier}]"
                head = f"- {tag} [{date}] {it['title']}"
                if stock and stock not in it["title"]:
                    head += f"（标的：{stock}）"
                head += f" —— {src}"
                if kind and kind != "网页":
                    head += f" · {kind}"
                L.append(head)
                if it.get("snippet"):
                    L.append(f"    {it['snippet'][:240]}")
    return "\n".join(L)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--bank", action="store_true")
    ap.add_argument("--close", type=float, default=None)
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--no-search", action="store_true")
    ap.add_argument("--engine-test", action="store_true",
                    help="只做检索层三引擎对比，不取结构化数据")
    a = ap.parse_args()

    if a.engine_test:
        q = f"{a.name} 净息差 资产质量 风险" if a.bank else f"{a.name} 业绩 增速 风险"
        for label, fn in (("东财语义(cmsArticle)", lambda: search_eastmoney(q, 5)),
                          ("Bing", lambda: search_bing(q, 5)),
                          ("DuckDuckGo", lambda: search_ddg(q, 5))):
            print("=" * 68)
            print(f"{label}   query = {q}")
            print("=" * 68)
            try:
                rs = fn()
                if not rs:
                    print("  (0 条)")
                for r in rs:
                    d = r.get("date") or ""
                    print(f"  · [{r.get('tier','C')}]{(' ' + d) if d else ''} {r['title'][:58]}")
                    print(f"    来源 {r.get('source') or r.get('url','')[:50]}")
                    if r.get("snippet"):
                        print(f"    {r['snippet'][:150]}")
            except Exception as e:
                print(f"  失败 {type(e).__name__}: {e}")
            print()
        sys.exit(0)

    ev = collect_evidence(a.code, a.name, a.bank, close=a.close,
                          use_cache=not a.no_cache, with_search=not a.no_search)
    print(evidence_to_text(ev))
