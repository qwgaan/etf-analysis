"""
A 股/ETF 盘中实时行情与当天分时 K 线接口。

实时价:
- 仅用于警戒推送前的「提前 3 分钟预热」场景:
  交易时段内,在配置推送时间点前 3 分钟开始查询;
  每 60 秒查一次,获取到值即停止后续查询;
  到达推送时间后,用最新实时价替代日 K 收盘价评估警戒;
  非交易时段或查询失败时,自动回退到日 K 收盘价。
- 数据源: 新浪 hq.sinajs.cn (无需认证、稳定性高、兼容 ETF/A 股)。

当天分时 K 线:
- 按需拉取,无需预热;返回当天 1 分钟 OHLCV,供前端绘制「当天」K 线图。
- 数据源: 新浪财经分钟线 stock_zh_a_minute(兼容 ETF 与 A 股)。
"""
from __future__ import annotations

import copy
import datetime as dt
import re
import threading
import urllib.error
import urllib.request
from typing import Any

import akshare as ak
import pandas as pd

from . import data_source as ds

try:
    from . import tdx_source as tdx
except Exception:  # pragma: no cover
    tdx = None

try:
    from . import config as _cfg_mod
except Exception:  # pragma: no cover
    _cfg_mod = None


# --------------------- 兜底源调用超时保护(防拖挂 HTTP 请求) ---------------------
# 东财(akshare)在代理不通时会无限重试(Max retries ~20s+),新浪 urllib 已有 timeout,
# 这里给东财兜底调用加墙钟超时:超时即放弃,由上层其它源或日 K 兜底,绝不拖挂请求。
_em_in_flight = threading.Event()


def _bounded(fn, timeout: float):
    """在 worker 线程中执行 fn(),墙钟超时返回 None 并放弃该兜底源。"""
    if _em_in_flight.is_set():
        return None
    box: dict = {}

    def _worker():
        try:
            box["v"] = fn()
        except Exception:  # noqa: BLE001
            pass
        finally:
            _em_in_flight.clear()

    _em_in_flight.set()
    th = threading.Thread(target=_worker, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        return None
    return box.get("v")


def _tdx_enabled() -> bool:
    """通达信直连源是否可用(模块已加载且配置启用)。"""
    return tdx is not None and tdx.is_enabled()


SINA_HQ_URL = "https://hq.sinajs.cn/list={}"


# 东财实时快照缓存(仅在新浪失败兜底时使用,避免频繁拉全市场快照)
_EM_SNAPSHOT: dict[str, Any] = {"ts": 0.0, "data": {}}

# 默认数据源顺序(当配置不可用时):数组顺序=优先级/回退顺序
_DEFAULT_DATA_SOURCES: dict[str, list[str]] = {
    "realtime": ["tdx", "sina", "em"],
    "intraday": ["tdx", "em", "sina"],
    "kline": ["sina", "em", "tdx"],
}


def _data_sources() -> dict[str, list[str]]:
    """读取当前数据源顺序。"""
    if _cfg_mod is None:
        return copy.deepcopy(_DEFAULT_DATA_SOURCES)
    try:
        ds = _cfg_mod.load_user().get("data_sources") or copy.deepcopy(_DEFAULT_DATA_SOURCES)
        # 兼容: 若后端仍读到旧 dict,转成数组(实际应由 _migrate_config 处理)
        for purpose in list(ds.keys()):
            if isinstance(ds[purpose], dict):
                ds[purpose] = [s for s in _DEFAULT_DATA_SOURCES[purpose] if ds[purpose].get(s)]
            elif not isinstance(ds[purpose], list):
                ds[purpose] = copy.deepcopy(_DEFAULT_DATA_SOURCES[purpose])
        return ds
    except Exception:
        return copy.deepcopy(_DEFAULT_DATA_SOURCES)


def _em_realtime_snapshot() -> dict[str, dict[str, float | None]]:
    """东财实时快照兜底:返回 {code: {"price", "open", "prev_close"}}。

    - 仅在新浪实时行情失败时调用,合并 fund_etf_spot_em + stock_zh_a_spot_em;
    - 60 秒内复用缓存,避免同一推送窗口重复拉取全市场快照。
    - 东财快照通常不含昨收,prev_close 置 None(调用方回退到日 K 开盘价)。
    """
    import time as _t
    now = _t.time()
    if now - _EM_SNAPSHOT.get("ts", 0.0) < 60 and _EM_SNAPSHOT.get("data"):
        return _EM_SNAPSHOT["data"]

    data: dict[str, dict[str, float | None]] = {}
    akmod = None
    try:
        akmod = _akshare_safe_import()
    except Exception:
        akmod = None
    if akmod is not None:
        for fn_name in ("fund_etf_spot_em", "stock_zh_a_spot_em"):
            try:
                fn = getattr(akmod, fn_name, None)
                if fn is None:
                    continue
                # 东财在代理不通时会无限重试(~20s+),用墙钟超时保护:超时即跳过该源
                df = _bounded(fn, 4)
                if df is None:
                    print(f"[intraday] 东财实时快照 {fn_name} 超时/失败,跳过")
                    continue
                for _, row in df.iterrows():
                    code = str(row.get("代码", "")).zfill(6)
                    if not code:
                        continue
                    try:
                        price = float(row.get("最新价", 0) or 0)
                        open_ = float(row.get("今开", 0) or 0)
                    except (ValueError, TypeError):
                        continue
                    if price <= 0:
                        continue
                    data.setdefault(code, {"price": price, "open": open_ or None, "prev_close": None})
            except Exception as e:
                print(f"[intraday] 东财实时快照 {fn_name} 失败: {e}")
    _EM_SNAPSHOT["ts"] = now
    _EM_SNAPSHOT["data"] = data
    return data


def _akshare_safe_import():
    import akshare as _ak
    return _ak


def _sina_list_code(code: str) -> str:
    """把 6 位代码映射为新浪行情接口的 sh/sz 前缀格式。"""
    code = str(code).zfill(6)
    # 沪市: 60/68/88/89(股票); 11/50-59/90/99(ETF/指数/基金)
    if code.startswith(
        ("60", "68", "88", "89", "50", "51", "52", "53", "54", "55", "56", "57", "58", "59", "90", "99", "11")
    ):
        return f"sh{code}"
    # 其余默认深市(00/30/08 股票; 12/13/15/16/18 ETF 等)
    return f"sz{code}"


def _sina_fetch_prices(codes: list[str], timeout: float = 6.0) -> dict[str, float | None]:
    """从新浪行情接口拉取一批实时价,返回 {code: price};失败/缺失项以 None 表示。"""
    codes = [str(c).zfill(6) for c in codes]
    sina_codes = [_sina_list_code(c) for c in codes]
    url = SINA_HQ_URL.format(",".join(sina_codes))
    out: dict[str, float | None] = {c: None for c in codes}
    try:
        req = urllib.request.Request(url)
        req.add_header("Referer", "https://finance.sina.com.cn")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("gbk", errors="ignore")
    except Exception as e:
        print(f"[intraday] 新浪实时行情查询失败: {e}")
        return out
    if text:
        for line in text.splitlines():
            m = re.match(r'var hq_str_([a-z]{2})(\d{6})="([^"]*)"', line.strip())
            if not m:
                continue
            _, code, data = m.groups()
            parts = data.split(",")
            if len(parts) < 4:
                continue
            try:
                price = float(parts[3])
                if price <= 0:
                    continue
                out[code] = price
            except (ValueError, IndexError):
                continue
    return out


def _sina_fetch_quotes(codes: list[str], timeout: float = 6.0) -> dict[str, dict[str, float | None]]:
    """从新浪行情接口拉取一批实时行情详情,返回 {code: {"price","prev_close","open"}}。"""
    codes = [str(c).zfill(6) for c in codes]
    sina_codes = [_sina_list_code(c) for c in codes]
    url = SINA_HQ_URL.format(",".join(sina_codes))
    out: dict[str, dict[str, float | None]] = {
        c: {"price": None, "prev_close": None, "open": None} for c in codes
    }
    try:
        req = urllib.request.Request(url)
        req.add_header("Referer", "https://finance.sina.com.cn")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("gbk", errors="ignore")
    except Exception as e:
        print(f"[intraday] 新浪实时行情详情查询失败: {e}")
        return out
    if text:
        for line in text.splitlines():
            m = re.match(r'var hq_str_([a-z]{2})(\d{6})="([^"]*)"', line.strip())
            if not m:
                continue
            _, code, data = m.groups()
            parts = data.split(",")
            if len(parts) < 4:
                continue
            try:
                price = float(parts[3]) if float(parts[3]) > 0 else None
                prev_close = float(parts[2]) if float(parts[2]) > 0 else None
                open_ = float(parts[1]) if float(parts[1]) > 0 else None
                out[code] = {"price": price, "prev_close": prev_close, "open": open_}
            except (ValueError, IndexError):
                continue
    return out


def is_trading_hours(now: dt.datetime | None = None) -> bool:
    """当前是否处于 A 股交易时段(9:30-11:30 或 13:00-15:00)。"""
    now = now or dt.datetime.now()
    t = now.time()
    morning = dt.time(9, 30, 0) <= t <= dt.time(11, 30, 0)
    afternoon = dt.time(13, 0, 0) <= t <= dt.time(15, 0, 0)
    return morning or afternoon


def is_market_open_day(d: dt.date | None = None) -> bool:
    """是否为工作日(周一~周五),不含节假日判断(由调用方补充)。"""
    d = d or dt.date.today()
    return d.weekday() < 5


def fetch_realtime_prices(codes: list[str], timeout: float = 6.0) -> dict[str, float | None]:
    """
    批量查询实时行情,返回 {code: price}。

    数据源三级兜底: 通达信直连(主) → 新浪(兜底) → 东财实时快照(末级兜底)。
    - 价格取最新成交价。
    - 未开盘、停牌或接口异常时,对应 code 返回 None。
    - 只要有一个源查到值就不会抛异常,失败项以 None 表示。
    """
    if not codes:
        return {}

    codes = [str(c).zfill(6) for c in codes]
    result: dict[str, float | None] = {c: None for c in codes}

    # 按用户勾选的数据源顺序回退(默认:通达信 → 新浪 → 东财)
    order = _data_sources().get("realtime", _DEFAULT_DATA_SOURCES["realtime"])

    for src in order:
        if not any(v is None for v in result.values()):
            break
        if src == "tdx" and _tdx_enabled():
            try:
                tdx_prices = tdx.get_realtime(codes)
                for c in codes:
                    p = (tdx_prices.get(c) or {}).get("price")
                    if p is not None and result.get(c) is None:
                        result[c] = p
            except Exception as e:
                print(f"[intraday] 通达信实时价异常,回退下一源: {e}")
        elif src == "sina":
            sina = _sina_fetch_prices(codes, timeout=timeout)
            for c in codes:
                if result.get(c) is None and sina.get(c) is not None:
                    result[c] = sina[c]
        elif src == "em":
            snap = _em_realtime_snapshot()
            for c in codes:
                if result.get(c) is None and c in snap:
                    result[c] = snap[c].get("price")

    return result


def fetch_realtime_prices_once(codes: list[str]) -> dict[str, float | None]:
    """兼容别名,语义同 fetch_realtime_prices。"""
    return fetch_realtime_prices(codes)


def fetch_realtime_quotes(codes: list[str], timeout: float = 6.0) -> dict[str, dict[str, float | None]]:
    """
    批量查询实时行情(更完整字段),返回 {code: {"price","prev_close","open"}}。

    数据源三级兜底: 通达信直连(主) → 新浪(兜底) → 东财实时快照(末级兜底)。
    失败/停牌项对应字段为 None,不会抛异常。
    """
    if not codes:
        return {}
    codes = [str(c).zfill(6) for c in codes]
    result: dict[str, dict[str, float | None]] = {
        c: {"price": None, "prev_close": None, "open": None} for c in codes
    }

    # 按用户勾选的数据源顺序回退(默认:通达信 → 新浪 → 东财)
    order = _data_sources().get("realtime", _DEFAULT_DATA_SOURCES["realtime"])

    for src in order:
        if not any(v["price"] is None for v in result.values()):
            break
        if src == "tdx" and _tdx_enabled():
            try:
                tdx_q = tdx.get_realtime(codes)
                for c in codes:
                    q = tdx_q.get(c) or {}
                    if q.get("price") is not None and result.get(c, {}).get("price") is None:
                        result[c] = {
                            "price": q.get("price"),
                            "prev_close": q.get("prev_close"),
                            "open": q.get("open"),
                        }
            except Exception as e:
                print(f"[intraday] 通达信实时行情详情异常,回退下一源: {e}")
        elif src == "sina":
            sina = _sina_fetch_quotes(codes, timeout=timeout)
            for c in codes:
                if result.get(c, {}).get("price") is None and sina.get(c, {}).get("price") is not None:
                    result[c] = sina[c]
        elif src == "em":
            snap = _em_realtime_snapshot()
            for c in codes:
                if result.get(c, {}).get("price") is None and c in snap:
                    s = snap[c]
                    result[c] = {"price": s.get("price"), "prev_close": s.get("prev_close"), "open": s.get("open")}

    return result


def _normalize_intraday_df(df: pd.DataFrame, date: dt.date) -> pd.DataFrame | None:
    """把东财/新浪当天分时列名统一并过滤到指定日期。"""
    if df is None or df.empty:
        return None
    if "时间" in df.columns:
        df = df.rename(columns={"时间": "time"})
    elif "day" in df.columns:
        df = df.rename(columns={"day": "time"})
    else:
        return None
    if "成交额" in df.columns:
        df = df.rename(columns={"成交额": "amount"})
    if "amount" not in df.columns:
        df["amount"] = df["close"] * df["volume"]
    df["time"] = pd.to_datetime(df["time"], errors="coerce")
    df = df.dropna(subset=["time"])
    df_today = df[df["time"].dt.date == date].copy()
    if df_today.empty:
        return None
    df_today = df_today.sort_values("time").reset_index(drop=True)
    for col in ["open", "high", "low", "close", "volume", "amount"]:
        if col in df_today.columns:
            df_today[col] = pd.to_numeric(df_today[col], errors="coerce").fillna(0.0)
    return df_today[["time", "open", "high", "low", "close", "volume", "amount"]]


def fetch_today_kline(code: str, date: dt.date | None = None) -> pd.DataFrame | None:
    """
    拉取指定代码当天 1 分钟 K 线。

    按用户勾选的数据源顺序回退(默认:通达信 → 东财 → 新浪)。
    列名: time, open, high, low, close, volume, amount。
    """
    code = str(code).zfill(6)
    date = date or dt.date.today()

    order = _data_sources().get("intraday", _DEFAULT_DATA_SOURCES["intraday"])

    date_str = date.strftime("%Y-%m-%d")
    start_dt = f"{date_str} 09:30:00"
    end_dt = f"{date_str} 17:00:00"

    for src in order:
        df: pd.DataFrame | None = None
        if src == "tdx" and _tdx_enabled():
            try:
                tdx_df = tdx.get_today_minutes(code)
                if tdx_df is not None and not tdx_df.empty:
                    return tdx_df
            except Exception as e:
                print(f"[intraday] {code} 通达信当天分时失败,尝试下一源: {e}")
        elif src == "em":
            try:
                if ds.is_stock_code(code):
                    df = _bounded(lambda: ak.stock_zh_a_hist_min_em(symbol=code, period="1", adjust="", start_date=start_dt, end_date=end_dt), 6)
                else:
                    df = _bounded(lambda: ak.fund_etf_hist_min_em(symbol=code, period="1", adjust="", start_date=start_dt, end_date=end_dt), 6)
            except Exception as e:
                print(f"[intraday] {code} 东财当天分时拉取失败: {e}")
        elif src == "sina":
            try:
                df = _bounded(lambda: ak.stock_zh_a_minute(symbol=_sina_list_code(code), period="1", adjust=""), 6)
            except Exception as e:
                print(f"[intraday] {code} 新浪当天分时拉取失败: {e}")
        normalized = _normalize_intraday_df(df, date)
        if normalized is not None and not normalized.empty:
            return normalized

    return None
