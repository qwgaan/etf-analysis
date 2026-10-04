"""
指数估值分位模块(ETF 专用:底层跟踪指数的 PE 分位 + 当前股息率)

为什么需要这个模块:
- ETF 本身没有 PE/PB —— 基金是一篮子股票的打包产品,基金层面不存在市盈率。
- 要看 ETF 的估值,只能看**它跟踪的指数**。很多行情软件在 ETF 页面直接显示 PE,
  那个数值其实是底层指数的 PE,不是 ETF 的(容易混淆,展示时必须标清"跟踪指数")。
- 现金类 ETF(货币基金,如 511990)底层是存款/短债/同业存单,**没有股票也就没有 PE**;
  商品类 ETF(黄金,如 518880)是一篮子实物,**同样没有 PE**。这两类不会出现在映射表里 -> 返回 None。
- 境外指数 ETF(纳斯达克100、标普500 等)不在中证指数公司覆盖范围内 -> 返回 None。

数据源(均为中证指数官网 csindex):
- PE 历史: https://www.csindex.com.cn/csindex-home/perf/index-perf (JSON)
  **必须带 User-Agent / Referer** —— akshare 内部用裸 requests 调该接口,实测会被 WAF 判 403;
  且该接口对**短时间高频调用会临时封 IP(实测约 6 分钟)**,因此本模块:
    ① 所有请求串行 + 最小间隔节流(见 _MIN_INTERVAL);② 结果按自然日缓存,单只每天最多 1 次;
    ③ 遇到 403/非 JSON 一律降级为 None,绝不重试轰炸;④ 失败进 10 分钟负缓存(见 _FAIL_TTL),
    避免「被风控 -> 每个请求都重试 -> 调用量继续累加 -> 封得更久」的自我延续循环。
- 当前股息率: {index_code}indicator.xls (静态 OSS 文件,**不受上面限流影响**)
  该文件只有最近约 20 个交易日 -> **只能取「当前股息率」,拿不到股息率的历史分位**。

映射表(ETF -> 跟踪指数):
- 内置 `INDEX_MAP` 是出厂默认(仅覆盖中证官网能取到数据的 A 股指数 ETF)。
- **用户可改**:自定义内容存在 `config/index_map.json`,可覆盖内置、给内置没有的 ETF
  手工指定、或把某只显式设为「不用指数」。见 `effective_map()`。
- ETF 本身没有 PE/PB,所以「修改/指定」在这里只影响估值口径,不影响行情与信号。

窗口: 近十年(与个股估值口径一致,见 valuation.VALUATION_WINDOW_YEARS)。
分位定义: pct = (历史 PE <= 当前 PE).mean() * 100。
"""
from __future__ import annotations

import copy
import io
import json
import logging
import threading
import time
from pathlib import Path

import pandas as pd
import requests

logger = logging.getLogger("index_valuation")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = PROJECT_ROOT / "data" / "index_valuation"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# 近十年窗口(与个股估值保持一致)
WINDOW_YEARS = 10
# 偏高阈值(与个股估值一致)
WARN_PCT = 70.0

# 单次 HTTP 超时(秒)
_TIMEOUT = 30.0
# 相邻两次 csindex 请求的最小间隔(秒),用于避免触发官网风控
_MIN_INTERVAL = 1.5
# 取数失败后的「负缓存」存活时长(秒)。失败结果不写当天缓存(否则一次瞬时失败
# 会整天显示无估值),但也不能完全不记 —— 若正被 csindex 风控,每个请求都去重试
# 会持续累加调用量、把封禁时间越拖越长,形成自我延续的循环。10 分钟足以跨过
# 实测约 6 分钟的封禁窗口,过期后自动重试。
_FAIL_TTL = 600.0

_PERF_API = "https://www.csindex.com.cn/csindex-home/perf/index-perf"
_INDICATOR_XLS = ("https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/"
                  "file/autofile/indicator/{code}indicator.xls")

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Referer": "https://www.csindex.com.cn/",
    "Accept": "application/json, text/plain, */*",
}

# ---------------------------------------------------------------------------
# ETF -> 跟踪指数 映射表
#
# ⚠️ 维护须知:这里**没有自动数据源**。akshare 没有任何接口能给出 ETF 的跟踪指数
#   (fund_etf_spot_em 只有价格字段;fund_info_index_em 的「跟踪标的」列只是
#    「沪深指数/行业主题」这类粗分类,且不含场内 ETF)。所以只能人工维护。
#
# 收录标准(不是凭名称猜的):每条都用 **ETF 与指数日收益相关系数** 客观校验过,
#   仅收录 corr >= 0.97 的条目(样本为 2024-01 ~ 2026-09 共 666 个交易日)。
#   校验脚本与原始结果见 archive/index_pe_probe_20261004/ 与 temp/map_probe/。
#   被剔除的例子:512800 银行(corr 0.46)、512200 房地产(0.22)、159611 电力(0.41)
#   —— 分不清是映射错还是 ETF 价格数据脏,宁可不收。
#
# 一个真实踩过的坑:稀土 ETF(516150)最初记成 930632,而 930632 其实是
#   「中证稀有金属主题指数」——两个指数名字相近、日相关也有 0.91,极易误收。
#   正确代码是 930598「中证稀土产业指数」(corr 0.991)。**新增条目务必跑校验。**
#
# 补充判据「累计收益差」:真正跟踪的 ETF 两年累计收益应与指数接近,选错相似指数会明显发散。
#   注意该判据对**分红型 ETF 会误报** —— 红利类 ETF 分红除权后其(不复权)价格会系统性地
#   落后指数,例如 512890 累计差 +15% 恰好约等于 2.7 年 × 4.4% 股息率,
#   但它的日相关高达 0.987、指数身份也明确,属于正常现象,不是映射错误。
#
# 未收录的 ETF 会走「无指数 PE」分支,页面显示占位符,不会报错。
# ---------------------------------------------------------------------------
INDEX_MAP: dict[str, dict[str, str]] = {
    # ---- 宽基 ----
    "510050": {"code": "000016", "name": "上证50"},
    "510300": {"code": "000300", "name": "沪深300"},
    "159919": {"code": "000300", "name": "沪深300"},
    "510500": {"code": "000905", "name": "中证500"},
    "512500": {"code": "000905", "name": "中证500"},
    "512100": {"code": "000852", "name": "中证1000"},
    "560010": {"code": "000852", "name": "中证1000"},
    "512050": {"code": "000510", "name": "中证A500"},
    "159352": {"code": "000510", "name": "中证A500"},
    "588000": {"code": "000688", "name": "科创50"},
    "510180": {"code": "000010", "name": "上证180"},
    "510210": {"code": "000001", "name": "上证指数"},
    # ---- 红利 ----
    "510880": {"code": "000015", "name": "红利指数"},
    "512890": {"code": "H30269", "name": "红利低波"},
    "563020": {"code": "H30269", "name": "红利低波"},
    "515080": {"code": "000922", "name": "中证红利"},
    "515100": {"code": "930955", "name": "红利低波100"},
    # ---- 行业 / 主题 ----
    "512880": {"code": "399975", "name": "证券公司"},
    "512170": {"code": "399989", "name": "中证医疗"},
    "512010": {"code": "000913", "name": "300医药"},
    "512660": {"code": "399967", "name": "中证军工"},
    "515030": {"code": "399976", "name": "CS新能车"},
    "515790": {"code": "931151", "name": "光伏产业"},
    "512690": {"code": "399987", "name": "中证酒"},
    "159869": {"code": "930901", "name": "动漫游戏"},
    "512980": {"code": "399971", "name": "中证传媒"},
    "512720": {"code": "930651", "name": "CS计算机"},
    "512400": {"code": "000819", "name": "有色金属"},
    # 稀土:930632 是「中证稀有金属主题」(corr 0.910 / 累计收益差 +7.8%,错);
    #      930598 才是「中证稀土产业」(corr 0.991 / +1.9%)。两个指数名字相近但不同,勿混用。
    "516150": {"code": "930598", "name": "稀土产业"},
    "512580": {"code": "000827", "name": "中证环保"},
    "159928": {"code": "000932", "name": "800消费"},
    "515170": {"code": "000807", "name": "食品饮料"},
    "515210": {"code": "930606", "name": "中证钢铁"},
    "516950": {"code": "930608", "name": "中证基建"},
    "159825": {"code": "000949", "name": "中证农业"},
    "159865": {"code": "930707", "name": "中证畜牧"},
    "159766": {"code": "930633", "name": "中证旅游"},
}


# ---------------------------------------------------------------------------
# 用户自定义映射
# ---------------------------------------------------------------------------
# 内置 INDEX_MAP 是「出厂默认」。用户可以改任意一条,也可以给内置没有的 ETF
# (境外指数、黄金等商品类)手工指定一个指数代码。
#
# 为什么单独存一个文件,而不是塞进 config/user.json:
#   ① user.json 会被「参数配置」整体回写,映射容易被顺手覆盖掉;
#   ② 映射有几十条,diff_for_ui 会把它们全算成「与默认不同」,污染设置页高亮。
# 文件不存在 = 全部沿用内置,对老用户零影响。
#
# 条目语义(键是 6 位 ETF 代码):
#   {"code": "930598", "name": "稀土产业"}  —— 用这个指数(可覆盖内置,也可新增)
#   {"code": "",       "name": ""}          —— 明确「不用指数」,即关掉某条内置映射
# 没出现在文件里的 ETF —— 沿用内置。
USER_MAP_PATH = PROJECT_ROOT / "config" / "index_map.json"

_OVERRIDE: dict | None = None
_OVERRIDE_LOCK = threading.Lock()


def normalize_entry(v) -> dict | None:
    """规范化一条用户映射。返回 None 表示这条不可用(当作没填)。"""
    if not isinstance(v, dict):
        return None
    code = str(v.get("code") or "").strip().upper()
    name = str(v.get("name") or "").strip()
    if not code:
        return {"code": "", "name": ""}      # 显式关闭
    return {"code": code, "name": name}


def load_user_map() -> dict[str, dict]:
    """读自定义映射(带进程内缓存)。文件缺失/损坏一律回退空表。"""
    global _OVERRIDE
    with _OVERRIDE_LOCK:
        if _OVERRIDE is None:
            data: dict = {}
            try:
                if USER_MAP_PATH.exists():
                    raw = json.loads(USER_MAP_PATH.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        for k, v in raw.items():
                            e = normalize_entry(v)
                            if e is not None:
                                data[str(k).zfill(6)] = e
            except Exception as e:
                logger.warning("[index_valuation] 自定义映射读取失败,本次按内置处理: %s", e)
                data = {}
            _OVERRIDE = data
        return copy.deepcopy(_OVERRIDE)


def save_user_map(m: dict) -> dict:
    """整表写入自定义映射(只留有效条目),返回规范化后的内容。"""
    clean: dict = {}
    for k, v in (m or {}).items():
        e = normalize_entry(v)
        if e is None:
            continue
        clean[str(k).zfill(6)] = e
    global _OVERRIDE
    with _OVERRIDE_LOCK:
        _OVERRIDE = clean
    USER_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
    USER_MAP_PATH.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    return copy.deepcopy(clean)


def builtin_entry(etf_code: str) -> dict | None:
    """该 ETF 的内置映射(不含用户覆盖)。"""
    e = INDEX_MAP.get(str(etf_code).zfill(6))
    return dict(e) if e else None


def effective_map() -> dict[str, dict]:
    """内置叠加用户自定义后的最终映射(每次调用都重算,保证改完立即生效)。"""
    out = {k: dict(v) for k, v in INDEX_MAP.items()}
    for code, e in load_user_map().items():
        if not e.get("code"):
            out.pop(code, None)          # 用户显式关闭
        else:
            out[code] = dict(e)
    return out


def get_entry(etf_code: str) -> dict | None:
    return effective_map().get(str(etf_code).zfill(6))


def invalidate_codes(codes) -> int:
    """清掉这些 ETF 的两级缓存。映射改了必须重算,否则会拿旧指数结果糊弄。"""
    codes = {str(c).zfill(6) for c in codes}
    n = 0
    with _MEM_LOCK:
        for c in codes:
            if _MEM.pop(c, None) is not None:
                n += 1
    with _FAIL_LOCK:
        for c in codes:
            _FAIL.pop(c, None)
    with _CACHE_LOCK:
        dc = _get_day_cache()
        for c in codes:
            dc.pop(c, None)
        _save_day_cache()
    return n

# 进程内内存缓存(etf_code -> 估值 dict)。只存**成功**结果,失败进 _FAIL。
_MEM: dict[str, object] = {}
_MEM_LOCK = threading.Lock()

# 失败负缓存(etf_code -> 到期时间戳),见 _FAIL_TTL 说明
_FAIL: dict[str, float] = {}
_FAIL_LOCK = threading.Lock()

# 当日磁盘缓存
_DAY_CACHE: dict | None = None
_CACHE_LOCK = threading.Lock()

# csindex 请求节流(全局串行,避免并发触发风控)
_NET_LOCK = threading.Lock()
_last_call = [0.0]


def _day_cache_path() -> Path:
    return CACHE_DIR / f"index_valuation_{time.strftime('%Y%m%d')}.json"


def _get_day_cache() -> dict:
    global _DAY_CACHE
    if _DAY_CACHE is None:
        try:
            p = _day_cache_path()
            _DAY_CACHE = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        except Exception:
            _DAY_CACHE = {}
    return _DAY_CACHE


def _save_day_cache() -> None:
    try:
        _day_cache_path().write_text(
            json.dumps(_DAY_CACHE or {}, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _throttled_get(url: str, params: dict | None = None):
    """带全局节流的 GET。串行 + 最小间隔,避免触发 csindex 风控。"""
    with _NET_LOCK:
        wait = _MIN_INTERVAL - (time.time() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        try:
            return requests.get(url, params=params, headers=_HEADERS, timeout=_TIMEOUT)
        finally:
            _last_call[0] = time.time()


def _fetch_hist(index_code: str, why: dict | None = None) -> pd.DataFrame | None:
    """取指数历史(收盘 + 滚动市盈率)。失败/被风控一律返回 None,不重试轰炸。

    why: 可选的「失败原因」出口。界面上的「验证」按钮需要区分
         「代码写错」和「被风控」—— 前者让用户改代码,后者让用户等一会儿,
         所以这里把原因写回调用方传进来的 dict(不想为它改函数签名返回值)。
    """
    def _fail(msg: str) -> None:
        if why is not None:
            why["error"] = msg

    end = pd.Timestamp.now().strftime("%Y%m%d")
    start = (pd.Timestamp.now() - pd.DateOffset(years=WINDOW_YEARS)).strftime("%Y%m%d")
    try:
        r = _throttled_get(_PERF_API, {"indexCode": index_code,
                                       "startDate": start, "endDate": end})
        if r.status_code != 200 or not r.headers.get("Content-Type", "").startswith(
                "application/json"):
            # 403 通常是官网临时风控(实测短时间高频调用后封 IP 约 6 分钟)
            logger.warning("[index_valuation] %s 取数失败 status=%s (可能被风控)",
                           index_code, r.status_code)
            if r.status_code in (403, 429):
                _fail(f"被中证官网限流(HTTP {r.status_code}),等几分钟再试;"
                      "连续快速尝试会把封禁时间拖长")
            else:
                _fail(f"中证官网返回 HTTP {r.status_code},未拿到数据")
            return None
        data = (r.json() or {}).get("data") or []
        if not data:
            _fail("中证官网没有这个代码的数据 —— 请确认指数代码是否写对")
            return None
        df = pd.DataFrame(data)
        if df.shape[1] < 16:
            _fail("返回数据结构异常,可能是代码不属于中证官网的指数体系")
            return None
        df = df.iloc[:, [0, 3, 9, 15]]
        df.columns = ["date", "short_name", "close", "pe"]
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        df["pe"] = pd.to_numeric(df["pe"], errors="coerce")
        return df.dropna(subset=["date"]).sort_values("date")
    except Exception as e:
        logger.warning("[index_valuation] %s 取数异常: %s", index_code, e)
        _fail(f"请求异常: {e}")
        return None


def _fetch_dividend_yield(index_code: str) -> float | None:
    """取指数当前股息率(总股本口径)。

    数据源是静态 xls,只有最近约 20 个交易日 -> 只能给当前值,没有历史分位。
    """
    try:
        url = _INDICATOR_XLS.format(code=index_code)
        r = _throttled_get(url)
        if r.status_code != 200:
            return None
        df = pd.read_excel(io.BytesIO(r.content))
        col = next((c for c in df.columns if "股息率1" in str(c)), None)
        if col is None or df.empty:
            return None
        # 文件按日期倒序,首行即最新
        v = pd.to_numeric(df[col], errors="coerce").dropna()
        return round(float(v.iloc[0]), 3) if len(v) else None
    except Exception as e:
        logger.warning("[index_valuation] %s 股息率取数失败: %s", index_code, e)
        return None


def _compute(etf_code: str) -> dict | None:
    info = get_entry(etf_code)          # 内置 + 用户自定义
    if not info:
        return None
    df = _fetch_hist(info["code"])
    if df is None or df.empty:
        return None
    pe = df["pe"].dropna()
    if len(pe) == 0:
        return None
    cur = float(pe.iloc[-1])
    pct = round(float((pe <= cur).mean() * 100.0), 2)
    # 用户只填了代码没填名称时,用官网返回的指数简称自动补上(省得用户去查)
    name = (info.get("name") or "").strip()
    if not name:
        try:
            name = str(df["short_name"].iloc[-1]).strip()
        except Exception:
            name = ""
    return {
        # 复用个股估值的字段名,便于前端统一处理
        "pe": round(cur, 2),
        "pb": None,
        "pe_pct": pct,
        "pb_pct": None,
        "pe_range": [float(pe.min()), float(pe.max())],
        "pb_range": None,
        "window_years": WINDOW_YEARS,
        # 以下为指数估值专属字段
        "source": "csindex",
        "index_code": info["code"],
        "index_name": name or info["code"],
        "samples": int(len(pe)),
        "div_yield": _fetch_dividend_yield(info["code"]),
    }


def fetch_one(etf_code: str) -> dict | None:
    """对单只 ETF 算「跟踪指数」的 PE 分位 + 当前股息率。未收录/异常 -> None。"""
    code = str(etf_code).zfill(6)
    if code not in effective_map():
        return None
    with _MEM_LOCK:
        if code in _MEM:
            return _MEM[code]
    dc = _get_day_cache()
    if code in dc:
        val = dc[code]
        with _MEM_LOCK:
            _MEM[code] = val
        return val
    with _FAIL_LOCK:
        if _FAIL.get(code, 0.0) > time.time():
            return None  # 刚失败过,负缓存未过期,先不重试(避免加剧风控)
    try:
        val = _compute(code)
    except Exception as e:
        # 兜底: _compute 内部已逐段 try/except,但任何未预期异常都不能穿透到上层接口
        # (屏幕接口一旦 500,用户看到的就是「页面打不开」)。
        logger.warning("[index_valuation] %s 计算异常: %s", code, e)
        val = None
    if val is not None:
        # 只缓存**成功**结果(内存 + 当日磁盘两份口径一致)。未收录的 ETF 在上面
        # 就已直接 return,不走这里。
        with _MEM_LOCK:
            _MEM[code] = val
        with _CACHE_LOCK:
            _get_day_cache()[code] = val
            _save_day_cache()
    else:
        # 失败**不写**当天缓存(否则一次瞬时失败会整天显示「无指数PE」,重启才能
        # 恢复),只进短 TTL 负缓存,过期后自动重试。
        with _FAIL_LOCK:
            _FAIL[code] = time.time() + _FAIL_TTL
    return val


def attach_index_valuation_to_items(items: list[dict]) -> list[dict]:
    """给 items 中的 ETF 附加指数估值(就地写 'valuation')。

    串行执行 —— csindex 对高频调用会封 IP,并发会显著抬高风控概率。
    """
    for it in items:
        code = it.get("code")
        if not code or it.get("valuation") is not None:
            continue
        try:
            val = fetch_one(str(code))
        except Exception as e:
            # 单只失败不能拖垮整个列表
            logger.warning("[index_valuation] %s 估值补全失败,跳过: %s", code, e)
            continue
        if val is not None:
            it["valuation"] = val
    return items


def probe_index(index_code: str) -> dict:
    """试取一个指数代码,给界面上的「验证」按钮即时反馈。

    不写任何缓存 —— 用户可能只是在试几个代码,试错的不该污染正式数据。
    每次调用会真实打一次中证官网,所以界面上要提示「一次验一个,别连点」。
    """
    code = str(index_code or "").strip().upper()
    if not code:
        return {"ok": False, "error": "指数代码不能为空"}

    why: dict = {}
    t0 = time.time()
    try:
        df = _fetch_hist(code, why)
    except Exception as e:                      # 兜底,不让异常穿透到接口
        logger.warning("[index_valuation] 验证 %s 异常: %s", code, e)
        return {"ok": False, "error": f"取数异常: {e}"}

    if df is None or df.empty:
        return {"ok": False, "error": why.get("error", "没有取到该指数的历史数据")}

    pe = df["pe"].dropna()
    if len(pe) == 0:
        return {"ok": False, "error": "取到历史行情,但市盈率列为空,算不出分位"}

    cur = float(pe.iloc[-1])
    pct = round(float((pe <= cur).mean() * 100.0), 2)
    try:
        name = str(df["short_name"].iloc[-1]).strip()
    except Exception:
        name = ""

    def _d(v):
        try:
            return v.strftime("%Y-%m-%d")
        except Exception:
            return str(v)

    return {
        "ok": True,
        "index_code": code,
        "index_name": name or code,             # 官网简称,可直接回填到「名称」
        "samples": int(len(pe)),
        "pe": round(cur, 2),
        "pe_pct": pct,
        "warn": pct > WARN_PCT,
        "first_date": _d(df["date"].iloc[0]),
        "last_date": _d(df["date"].iloc[-1]),
        "div_yield": _fetch_dividend_yield(code),
        "elapsed": round(time.time() - t0, 2),
    }


def is_high(valuation: dict | None) -> bool:
    """指数估值是否偏高(PE 分位 > 阈值)。"""
    if not valuation or valuation.get("source") != "csindex":
        return False
    pct = valuation.get("pe_pct")
    return pct is not None and pct > WARN_PCT


if __name__ == "__main__":
    import sys as _sys
    if len(_sys.argv) > 1:
        print(json.dumps(probe_index(_sys.argv[1]), ensure_ascii=False, indent=2))
    else:
        for c in ["512890", "512400", "516150", "518880", "159941"]:
            print(c, fetch_one(c))
