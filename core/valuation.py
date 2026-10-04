"""
估值分位模块(市盈率 PE(TTM) / 市净率 PB 历史分位)

用途:
- 在「我的自选」与「当前信号」中展示单只 A 股当前估值(PE/PB)在历史 N 年区间中所处
  的分位。分位 > 70% 视为「估值偏高」(见需求),前端标红提示。
- 仅对 A 股股票有意义(ETF / 基金没有 PE/PB -> 本模块返回 None)。
  ETF 的估值改看「底层跟踪指数」,由 `index_valuation` 模块提供,
  在 `attach_valuation_to_items` 里统一补齐,调用方无需区分。
- 数据源: akshare `stock_zh_valuation_baidu`(百度股市通)。
  注意:`indicator` 必须严格传 '市盈率(TTM)' / '市净率',传 '市盈率' 会触发 akshare 内部结构
  解析异常(已踩坑)。`period` 取 '近十年' 覆盖两个完整牛熊周期(2015-16 与 2021 高点),
  比 5 年更有代表性;常量 `VALUATION_WINDOW_YEARS` 可一键切换为 5。
- 分位定义: 当前估值在历史上「不高于它」的交易日占比
  pct = (历史值 <= 当前值).mean() * 100,范围 0~100。
- 健壮性: 网络超时 / 异常一律降级为 None,绝不抛异常阻塞 UI;结果按自然日磁盘缓存 +
  进程内内存缓存,重复请求不重复打 API。
"""
from __future__ import annotations

import json
import logging
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

from . import data_source as ds
from . import index_valuation

logger = logging.getLogger("valuation")

# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 估值缓存目录(按自然日分文件)
VALUATION_CACHE_DIR = PROJECT_ROOT / "data" / "valuation"
VALUATION_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# 默认回溯窗口(年)。10 年覆盖两轮牛熊周期,比 5 年更能反映估值水位。
# 如需改成 5 年,把这里改成 5 即可(前端会用返回的 window_years 自适应展示)。
VALUATION_WINDOW_YEARS = 10

# 偏高阈值:分位高于此值视为「估值偏高」,前端标红。
VALUATION_WARN_PCT = 70.0

# akshare period 参数 <-> 内部年数
_PERIOD_MAP = {1: "近一年", 3: "近三年", 5: "近五年", 10: "近十年"}

# akshare indicator 参数(必须严格使用这两个字面值,踩过坑)
_BAIDU_INDICATORS = {
    "pe": "市盈率(TTM)",
    "pb": "市净率",
}

# 单只股票一次完整拉取(PE + PB)的网络超时(秒)
_FETCH_TIMEOUT = 40.0

# akshare 模块懒加载缓存
_AK = None
_AK_LOCK = threading.Lock()

# 进程内内存缓存(code -> 估值 dict 或 None),避免同进程重复计算
_MEM: dict[str, object] = {}
_MEM_LOCK = threading.Lock()

# 当日磁盘缓存(整个文件一次性读写)
_DAY_CACHE: dict[str, object] | None = None
_CACHE_LOCK = threading.Lock()


# ------------- 模块懒加载 -------------
def _ak_module():
    """懒加载 akshare,避免硬依赖让 UI 启动失败;多个线程并发时由锁保证只 import 一次。"""
    global _AK
    if _AK is not None:
        return _AK
    with _AK_LOCK:
        if _AK is not None:
            return _AK
        import akshare as ak  # noqa: F401  (延迟到真正用到估值时才 import)
        _AK = ak
    return _AK


# ------------- 磁盘缓存(按自然日) -------------
def _day_cache_path() -> Path:
    return VALUATION_CACHE_DIR / f"valuation_{time.strftime('%Y%m%d')}.json"


def _load_day_cache() -> dict:
    try:
        p = _day_cache_path()
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _get_day_cache() -> dict:
    global _DAY_CACHE
    if _DAY_CACHE is None:
        _DAY_CACHE = _load_day_cache()
    return _DAY_CACHE


def _save_day_cache() -> None:
    try:
        p = _day_cache_path()
        p.write_text(json.dumps(_DAY_CACHE or {}, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _day_cache_set(code: str, val) -> None:
    with _CACHE_LOCK:
        _get_day_cache()[code] = val
        _save_day_cache()


# ------------- 网络调用(带超时) -------------
def _call_with_timeout(func, timeout: float):
    """在独立线程执行 func,带 socket 级超时,避免网络请求无限挂住。返回 (ok, value_or_error)。"""
    old = socket.getdefaulttimeout()
    box: dict = {}

    def _runner():
        try:
            socket.setdefaulttimeout(timeout)
            box["value"] = func()
            box["ok"] = True
        except Exception as e:  # noqa: BLE001
            box["value"] = str(e)
            box["ok"] = False

    t = threading.Thread(target=_runner, daemon=True, name="valuation-timeout")
    t.start()
    t.join(timeout=timeout + 5.0)
    socket.setdefaulttimeout(old)
    if t.is_alive():
        return False, f"估值拉取超过 {timeout} 秒仍未返回"
    return bool(box.get("ok")), box.get("value")


def _fetch_raw(code: str, window_years: int):
    """联网拉取单只股票的 PE/PB 历史序列,返回 (pe_df, pb_df) 或异常时 None。带超时。"""
    ak = _ak_module()
    period = _PERIOD_MAP.get(window_years, "近十年")

    def _do():
        pe = ak.stock_zh_valuation_baidu(symbol=code, indicator=_BAIDU_INDICATORS["pe"], period=period)
        pb = ak.stock_zh_valuation_baidu(symbol=code, indicator=_BAIDU_INDICATORS["pb"], period=period)
        return pe, pb

    ok, val = _call_with_timeout(_do, _FETCH_TIMEOUT)
    if not ok:
        logger.warning("[valuation] %s 估值拉取超时/失败: %s", code, val)
        return None
    return val


# ------------- 计算 -------------
def _series_stats(df):
    """从 akshare 返回的 DataFrame([date, value])提取 (当前值, [min,max], 数值数组)。无数据返回 (None,None,[])。"""
    if df is None or getattr(df, "empty", True) or "value" not in getattr(df, "columns", []):
        return None, None, []
    s = pd.to_numeric(df["value"], errors="coerce").dropna()
    if len(s) == 0:
        return None, None, []
    cur = float(s.iloc[-1])          # 最后一行即最新估值
    rng = [float(s.min()), float(s.max())]
    arr = s.tolist()
    return cur, rng, arr


def _pct_of(arr, current) -> float | None:
    if not arr or current is None:
        return None
    below = sum(1 for v in arr if v <= current)
    return round(below / len(arr) * 100.0, 2)


def _compute(code: str, window_years: int) -> dict | None:
    """核心计算:返回估值分位 dict;ETF / 无数据 / 拉取失败返回 None。"""
    raw = _fetch_raw(code, window_years)
    if raw is None:
        return None
    pe_df, pb_df = raw
    pe_cur, pe_rng, pe_arr = _series_stats(pe_df)
    pb_cur, pb_rng, pb_arr = _series_stats(pb_df)
    if pe_cur is None and pb_cur is None:
        return None
    return {
        "pe": pe_cur,
        "pb": pb_cur,
        "pe_pct": _pct_of(pe_arr, pe_cur) if pe_arr else None,
        "pb_pct": _pct_of(pb_arr, pb_cur) if pb_arr else None,
        "pe_range": pe_rng,
        "pb_range": pb_rng,
        "window_years": window_years,
        "source": "baidu",
    }


# ------------- 对外接口 -------------
def fetch_one(code: str, window_years: int = VALUATION_WINDOW_YEARS) -> dict | None:
    """对单只 A 股计算 PE/PB 历史分位。ETF / 异常 -> 返回 None。带二级缓存。"""
    code = str(code).zfill(6)
    if not ds.is_stock_code(code):
        return None  # ETF 无 PE/PB

    # 1) 进程内内存缓存
    with _MEM_LOCK:
        if code in _MEM:
            return _MEM[code]
    # 2) 当日磁盘缓存
    dc = _get_day_cache()
    if code in dc:
        val = dc[code]
        with _MEM_LOCK:
            _MEM[code] = val
        return val
    # 3) 联网拉取 + 写回两级缓存(失败不落任何缓存,理由同 fetch_batch)
    val = _compute(code, window_years)
    if val is not None:
        with _MEM_LOCK:
            _MEM[code] = val
        _day_cache_set(code, val)
    return val


def fetch_batch(codes: list[str], max_workers: int = 4,
                window_years: int = VALUATION_WINDOW_YEARS) -> dict[str, dict | None]:
    """并发批量计算,返回 {code: 估值 dict 或 None}。已缓存的命中缓存不重复联网。"""
    codes = [str(c).zfill(6) for c in codes]
    results: dict[str, dict | None] = {}
    to_fetch: list[str] = []

    for c in codes:
        if not ds.is_stock_code(c):
            results[c] = None
            continue
        with _MEM_LOCK:
            if c in _MEM:
                results[c] = _MEM[c]
                continue
        dc = _get_day_cache()
        if c in dc:
            v = dc[c]
            with _MEM_LOCK:
                _MEM[c] = v
            results[c] = v
            continue
        to_fetch.append(c)

    if to_fetch:
        try:
            with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(to_fetch)))) as ex:
                for c, v in zip(to_fetch, ex.map(lambda x: _compute(x, window_years), to_fetch)):
                    results[c] = v
        except Exception as e:  # noqa: BLE001
            logger.warning("[valuation] 批量估值异常: %s", e)
            for c in to_fetch:
                results.setdefault(c, None)
        # 一次性写回缓存(加锁)。只写**成功**结果:失败若也进内存缓存,该标的会
        # 在整个进程生命周期内都显示「无估值」(与磁盘缓存同一个坑,进程常驻时
        # 窗口比「当天」还长)。不缓存 = 下次请求自动重试。
        with _MEM_LOCK:
            for c in to_fetch:
                v = results.get(c)
                if v is not None:
                    _MEM[c] = v
        try:
            with _CACHE_LOCK:
                dc = _get_day_cache()
                for c in to_fetch:
                    v = results.get(c)
                    # 只落盘**成功**结果:拉取失败(网络抖动等)若也写进去,
                    # 会让该标的整天都显示「无估值」,重启才能恢复。
                    if v is not None:
                        dc[c] = v
                _save_day_cache()
        except Exception:
            pass

    return results


def attach_valuation_to_items(items: list[dict], max_workers: int = 4,
                              window_years: int = VALUATION_WINDOW_YEARS) -> list[dict]:
    """给 items(每个含 'code' 的 dict)批量附加估值分位,就地添加 'valuation' 字段。

    - A 股股票 -> 自身 PE/PB 的历史分位(百度股市通)。
    - ETF      -> **底层跟踪指数**的 PE 分位 + 当前股息率(中证指数官网,见 index_valuation)。
                  ETF 本身没有 PE;未收录映射的 ETF(境外指数、商品/货币类)仍为 None。
    拉取失败 -> None,单只失败不影响其余。
    """
    if not items:
        return items
    codes = [str(it.get("code", "")).zfill(6) for it in items]
    results = fetch_batch(codes, max_workers=max_workers, window_years=window_years)
    for it, code in zip(items, codes):
        it["valuation"] = results.get(code)
    # ETF 自身估值为 None,这里补上「跟踪指数」的口径
    index_valuation.attach_index_valuation_to_items(items)
    return items


def is_high(valuation: dict | None) -> bool:
    """估值是否偏高(分位 > 阈值),供后端/前端快速判定标红。"""
    if not valuation:
        return False
    pe = valuation.get("pe_pct")
    pb = valuation.get("pb_pct")
    if pe is not None and pe > VALUATION_WARN_PCT:
        return True
    if pb is not None and pb > VALUATION_WARN_PCT:
        return True
    return False


if __name__ == "__main__":
    # 命令行自测
    for c in ["600519", "000001", "510300", "300750"]:
        print(c, fetch_one(c))
