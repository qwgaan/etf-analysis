"""
通达信直连行情源(pytdx),作为实时价 / 当天分时 / 日 K 的抗限流兜底源。

定位:
- 直连券商行情主站(TCP 7709),数据来自交易所行情经券商转发,比新浪/东财的 HTTP 接口更"源",
  延迟更低、且不依赖网站反爬/限流,是现有新浪/东财链路之外的一层高可用兜底。
- 本模块只负责「接通达信」这一件事:连接管理(best_ip + 多服务器 failover)、节流、复权、字段归一。
- 上层(intraday_price / data_source)负责把它串进 通达信→新浪→东财 的三级兜底顺序。

注意:
- 单 IP 频率硬限制约 3 次/秒,本模块统一节流 0.34s。
- 行情主站地址会漂移,内置候选列表失效时退化为 best_ip 探测。
- 合规:仅用于个人研究/自用,非商业、非高频;公开仓库与 Docker 镜像请在 README 加免责声明。
"""
from __future__ import annotations

import copy
import datetime as _dt
import logging
import threading
import time
from typing import Any, Optional

try:
    from . import config as _cfg_mod
except Exception:  # pragma: no cover
    _cfg_mod = None

logger = logging.getLogger("tdx_source")

# 配置同步缓存:避免每次请求都读盘
_cfg_sync_ts = 0.0
_CFG_SYNC_INTERVAL = 10.0

# 内置候选行情主站(公开常见可用;best_ip 探测失败时使用)。首个为已验证可用节点。
_FALLBACK_HOSTS: list[tuple[str, int]] = [
    ("sztdx.gtjas.com", 7709),
    ("shtdx.gtjas.com", 7709),
    ("119.147.212.81", 7709),
    ("221.231.141.60", 7709),
    ("101.227.73.20", 7709),
    ("14.215.128.18", 7709),
    ("122.192.35.44", 7709),
]

# 单 IP 频率硬限制约 3 次/秒 => 间隔 0.34s
_MIN_INTERVAL = 0.34

# 单服务器连接超时(秒)
_DEFAULT_TIMEOUT = 8

# 全局配置(由 configure 从用户配置载入)
_cfg: dict[str, Any] = {
    "timeout": _DEFAULT_TIMEOUT,
    "min_interval": _MIN_INTERVAL,
    "best_ip": False,       # 是否每次启动尝试 best_ip 探测(较慢,默认关)
    # 数据源顺序(由 _sync_cfg 从 user 配置同步):数组中某用途包含 "tdx" 即启用通达信
    "data_sources": {
        "realtime": ["tdx", "sina", "em"],
        "intraday": ["tdx", "em", "sina"],
        "kline": ["sina", "em", "tdx"],
    },
}

# 连接单例与节流状态
_api: Any = None
_api_host: Optional[tuple[str, int]] = None
_last_good_host: Optional[tuple[str, int]] = None  # 上次成功连接的主站,下次优先复用,避免冷启动逐个试慢主机
_api_lock = threading.Lock()
_throttle_lock = threading.Lock()
_last_call_ts = 0.0
_init_lock = threading.Lock()


def configure(cfg: Optional[dict] = None) -> None:
    """从用户配置片段 {"tdx_source": {...}, "data_sources": {...}} 更新全局参数。"""
    if not cfg:
        return
    sub = cfg.get("tdx_source") if isinstance(cfg.get("tdx_source"), dict) else cfg
    if isinstance(sub, dict):
        for k in ("timeout", "min_interval", "best_ip"):
            if k in sub:
                _cfg[k] = sub[k]
    ds = cfg.get("data_sources") if isinstance(cfg.get("data_sources"), dict) else None
    if ds:
        normalized: dict[str, list[str]] = {}
        defaults = {"realtime": ["tdx", "sina", "em"], "intraday": ["tdx", "em", "sina"], "kline": ["sina", "em", "tdx"]}
        for purpose, default_order in defaults.items():
            val = ds.get(purpose, default_order)
            if isinstance(val, dict):
                val = [s for s in default_order if val.get(s)]
            elif not isinstance(val, list):
                val = copy.deepcopy(default_order)
            normalized[purpose] = [s for s in val if s in ("tdx", "sina", "em")]
        _cfg["data_sources"] = normalized
    _cfg["min_interval"] = max(0.1, float(_cfg.get("min_interval", _MIN_INTERVAL)))
    _cfg["timeout"] = max(2, int(_cfg.get("timeout", _DEFAULT_TIMEOUT)))
    logger.info("[tdx] 配置已更新: timeout=%s min_interval=%s best_ip=%s data_sources=%s",
                _cfg["timeout"], _cfg["min_interval"], _cfg["best_ip"], _cfg["data_sources"])


def _sync_cfg() -> None:
    """从用户配置自动同步 tdx_source 子配置(带时间缓存,避免频繁读盘)。"""
    global _cfg_sync_ts
    if _cfg_mod is None:
        return
    now = time.time()
    if now - _cfg_sync_ts < _CFG_SYNC_INTERVAL:
        return
    try:
        user = _cfg_mod.load_user()
        configure(user)
        _cfg_sync_ts = now
    except Exception as e:  # noqa: BLE001
        logger.warning("[tdx] 配置同步失败: %s", e)


def _any_tdx_enabled() -> bool:
    """任一用途启用了通达信,模块才会尝试建连。"""
    ds = _cfg.get("data_sources") or {}
    return any("tdx" in (ds.get(p) or []) for p in ("realtime", "intraday", "kline"))


def is_enabled() -> bool:
    _sync_cfg()
    return _any_tdx_enabled()


def is_kline_fallback_enabled() -> bool:
    _sync_cfg()
    return "tdx" in (_cfg.get("data_sources") or {}).get("kline", [])


def _throttle() -> None:
    """满足单 IP ≤3 次/秒的节流。"""
    with _throttle_lock:
        now = time.time()
        wait = _cfg["min_interval"] - (now - _last_call_ts)
        if wait > 0:
            time.sleep(wait)
        globals()["_last_call_ts"] = time.time()


def _resolve_market(code: str):
    """6 位代码 -> (market, code)。返回 None 表示该代码通达信标准行情不支持(如北交所),由上层回退。"""
    from pytdx.params import TDXParams
    c = str(code).zfill(6)
    if c.startswith(("5", "6", "9")):
        return TDXParams.MARKET_SH, c
    if c.startswith(("0", "1", "2", "3")):
        return TDXParams.MARKET_SZ, c
    # 8/4 开头:北交所/老三板,标准行情 API 不支持,交由新浪/东财兜底
    return None, c


def _build_api() -> Optional[Any]:
    """构建并连接一个 TdxHq_API,带 failover。返回已连接 api 或 None。"""
    from pytdx.hq import TdxHq_API

    # 优先复用上次成功的主站,避免每次冷启动都从列表头逐个试(慢主机会拖长首连)
    hosts: list[tuple[str, int]] = list(_FALLBACK_HOSTS)
    if _last_good_host:
        hosts = [h for h in hosts if h != _last_good_host]
        hosts.insert(0, _last_good_host)
    if _cfg.get("best_ip"):
        try:
            from pytdx.util.best_ip import select_best_ip
            best = select_best_ip()
            if best and best.get("ip"):
                hosts = [(best["ip"], int(best.get("port", 7709)))] + hosts
        except Exception as e:  # noqa: BLE001
            logger.warning("[tdx] best_ip 探测失败: %s", e)

    last_err: Any = None
    # 连接超时收紧到 6s:单主机握手过慢即跳过试下一台,避免首连被一台慢主机拖死
    timeout = max(3, min(int(_cfg["timeout"]), 6))
    for ip, port in hosts:
        try:
            api = TdxHq_API(heartbeat=True, auto_retry=False)
            if api.connect(ip, port, timeout):
                logger.info("[tdx] 行情主站连接成功 %s:%s", ip, port)
                globals()["_last_good_host"] = (ip, port)
                return api
            try:
                api.disconnect()
            except Exception:
                pass
        except Exception as e:  # noqa: BLE001
            last_err = e
            logger.warning("[tdx] 连接 %s:%s 失败: %s", ip, port, e)
    logger.error("[tdx] 所有行情主站连接失败(最后错误: %s)", last_err)
    return None


def _get_api() -> Optional[Any]:
    """惰性获取已连接 api(单例),失败自动重连。"""
    global _api, _api_host
    with _init_lock:
        if _api is not None:
            # 探活
            try:
                if not getattr(_api, "connected", False):
                    _api.disconnect()
                    _api = None
            except Exception:  # noqa: BLE001
                try:
                    _api.disconnect()
                except Exception:
                    pass
                _api = None
        if _api is None:
            _api = _build_api()
            if _api is not None:
                _api_host = _last_good_host  # 实际连上的主站,仅用于日志
    return _api


def _guard() -> Optional[Any]:
    """调用前统一检查:未启用 / 连接失败 直接返回 None。"""
    if not is_enabled():
        return None
    return _get_api()


# ----------------------- 调用超时保护(防拖挂 HTTP 请求) -----------------------
# True 表示有 TDX 调用正在执行(串行 + 防堆积)
_in_flight = threading.Event()


def _run_with_timeout(fn, timeout: float):
    """在 worker 线程中执行 fn(),墙钟超时即放弃(返回 None),绝不拖挂上层请求。

    - 串行:若有调用正在进行(含已超时孤儿线程),直接返回 None,由上层新浪/东财兜底,避免堆积。
    - 超时:标记 _api 为 None,下次调用自动重连。
    - fn 内部可包含「连接 + 取数」,连接耗时被一并计入墙钟超时。
    """
    if _in_flight.is_set():
        return None
    box: dict = {}

    def _worker():
        try:
            box["v"] = fn()
        except Exception as e:  # noqa: BLE001
            box["e"] = e
        finally:
            _in_flight.clear()

    _in_flight.set()
    th = threading.Thread(target=_worker, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        logger.warning("[tdx] 调用超时(>%.1fs),放弃并触发重连", timeout)
        globals()["_api"] = None
        return None
    if "e" in box:
        logger.warning("[tdx] 调用异常: %s", box["e"])
        globals()["_api"] = None
        return None
    return box.get("v")


# ----------------------------- 实时价 -----------------------------
def get_realtime(codes: list[str]) -> dict[str, dict[str, Optional[float]]]:
    """
    批量实时行情。返回 {code: {"price","prev_close","open"}}。
    失败/不支持的代码不会出现在结果中,由上层新浪/东财兜底补全。

    连接与取数均在受墙钟超时保护的 worker 线程内执行,行情主站抖动时
    最多阻塞 timeout+3 秒即放弃,绝不拖挂上层 HTTP 请求。
    """
    if not codes:
        return {}
    pairs = [_resolve_market(c) for c in codes]
    # 过滤通达信不支持的代码,其余保持与原 codes 顺序对应
    valid_pairs = [p for p in pairs if p[0] is not None]
    if not valid_pairs:
        return {}

    def _call():
        api = _guard()  # 连接(可能耗时)放进受超时保护的 worker 内
        if api is None:
            return None
        with _api_lock:
            _throttle()
            return api.get_security_quotes(valid_pairs)

    resp = _run_with_timeout(_call, _cfg["timeout"] + 3)
    if not resp:
        return {}
    out: dict[str, dict[str, Optional[float]]] = {}
    for r in resp:
        code = str(r.get("code", "")).zfill(6)
        # 通达信 get_security_quotes 对 ETF/基金的价格字段单位为「放大 10 倍」(元→角),
        # 对股票则正常。非股票代码(ETF/基金,5/1 开头)统一除以 10 归一。
        # 注:日K/分时用的 get_security_bars 单位正常,无需此修正。
        scale = 1.0 if code[0] in "0234689" else 0.1
        price = _to_float(r.get("price"))
        prev = _to_float(r.get("last_close"))
        open_ = _to_float(r.get("open"))
        out[code] = {
            "price": (price * scale) if price is not None else None,
            "prev_close": (prev * scale) if prev is not None else None,
            "open": (open_ * scale) if open_ is not None else None,
        }
    return out


# ----------------------------- 当天分时 -----------------------------
def get_today_minutes(code: str) -> Optional["Any"]:
    """
    当天 1 分钟 K 线(OHLCV),返回 DataFrame[time,open,high,low,close,volume,amount] 或 None。

    连接与取数均在受墙钟超时保护的 worker 线程内执行。
    """
    import pandas as pd

    market, c = _resolve_market(code)
    if market is None:
        return None

    def _call():
        api = _guard()
        if api is None:
            return None
        with _api_lock:
            _throttle()
            return api.get_security_bars(8, market, c, 0, 240)

    bars = _run_with_timeout(_call, _cfg["timeout"] + 3)
    if not bars:
        return None

    df = pd.DataFrame(bars)
    if "datetime" not in df.columns:
        return None
    df["time"] = pd.to_datetime(df["datetime"], errors="coerce")
    df = df.dropna(subset=["time"])
    today = _dt.datetime.now().date()
    df = df[df["time"].dt.date == today]
    if df.empty:
        return None
    df = df.sort_values("time").reset_index(drop=True)
    out = pd.DataFrame({
        "time": df["time"],
        "open": pd.to_numeric(df["open"], errors="coerce"),
        "high": pd.to_numeric(df["high"], errors="coerce"),
        "low": pd.to_numeric(df["low"], errors="coerce"),
        "close": pd.to_numeric(df["close"], errors="coerce"),
        "volume": pd.to_numeric(df["vol"], errors="coerce"),
        "amount": pd.to_numeric(df["amount"], errors="coerce"),
    })
    return out[["time", "open", "high", "low", "close", "volume", "amount"]]


# ----------------------------- 日 K(后复权) -----------------------------
def get_kline_tdx(code: str, count: int = 800, adjust: str = "raw") -> "Any":
    """
    历史日 K,返回 DataFrame[date,open,high,low,close,volume] 或 None。
    - 单次最多 800 条,按需分页累计到 count。
    - adjust="raw"(默认,未复权,对齐新浪 ETF 缓存); "qfq" 前复权(对齐 A 股缓存); "hfq" 后复权(备用)。
    - adjust != "raw" 时使用 get_xdxr_info 做复权。
    连接与取数(含除权信息)均在受墙钟超时保护的 worker 线程内执行。
    """
    import pandas as pd

    market, c = _resolve_market(code)
    if market is None:
        return None

    def _call():
        api = _guard()
        if api is None:
            return None
        need = int(count)
        bars_all: list[dict] = []
        start = 0
        xdxr: list = []
        with _api_lock:
            while len(bars_all) < need:
                chunk = need - len(bars_all)
                take = min(800, chunk)
                _throttle()
                part = api.get_security_bars(9, market, c, start, take)
                if not part:
                    break
                bars_all.extend(part)
                if len(part) < take:
                    break
                start += take
            if adjust != "raw" and bars_all:
                _throttle()
                xdxr = api.get_xdxr_info(market, c) or []
        return bars_all, xdxr

    res = _run_with_timeout(_call, _cfg["timeout"] + 3)
    if res is None:
        return None
    bars_all, xdxr = res

    if not bars_all:
        return None

    df = pd.DataFrame(bars_all)
    if "datetime" not in df.columns:
        return None
    df["date"] = pd.to_datetime(df["datetime"], errors="coerce")
    df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    if df.empty:
        return None

    df = df.rename(columns={"vol": "volume"})
    # adjust="raw" 表示不调整(直接返回通达信未复权原始价),用于对齐新浪 ETF 缓存语义
    if adjust != "raw":
        df = _adjust(df, xdxr, adjust)
    out = pd.DataFrame({
        "date": df["date"],
        "open": pd.to_numeric(df["open"], errors="coerce"),
        "high": pd.to_numeric(df["high"], errors="coerce"),
        "low": pd.to_numeric(df["low"], errors="coerce"),
        "close": pd.to_numeric(df["close"], errors="coerce"),
        "volume": pd.to_numeric(df["volume"], errors="coerce"),
    })
    return out[["date", "open", "high", "low", "close", "volume"]]


def _adjust(df: "Any", xdxr: list, adjust: str = "hfq") -> "Any":
    """对未复权日K做复权。adjust='hfq' 后复权 / 'qfq' 前复权(基准=最新日)。量保持不变。

    xdxr 为已预取的除权除息信息(由调用方在受超时保护的 worker 内取得)。
    """
    import pandas as pd

    if not xdxr:
        return df  # 无除权事件,原始价即复权价

    # 建立 日期 -> 前收 映射(用于计算理论除权价)
    df = df.copy()
    df["date_only"] = df["date"].dt.date
    prev_close_map = {}
    closes = df["close"].tolist()
    dates = df["date_only"].tolist()
    for i, d in enumerate(dates):
        prev_close_map[d] = closes[i - 1] if i > 0 else closes[i]

    # 事件按日期升序,累计后复权因子
    events = []
    for e in xdxr:
        try:
            d = _dt.date(int(e["year"]), int(e["month"]), int(e["day"]))
        except Exception:
            continue
        fenhong = float(e.get("fenhong") or 0)
        songzhuangu = float(e.get("songzhuangu") or 0)
        peigu = float(e.get("peigu") or 0)
        peigujia = float(e.get("peigujia") or 0)
        events.append((d, fenhong, songzhuangu, peigu, peigujia))
    events.sort(key=lambda x: x[0])

    per_day_factor = {}
    cum = 1.0
    for d, fenhong, songzhuangu, peigu, peigujia in events:
        pc = prev_close_map.get(d)
        if pc is None or pc <= 0:
            continue
        denom = 1.0 + songzhuangu + peigu
        if denom <= 0:
            continue
        xr_price = (pc - fenhong + peigu * peigujia) / denom
        if xr_price <= 0:
            continue
        ratio = pc / xr_price
        cum *= ratio
        per_day_factor[d] = cum

    # 把累积因子映射到每一行(取该行日期对应的最新累积因子)
    factors = [1.0] * len(df)
    running = 1.0
    ev_dates = sorted(per_day_factor.keys())
    ei = 0
    for i, d in enumerate(dates):
        while ei < len(ev_dates) and ev_dates[ei] <= d:
            running = per_day_factor[ev_dates[ei]]
            ei += 1
        factors[i] = running

    last_factor = factors[-1] if factors else 1.0
    for col in ("open", "high", "low", "close"):
        series = pd.to_numeric(df[col], errors="coerce")
        if adjust == "qfq":
            df[col] = series * pd.Series(factors, index=df.index) / last_factor
        else:
            df[col] = series * pd.Series(factors, index=df.index)
    return df


def _to_float(v) -> Optional[float]:
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def health_check() -> dict:
    """返回连通状态,供前端/运维查看。"""
    api = _guard()
    return {
        "enabled": is_enabled(),
        "connected": api is not None,
        "host": _api_host,
    }
