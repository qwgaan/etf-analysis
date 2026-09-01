#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
投资分析 · 数据源层 (akshare_ds)
================================

单一稳定的数据入口，供 financial-expert / stock-advisor / a-share-analyst 调用。

设计约束（来自 ETF 项目实测经验 + 沙箱代理环境）：
  ✅ 行情主源：新浪(Sina)
  ✅ 行情兜底源：通达信(TDX) —— 在线行情(pytdx) + 本地 .day 文件(pytdx.reader)
       · TDX 与 sina/ths 是**完全独立的基础设施**，sina 限流时 TDX 仍可兜底
       · TDX 在线走自己的行情服务器；本地 .day 走离线文件，零网络、零限流
  ✅ 基本面：同花顺(THS)
  ❌ 严禁任何 `_em`（东方财富）函数 —— 沙箱代理会直接封掉 push2.eastmoney.com
  ❌ 不用 tushare —— 需注册 token 且有频限，不如 akshare 即装即用

【关于转换】你最关心的"TDX 值要不要转换"——结论：经过 pytdx 的 API/reader **全自动**，
只有手搓原始 32 字节二进制记录时才需手动 ÷100(A股)/÷1000(ETF)。本层全部走 pytdx，无需手动转换：
  · TDX 在线 get_security_bars：源码 _cal_price1000 已返回「元」
  · TDX 本地 .day 文件：TdxDailyBarReader 按证券类型系数自动反归一化
    （A股系数 0.01→价格÷100，ETF 系数 0.001→价格÷1000，成交量均 ÷100）

【3 源优先级 + 降级】（get_stock_daily / get_etf_daily 内部自动执行）：
  1) 新浪 sina        —— 主力，沙箱/本机均可用
  2) TDX 在线         —— sina 失败/空时启用（需能连到 TDX 行情服务器）
  3) TDX 本地文件     —— 在线也失败时启用（需配置 TDX_VIPDOC 指向 vipdoc 目录）
  4) 历史缓存兜底     —— 前几次成功拉取已落盘 cache/daily_*.csv，最后保底
  任一路成功即返回，并记录实际命中的 source；全部失败才抛异常（依赖方按「数据源降级」处理）。

调用方式：
  1) 作为模块：from akshare_ds import get_stock_daily, get_etf_daily, ...
  2) 作为 CLI  ：python akshare_ds.py daily --symbol 300394 --start 20260801 --json
  3) 带 source ：get_stock_daily_with_source(...) -> (records, source_name)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, date

import akshare as ak
import pandas as pd

# ---------------------------------------------------------------------------
# 工具：代码归一化（akshare 新浪源需要 sh/sz 前缀）
# ---------------------------------------------------------------------------

def _norm_symbol(code: str) -> str:
    """把裸代码转成 akshare 新浪源要求的 sh/sz 前缀形式。

    规则（与 ETF 项目一致）：
      6xxxxx -> sh600000        (上交所股票/基金)
      5xxxxx -> sh510050        (上交所 ETF)
      0/2/3/4/8/9xxxxx -> sz... (深交所股票)
      1xxxxx -> sz...           (深交所 ETF/LOF)
    """
    code = str(code).strip().lower()
    code = code.replace("sz", "").replace("sh", "").replace(".", "")
    if not code.isdigit():
        return code  # 指数等原样返回，由调用方决定
    head = code[0]
    if head == "6" or head == "5":
        return "sh" + code
    return "sz" + code


def _clean(v):
    """把 NaN / NaT / Timestamp 规整成 JSON 友好值。"""
    if v is None:
        return None
    if isinstance(v, (pd.Timestamp, datetime)):
        return v.strftime("%Y-%m-%d")
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(v, (bool,)):
        return v
    if isinstance(v, float) and (v != v):  # NaN
        return None
    return v


def _retry(fn, retries: int = 4, delay: float = 3.0, valid=None):
    """对不稳定的网络源做「重试 + 指数退避 + 抖动」。

    同花顺(THS)源在连续调用时容易被限频/瞬时断连，且限频时往往返回**空 DataFrame
    而非抛异常**——所以除异常外，valid() 判定为无效的空结果也要重试。
    最多重试 retries 次，每次间隔 delay * 2^(n-1) 秒（叠加 0~1s 随机抖动，避免同步限频）。
    """
    import random
    last = None
    if valid is None:
        valid = lambda r: r is not None
    for i in range(retries):
        try:
            r = fn()
            if valid(r):
                return r
            last = RuntimeError("empty/invalid result, retrying")
        except Exception as e:  # 任何网络/解析异常都重试
            last = e
        if i < retries - 1:
            time.sleep(delay * (2 ** i) + random.uniform(0, 1))
    # 全部失败才抛出，让上层按「数据源降级」处理
    raise last


_THS_THROTTLE = {"last": 0.0, "gap": 2.5}


def _ths_throttle():
    """同花顺源连续调用会被限频：保证两次 ths 请求之间至少间隔 gap 秒。"""
    now = time.time()
    wait = _THS_THROTTLE["gap"] - (now - _THS_THROTTLE["last"])
    if wait > 0:
        time.sleep(wait)
    _THS_THROTTLE["last"] = time.time()


def _df_to_records(df: pd.DataFrame) -> list[dict]:
    """DataFrame -> list[dict]，日期转 YYYY-MM-DD，NaN/NaT -> None。"""
    if df is None or len(df) == 0:
        return []
    df = df.copy()
    # 先把 datetime64 列转成字符串，避免 to_dict 后仍是 Timestamp
    for c in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[c]):
            df[c] = df[c].dt.strftime("%Y-%m-%d")
    records = df.where(pd.notnull(df), None).to_dict(orient="records")
    return [{k: _clean(v) for k, v in row.items()} for row in records]


# ---------------------------------------------------------------------------
# 通达信 TDX 相关：服务器列表 / 市场判定 / vipdoc 路径探测
# ---------------------------------------------------------------------------

# 公开 TDX 行情服务器（本机/独立 Docker 可用；沙箱网络策略会拦，属正常降级）
_TDX_SERVERS = [
    ("119.147.212.81", 7709),
    ("49.51.127.38", 7709),
    ("hqpy.xdtz.net", 7709),
    ("hq.lgtz.net", 7709),
    ("112.74.214.23", 7709),
    ("114.215.144.212", 7709),
]

# 常见 TDX 客户端 vipdoc 目录（本地 .day 文件源）。均可被环境变量 TDX_VIPDOC 覆盖。
_TDX_VIPDOC_CANDIDATES = [
    "D:/new_tdx/vipdoc", "C:/new_tdx/vipdoc", "D:/TDX/vipdoc", "C:/TDX/vipdoc",
    "D:/通达信/vipdoc", "F:/TDX/vipdoc", "/app/vipdoc", "/root/.wine/drive_c/new_tdx/vipdoc",
]


def _tdx_market(code: str):
    """返回 (market_int, exchange_prefix)。market: 0=上海 1=深圳。"""
    head = str(code)[0]
    if head in ("5", "6", "9"):
        return 0, "sh"
    return 1, "sz"


def _probe_vipdoc() -> str | None:
    env = os.environ.get("TDX_VIPDOC")
    if env and os.path.isdir(env):
        return env
    for p in _TDX_VIPDOC_CANDIDATES:
        if os.path.isdir(p):
            return p
    return None


# ---------------------------------------------------------------------------
# 每日 K 线：3 源优先级 + 降级 + 缓存兜底
# ---------------------------------------------------------------------------

_DAILY_CANON = ["date", "open", "high", "low", "close", "volume", "amount"]


def _canon_daily(df, start: str = None, end: str = None):
    """把任意源的日线 df 归一化为统一列(date,open,high,low,close,volume,amount)并切片。

    列名兼容中英文（日期/date、开盘/open...）；日期解析兼容 '2026-08-31' 与 20260831；
    空结果返回 None（触发降级到下一源）。
    """
    if df is None or len(df) == 0:
        return None
    df = df.copy()
    ren = {}
    for c in df.columns:
        cl = str(c).strip().lower()
        if cl in ("date", "日期"):
            ren[c] = "date"
        elif cl in ("open", "开盘"):
            ren[c] = "open"
        elif cl in ("high", "最高", "最高价"):
            ren[c] = "high"
        elif cl in ("low", "最低", "最低价"):
            ren[c] = "low"
        elif cl in ("close", "收盘", "收盘价"):
            ren[c] = "close"
        elif cl in ("volume", "成交量", "vol"):
            ren[c] = "volume"
        elif cl in ("amount", "成交额", "amt"):
            ren[c] = "amount"
    if ren:
        df = df.rename(columns=ren)
    if "date" not in df.columns:
        return None
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"])
    if start:
        sd = pd.to_datetime(start, format="%Y%m%d")
        df = df[df["date"] >= sd]
    if end:
        ed = pd.to_datetime(end, format="%Y%m%d")
        df = df[df["date"] <= ed]
    df = df.sort_values("date")
    cols = [c for c in _DAILY_CANON if c in df.columns]
    df = df[cols]
    return df if len(df) else None


def _daily_cache_path(kind: str, symbol: str, adjust: str) -> str:
    d = os.path.join(os.path.dirname(__file__), "..", "cache")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"daily_{kind}_{symbol}_{adjust}.csv")


def _save_daily_cache(kind: str, symbol: str, adjust: str, df: pd.DataFrame):
    try:
        df.to_csv(_daily_cache_path(kind, symbol, adjust), index=False, encoding="utf-8-sig")
    except Exception:
        pass


def _fetch_daily_cache(kind: str, symbol: str, start: str, end: str, adjust: str):
    p = _daily_cache_path(kind, symbol, adjust)
    if not os.path.exists(p):
        raise FileNotFoundError("无历史缓存")
    df = pd.read_csv(p)
    return _canon_daily(df, start, end)


def _fetch_sina_daily(kind: str, symbol: str, start: str, end: str, adjust: str):
    """新浪源（主力）。stock 用 stock_zh_a_daily；etf 用 fund_etf_hist_sina 后本地切片。"""
    if kind == "stock":
        sd = start or "20250101"
        ed = end or date.today().strftime("%Y%m%d")
        return ak.stock_zh_a_daily(symbol=_norm_symbol(symbol),
                                   start_date=sd, end_date=ed, adjust=adjust or "qfq")
    # ETF：新浪源一次返回全量，本地按日期切片，避免多次网络请求
    return ak.fund_etf_hist_sina(symbol=_norm_symbol(symbol))


def _fetch_tdx_online_daily(kind: str, symbol: str, start: str, end: str, adjust: str):
    """TDX 在线行情（pytdx，独立基础设施）。注意：返回**不复权**原始价。"""
    try:
        from pytdx.hq import TdxHq_API
    except Exception as e:
        raise RuntimeError("pytdx 未安装: " + str(e))
    code = _norm_symbol(symbol).lstrip("shsz")
    market, _ = _tdx_market(code)
    last = None
    for host, port in _TDX_SERVERS:
        api = None
        try:
            api = TdxHq_API(raise_exception=True)
            if not api.connect(host, port, time_out=4):
                last = RuntimeError("connect=false")
                continue
            raw = api.get_security_bars(9, market, code, 0, 800)  # 9=daily, 取最近 800 根
            if raw is None or len(raw) == 0:
                last = RuntimeError("empty bars")
                continue
            df = raw.rename(columns={c: str(c).lower() for c in raw.columns})
            return df
        except Exception as e:
            last = e
        finally:
            try:
                if api is not None:
                    api.disconnect()
            except Exception:
                pass
    raise last or RuntimeError("无可达的 TDX 行情服务器")


def _fetch_tdx_local_daily(kind: str, symbol: str, start: str, end: str, adjust: str):
    """TDX 本地 .day 文件（pytdx.reader，离线零限流）。需配置 TDX_VIPDOC。"""
    try:
        from pytdx.reader import TdxDailyBarReader
    except Exception as e:
        raise RuntimeError("pytdx 未安装: " + str(e))
    vipdoc = _probe_vipdoc()
    if not vipdoc:
        raise RuntimeError("未配置 TDX vipdoc 路径(设置 TDX_VIPDOC 或放到常见目录)")
    code = _norm_symbol(symbol).lstrip("shsz")
    _, exchange = _tdx_market(code)
    reader = TdxDailyBarReader(vipdoc)
    df = reader.get_df_by_code(code, exchange)  # 列: open/high/low/close/amount/volume, index=datetime
    df = df.reset_index()
    df = df.rename(columns={df.columns[0]: "date"})
    return df


def _resolve_daily(kind: str, symbol: str, start: str = "20250101",
                   end: str = None, adjust: str = "qfq"):
    """3 源优先级 + 降级。返回 (canon_df, source_name)。"""
    fetchers = [
        ("sina",      lambda: _fetch_sina_daily(kind, symbol, start, end, adjust)),
        ("tdx_online", lambda: _fetch_tdx_online_daily(kind, symbol, start, end, adjust)),
        ("tdx_local",  lambda: _fetch_tdx_local_daily(kind, symbol, start, end, adjust)),
        ("cache",      lambda: _fetch_daily_cache(kind, symbol, start, end, adjust)),
    ]
    errors = []
    for name, fn in fetchers:
        try:
            raw = fn()
            df = _canon_daily(raw, start, end)
            if df is not None and len(df):
                _save_daily_cache(kind, symbol, adjust, df)
                return df, name
        except Exception as e:
            errors.append(f"{name}: {e}")
    raise RuntimeError("所有数据源均失败 -> " + " | ".join(errors))


def get_stock_daily(symbol: str, start_date: str = "20250101",
                    end_date: str = None, adjust: str = "qfq") -> list[dict]:
    """A 股日线（前复权/后复权/不复权）。内部自动 3 源降级：新浪→TDX在线→TDX本地→缓存。

    symbol: 裸代码或 sh/sz 前缀，如 300394 / sz300394
    adjust: qfq(前复权,默认) / hfq(后复权) / ""(不复权)
            注：TDX 兜底源返回不复权原始价，复权语义以新浪为准。
    """
    df, _ = _resolve_daily("stock", symbol, start_date, end_date, adjust)
    return _df_to_records(df)


def get_stock_daily_with_source(symbol: str, start_date: str = "20250101",
                                end_date: str = None, adjust: str = "qfq"):
    """同 get_stock_daily，但额外返回命中的 source 名称。"""
    df, source = _resolve_daily("stock", symbol, start_date, end_date, adjust)
    return _df_to_records(df), source


def get_etf_daily(symbol: str, start_date: str = "20250101",
                  end_date: str = None, adjust: str = "") -> list[dict]:
    """ETF 历史 K 线。内部自动 3 源降级：新浪→TDX在线→TDX本地→缓存。

    symbol: 裸代码或 sh/sz 前缀，如 159915 / sz159915
    """
    df, _ = _resolve_daily("etf", symbol, start_date, end_date, adjust or "")
    return _df_to_records(df)


def get_etf_daily_with_source(symbol: str, start_date: str = "20250101",
                              end_date: str = None, adjust: str = ""):
    """同 get_etf_daily，但额外返回命中的 source 名称。"""
    df, source = _resolve_daily("etf", symbol, start_date, end_date, adjust or "")
    return _df_to_records(df), source


# ---------------------------------------------------------------------------
# 实时快照 / 选股（新浪源，稳定但全市场较慢，建议缓存）
# ---------------------------------------------------------------------------

_SPOT_CACHE = os.path.join(os.path.dirname(__file__), "..", "cache", "spot.csv")


def get_spot(use_cache: bool = True, cache_path: str = None) -> list[dict]:
    """全 A 实时快照（新浪源）。约 5000+ 行，较慢(~30s)，默认落盘缓存。"""
    cache_path = cache_path or _SPOT_CACHE
    if use_cache and os.path.exists(cache_path):
        try:
            df = pd.read_csv(cache_path)
            return _df_to_records(df)
        except Exception:
            pass
    df = ak.stock_zh_a_spot()
    os.makedirs(os.path.dirname(os.path.abspath(cache_path)), exist_ok=True)
    df.to_csv(cache_path, index=False, encoding="utf-8-sig")
    return _df_to_records(df)


def search_stocks(keyword: str, use_cache: bool = True) -> list[dict]:
    """按代码或名称关键字筛选股票（基于新浪全市场快照）。"""
    df = pd.DataFrame(get_spot(use_cache=use_cache))
    if df.empty:
        return []
    kw = str(keyword).lower()
    mask = df.astype(str).apply(lambda r: r.str.lower().str.contains(kw).any(), axis=1)
    return _df_to_records(df[mask].head(50))


def resolve_name(code: str) -> str:
    """按代码反查股票名称（基于全市场快照缓存，失败返回空字符串）。"""
    try:
        recs = get_spot(use_cache=True)
        code = str(code).strip().upper()
        for r in recs:
            c = str(r.get("代码") or r.get("code") or "").upper()
            if c == code:
                return r.get("名称") or r.get("name") or ""
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# 基本面（同花顺 THS 源，稳定；东财 em 被沙箱封禁，勿用）
# ---------------------------------------------------------------------------

def get_financial_abstract(symbol: str, indicator: str = "按年度") -> list[dict]:
    """财务摘要（同花顺）。indicator: 按年度 / 按报告期。返回按报告期降序（最新在前）。"""
    _ths_throttle()
    df = _retry(lambda: ak.stock_financial_abstract_ths(
        symbol=_norm_symbol(symbol).lstrip("shsz"), indicator=indicator),
        valid=lambda d: d is not None and len(d) > 0)
    if df is not None and len(df) and "报告期" in df.columns:
        df = df.sort_values("报告期", ascending=False)
    return _df_to_records(df)


def get_financial_indicators(symbol: str, start_year: str = "2015") -> list[dict]:
    """财务指标（同花顺）。含偿债/营运/盈利/成长等 50+ 维度。返回按日期降序（最新在前）。"""
    _ths_throttle()
    df = _retry(lambda: ak.stock_financial_analysis_indicator(
        symbol=_norm_symbol(symbol).lstrip("shsz"), start_year=start_year),
        valid=lambda d: d is not None and len(d) > 0)
    if df is not None and len(df) and "日期" in df.columns:
        df = df.sort_values("日期", ascending=False)
    return _df_to_records(df)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli():
    p = argparse.ArgumentParser(description="投资分析数据源层 (akshare + tdx)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_sym(sp): sp.add_argument("--symbol", required=True)
    def add_dates(sp):
        sp.add_argument("--start", default="20250101")
        sp.add_argument("--end", default=None)
        sp.add_argument("--adjust", default=None)
    def add_verbose(sp): sp.add_argument("--verbose", action="store_true")

    sp = sub.add_parser("daily"); add_sym(sp); add_dates(sp); add_verbose(sp)
    sp = sub.add_parser("etf");    add_sym(sp); add_dates(sp); add_verbose(sp)
    sp = sub.add_parser("spot")
    sp = sub.add_parser("search"); sp.add_argument("--keyword", required=True)
    sp = sub.add_parser("fin");    add_sym(sp); sp.add_argument("--indicator", default="按年度")
    sp = sub.add_parser("ind");    add_sym(sp); sp.add_argument("--start-year", default="2015")

    args = p.parse_args()
    out = []
    src = None
    if args.cmd == "daily":
        if args.verbose:
            out, src = get_stock_daily_with_source(args.symbol, args.start, args.end, args.adjust or "qfq")
        else:
            out = get_stock_daily(args.symbol, args.start, args.end, args.adjust or "qfq")
    elif args.cmd == "etf":
        if args.verbose:
            out, src = get_etf_daily_with_source(args.symbol, args.start, args.end, args.adjust or "")
        else:
            out = get_etf_daily(args.symbol, args.start, args.end, args.adjust or "")
    elif args.cmd == "spot":
        out = get_spot()
    elif args.cmd == "search":
        out = search_stocks(args.keyword)
    elif args.cmd == "fin":
        out = get_financial_abstract(args.symbol, args.indicator)
    elif args.cmd == "ind":
        out = get_financial_indicators(args.symbol, args.start_year)

    if args.cmd in ("daily", "etf") and args.verbose:
        print(f"# source: {src}", file=sys.stderr)
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    _cli()
