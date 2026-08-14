"""
数据源模块
- ETF 列表: 优先问财技能(skill iwencai),失败则用 AKShare `fund_etf_spot_em` 全市场快照。
- 历史 K 线: AKShare `fund_etf_hist_em`,带磁盘缓存(按 code 分文件)。
- 网络失败/超时时,使用空 DataFrame + warning,不抛异常阻挡 UI。

为什么不全用问财:问财只能拿到截面(最新价),没有历史 K 线,
而 BIAS/回撤/均线/52周高/低全部依赖时间序列。

接口一览:
- list_etfs() -> pd.DataFrame[code, name]
- fetch_kline(code, days) -> pd.DataFrame[date, open, high, low, close, volume]
- ensure_klines(codes, days) -> dict[code, df], 批量预热
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = PROJECT_ROOT / "data" / "klines"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# 让 akshare 缓存目录也被重定向到本地
os.environ.setdefault("AKSHARE_DATA_DIR", str(PROJECT_ROOT / "data" / "akshare"))


# ------------- 工具 -------------
def _today_str() -> str:
    return time.strftime("%Y%m%d")


def _years_ago_str(years: int) -> str:
    t = time.localtime()
    y = t.tm_year - years
    return f"{y}{t.tm_mon:02d}{t.tm_mday:02d}"


def _akshare_safe_import():
    """懒加载 akshare,避免硬依赖让 UI 启动失败。"""
    try:
        import akshare as ak
        return ak
    except ImportError as e:
        raise ImportError(
            "缺少依赖 akshare。请运行:\n"
            "  C:\\Users\\admin\\.workbuddy\\binaries\\python\\envs\\default\\Scripts\\python.exe "
            "-m pip install akshare"
        ) from e


# ------------- ETF 列表 -------------
def list_etfs(force_refresh: bool = False) -> pd.DataFrame:
    """
    返回 columns = [code, name],code 为 6 位字符串(不带交易所后缀)。

    优先数据源顺序:
    1. 问财: "全部 ETF 基金" -- 由 skill 调用方提供 csv,然后 cache 到 data/etf_list.csv
    2. AKShare: akshare.fund_etf_spot_em() -- 全市场实时快照,提取代码/名称
    """
    cache = PROJECT_ROOT / "data" / "etf_list.csv"
    if cache.exists() and not force_refresh:
        try:
            df = pd.read_csv(cache, dtype={"code": str})
            if not df.empty:
                return df
        except Exception:
            pass

    try:
        ak = _akshare_safe_import()
        df = ak.fund_etf_spot_em()
        # 该接口返回字段含 '代码' 和 '名称';有时是 '基金代码' / '基金简称'
        rename_map = {
            "代码": "code", "基金代码": "code",
            "名称": "name", "基金简称": "name", "ETF简称": "name",
        }
        df = df.rename(columns=rename_map)
        if "code" not in df.columns or "name" not in df.columns:
            raise RuntimeError(f"AKShare 返回字段没有 code/name: {df.columns.tolist()}")
        df = df[["code", "name"]].copy()
        df["code"] = df["code"].astype(str).str.zfill(6)
        df["name"] = df["name"].astype(str)
        df = df.drop_duplicates("code").reset_index(drop=True)
        cache.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(cache, index=False)
        return df
    except Exception as e:
        # 网络失败的兜底:用启动时缓存的 csv 或内置的迷你列表
        if cache.exists():
            return pd.read_csv(cache, dtype={"code": str})
        return _fallback_mini_pool()


def _fallback_mini_pool() -> pd.DataFrame:
    """完全没数据时的兜底迷你列表(覆盖主要宽基 + 行业),仅用于启动期 UI 不空白。"""
    rows = [
        ("510300", "华泰柏瑞沪深300ETF"),
        ("510500", "南方中证500ETF"),
        ("510050", "华夏上证50ETF"),
        ("588000", "华夏科创50ETF"),
        ("159915", "易方达创业板ETF"),
        ("159919", "嘉实沪深300ETF"),
        ("512100", "南方中证1000ETF"),
        ("512880", "国泰证券ETF"),
        ("510880", "华泰柏瑞红利ETF"),
        ("518880", "华安黄金ETF"),
        ("511880", "银华货币ETF"),
        ("513050", "易方达中概互联ETF"),
        ("513100", "国泰纳斯达克100ETF"),
        ("159941", "广发纳指ETF"),
        ("513500", "博时标普500ETF"),
        ("513880", "华泰柏瑞日经225ETF"),
    ]
    df = pd.DataFrame(rows, columns=["code", "name"])
    df.to_csv(PROJECT_ROOT / "data" / "etf_list.csv", index=False)
    return df


# ------------- 单只 ETF K 线 -------------
def _code_to_sina_symbol(code: str) -> str:
    """6 位代码 -> 新浪 symbol(sh/sz 前缀)。5 开头=上交所,1 开头=深交所。"""
    c = str(code).zfill(6)
    return ("sh" if c.startswith("5") else "sz") + c


def _normalize_kline(df: pd.DataFrame) -> pd.DataFrame:
    """把不同数据源返回的 K 线统一成 [open, high, low, close, volume] 的 date 索引。"""
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    # 东财源:中文列名;新浪源:英文列名
    rename = {
        "日期": "date", "开盘": "open", "收盘": "close",
        "最高": "high", "最低": "low", "成交量": "volume",
    }
    df = df.rename(columns=rename)
    if "date" not in df.columns:
        return pd.DataFrame()
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    for col in ("open", "high", "low", "close", "volume"):
        if col not in df.columns:
            df[col] = pd.NA
    return df[["open", "high", "low", "close", "volume"]].astype(float)


def fetch_kline(code: str, years: int = 3, adjust: str = "hfq") -> pd.DataFrame:
    """
    获取单只 ETF 历史 K 线;带磁盘缓存。

    数据源优先级:
    1. 新浪 fund_etf_hist_sina(稳定、不受东财限流,数据更久)
    2. 东财 fund_etf_hist_em(兜底,复权可选)

    参数:
        code: 6 位 ETF 代码,如 '510300'
        years: 回看年数(默认 3)
        adjust: 复权方式(仅东财源生效)
    """
    code = str(code).zfill(6)
    # 缓存 key 不区分源,统一 hfq 命名以复用旧缓存
    cache_path = CACHE_DIR / f"{code}_hfq.csv"
    if cache_path.exists():
        try:
            df = pd.read_csv(cache_path, parse_dates=["date"])
            df = df.set_index("date").sort_index()
            if not df.empty:
                mtime = cache_path.stat().st_mtime
                if (time.time() - mtime) < 24 * 3600:
                    return df
        except Exception:
            pass

    df = pd.DataFrame()
    ak = None
    try:
        ak = _akshare_safe_import()
    except Exception:
        pass

    # 1) 新浪源优先(稳定,不触发东财限流)
    if ak is not None:
        try:
            sym = _code_to_sina_symbol(code)
            raw = ak.fund_etf_hist_sina(symbol=sym)
            df = _normalize_kline(raw)
        except Exception:
            df = pd.DataFrame()
        if not df.empty:
            # 截断到回看年数
            cutoff = pd.Timestamp.now() - pd.Timedelta(days=365 * years)
            df = df[df.index >= cutoff]
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            df.to_csv(cache_path, index_label="date")
            return df

    # 2) 东财源兜底(带复权,但可能被限流)
    if ak is not None:
        try:
            start = _years_ago_str(years)
            end = _today_str()
            raw = ak.fund_etf_hist_em(
                symbol=code, period="daily",
                start_date=start, end_date=end, adjust=adjust,
            )
            df = _normalize_kline(raw)
        except Exception:
            df = pd.DataFrame()
        if not df.empty:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            df.to_csv(cache_path, index_label="date")
            time.sleep(0.05)
            return df

    # 3) 全部失败 -> 旧缓存兜底
    if cache_path.exists():
        try:
            df = pd.read_csv(cache_path, parse_dates=["date"]).set_index("date").sort_index()
            return df
        except Exception:
            pass
    return pd.DataFrame()


def list_cached_codes() -> list[str]:
    """返回本地已有 K 线缓存的 ETF 代码(6 位)。用于秒级全市场筛选。"""
    codes: set[str] = set()
    if not CACHE_DIR.exists():
        return []
    for p in CACHE_DIR.glob("*.csv"):
        # 文件名形如 510300_hfq.csv
        stem = p.stem
        code = stem.split("_")[0]
        if code.isdigit() and len(code) == 6:
            codes.add(code)
    return sorted(codes)


def batch_fetch_klines(codes: list[str], years: int = 3, progress_cb=None) -> dict[str, pd.DataFrame]:
    """批量拉取,带简易进度回调 progress_cb(done, total)。"""
    out: dict[str, pd.DataFrame] = {}
    total = len(codes)
    for i, c in enumerate(codes, 1):
        out[c] = fetch_kline(c, years=years)
        if progress_cb:
            try:
                progress_cb(i, total)
            except Exception:
                pass
    return out


# ------------- 给 Flask 路由层用的辅助 -------------
def attach_klines(pool_df: pd.DataFrame, years: int = 3) -> pd.DataFrame:
    """把每只 ETF 的 K 线作为 'kline' 列挂到 pool_df 上,方便筛选模块直接吃。"""
    pool_df = pool_df.copy()
    pool_df["kline"] = pool_df["code"].map(lambda c: fetch_kline(str(c).zfill(6), years=years))
    return pool_df


if __name__ == "__main__":
    # 命令行直接跑: 快速验证拉取
    df = list_etfs()
    print(f"ETF 列表共 {len(df)} 条")
    print(df.head().to_string(index=False))
    sample = df.iloc[0]["code"]
    k = fetch_kline(sample)
    print(f"\n{sample} K线 {len(k)} 行,最近:\n{k.tail().to_string()}")
