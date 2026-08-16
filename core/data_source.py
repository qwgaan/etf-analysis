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
from concurrent.futures import ThreadPoolExecutor
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
            "缺少依赖 akshare。请在当前 Python 环境中运行:\n"
            "  python -m pip install akshare\n"
            "(或 pip install -r requirements.txt)"
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


# ------------- 股票 vs ETF 判断 -------------
def is_stock_code(code: str) -> bool:
    """判断 6 位代码是否为 A 股股票(非 ETF/基金)。

    沪市 ETF 5xxxxx / 深市 ETF(含 LOF/REIT)1xxxxx;其余 0/2/3/4/6/8/9 开头视为股票。
    """
    c = str(code).zfill(6)
    return c[0] in "0234689"


def _stock_sina_symbol(code: str) -> str:
    """6 位 A 股代码 -> 新浪 symbol(sh/sz/bj 前缀)。"""
    c = str(code).zfill(6)
    if c[0] in "69":      # 沪市 60/68/69/90
        return "sh" + c
    if c[0] in "48":      # 北交所 43/83/87/88/92
        return "bj" + c
    return "sz" + c       # 深市 0/2/3


# ------------- A 股股票列表(仅名称/代码,用于搜索与名称解析;不做历史下载) -------------
def list_stocks(force_refresh: bool = False) -> pd.DataFrame:
    """返回 columns = [code, name] 的全市场 A 股列表。

    优先读本地缓存 data/stock_list.csv;没有或 force_refresh 时拉一次并缓存。
    数据源优先级:
    1. 新浪 stock_zh_a_spot(稳定,沙箱可用;代码带 sh/sz/bj 前缀,需剥离)
    2. 东财 stock_zh_a_spot_em(兜底;部分网络环境可用)

    注意:这只下载「名称/代码」截面,不下载任何历史 K 线,符合「股票不批量下载历史」的需求。
    """
    cache = PROJECT_ROOT / "data" / "stock_list.csv"
    if cache.exists() and not force_refresh:
        try:
            df = pd.read_csv(cache, dtype={"code": str})
            if not df.empty:
                return df
        except Exception:
            pass

    ak = None
    try:
        ak = _akshare_safe_import()
    except Exception:
        pass

    def _norm(df: pd.DataFrame) -> pd.DataFrame:
        df = df.rename(columns={"代码": "code", "名称": "name", "股票代码": "code", "股票简称": "name"})
        df = df[["code", "name"]].copy()
        # 剥离交易所前缀(sh/sz/bj),仅保留 6 位数字
        df["code"] = df["code"].astype(str).str.extract(r"(\d{6})", expand=False).fillna("").str.zfill(6)
        df = df[df["code"].str.fullmatch(r"\d{6}")]
        df["name"] = df["name"].astype(str)
        df = df.drop_duplicates("code").reset_index(drop=True)
        return df

    df = pd.DataFrame()
    if ak is not None:
        # 1) 新浪(稳定)
        try:
            df = _norm(ak.stock_zh_a_spot())
        except Exception:
            df = pd.DataFrame()
        # 2) 东财兜底
        if df.empty:
            try:
                df = _norm(ak.stock_zh_a_spot_em())
            except Exception:
                df = pd.DataFrame()

    if not df.empty:
        cache.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(cache, index=False)
        return df

    if cache.exists():
        try:
            return pd.read_csv(cache, dtype={"code": str})
        except Exception:
            pass
    return pd.DataFrame(columns=["code", "name"])


def search_stocks(q: str, limit: int = 15) -> list[dict]:
    """按代码前缀或名称关键字搜索 A 股,供前端自选输入下拉。返回 [{code, name}]。"""
    q = (q or "").strip().lower()
    if not q:
        return []
    df = list_stocks()
    if df.empty:
        return []
    if q.isdigit():
        matched = df[df["code"].astype(str).str.startswith(q)]
    else:
        matched = df[df["name"].astype(str).str.lower().str.contains(q, na=False)]
    return [{"code": str(r["code"]), "name": str(r["name"])} for _, r in matched.head(limit).iterrows()]


def resolve_name(code: str) -> str:
    """解析单个代码的名称:股票走 list_stocks,ETF 走 list_etfs;失败回退为代码本身。"""
    c = str(code).zfill(6)
    try:
        pool = list_stocks() if is_stock_code(c) else list_etfs()
        row = pool[pool["code"].astype(str).str.zfill(6) == c]
        if not row.empty:
            return str(row.iloc[0]["name"])
    except Exception:
        pass
    return c


def resolve_names(codes: list[str]) -> dict[str, str]:
    """批量解析名称(股票/ETF 分开查,只各自加载一次列表)。返回 {code: name}。"""
    codes = [str(c).zfill(6) for c in codes]
    out: dict[str, str] = {}
    for c in codes:
        out[c] = c
    stock_codes = [c for c in codes if is_stock_code(c)]
    etf_codes = [c for c in codes if not is_stock_code(c)]
    for kind_codes, loader in ((stock_codes, list_stocks), (etf_codes, list_etfs)):
        if not kind_codes:
            continue
        try:
            pool = loader()
            m = dict(zip(pool["code"].astype(str).str.zfill(6), pool["name"].astype(str)))
            for c in kind_codes:
                if c in m:
                    out[c] = m[c]
        except Exception:
            pass
    return out


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
    # 股票走独立源(新浪 stock_zh_a_daily / 东财 stock_zh_a_hist)
    if is_stock_code(code):
        return fetch_stock_kline(code, years=years)
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


# ------------- 单只 A 股 K 线 -------------
def fetch_stock_kline(code: str, years: int = 3, adjust: str = "qfq") -> pd.DataFrame:
    """获取单只 A 股历史日 K(前复权),带磁盘缓存。

    数据源优先级:
    1. 新浪 stock_zh_a_daily(稳定,不受东财限流,数据更久)
    2. 东财 stock_zh_a_hist(兜底,复权可选,支持北交所)

    缓存复用 `{code}_hfq.csv` 命名(与 ETF 一致,便于 list_cached_codes 统一识别)。
    """
    code = str(code).zfill(6)
    cache_path = CACHE_DIR / f"{code}_hfq.csv"
    if cache_path.exists():
        try:
            df = pd.read_csv(cache_path, parse_dates=["date"])
            df = df.set_index("date").sort_index()
            if not df.empty:
                if (time.time() - cache_path.stat().st_mtime) < 24 * 3600:
                    return df
        except Exception:
            pass

    ak = None
    try:
        ak = _akshare_safe_import()
    except Exception:
        pass

    df = pd.DataFrame()
    # 1) 新浪源优先(稳定)
    if ak is not None:
        try:
            sym = _stock_sina_symbol(code)
            raw = ak.stock_zh_a_daily(
                symbol=sym, start_date=_years_ago_str(years), end_date=_today_str(), adjust=adjust,
            )
            df = _normalize_kline(raw)
        except Exception:
            df = pd.DataFrame()
        if not df.empty:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            df.to_csv(cache_path, index_label="date")
            return df

    # 2) 东财源兜底(北交所 / 新浪失败时)
    if ak is not None:
        try:
            raw = ak.stock_zh_a_hist(
                symbol=code, period="daily",
                start_date=_years_ago_str(years), end_date=_today_str(), adjust=adjust,
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
            return pd.read_csv(cache_path, parse_dates=["date"]).set_index("date").sort_index()
        except Exception:
            pass
    return pd.DataFrame()


def fetch_stock_kline_full(code: str, adjust: str = "qfq") -> pd.DataFrame:
    """获取单只 A 股上市以来的完整历史 K 线(不截断),带独立 `{code}_full.csv` 缓存。"""
    code = str(code).zfill(6)
    cache_path = CACHE_DIR / f"{code}_full.csv"
    if cache_path.exists():
        try:
            df = pd.read_csv(cache_path, parse_dates=["date"]).set_index("date").sort_index()
            if not df.empty:
                if (time.time() - cache_path.stat().st_mtime) < 7 * 24 * 3600:
                    return df
        except Exception:
            pass

    ak = None
    try:
        ak = _akshare_safe_import()
    except Exception:
        pass

    df = pd.DataFrame()
    if ak is not None:
        try:
            sym = _stock_sina_symbol(code)
            raw = ak.stock_zh_a_daily(symbol=sym, adjust=adjust)  # 不传日期=全量
            df = _normalize_kline(raw)
        except Exception:
            df = pd.DataFrame()

    if df.empty:
        # 兜底:用默认 N 年缓存
        fallback = fetch_stock_kline(code, years=20)
        if not fallback.empty:
            df = fallback

    if not df.empty:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(cache_path, index_label="date")
        return df

    if cache_path.exists():
        try:
            return pd.read_csv(cache_path, parse_dates=["date"]).set_index("date").sort_index()
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


def fetch_kline_full(code: str) -> pd.DataFrame:
    """获取单只 ETF 上市以来的完整历史 K 线(不截断),带独立磁盘缓存。

    优先读本地 `{code}_full.csv` 缓存;没有则通过网络拉取全部历史后缓存。
    与默认 3 年缓存独立,避免互相覆盖。
    """
    code = str(code).zfill(6)
    if is_stock_code(code):
        return fetch_stock_kline_full(code)
    cache_path = CACHE_DIR / f"{code}_full.csv"
    if cache_path.exists():
        try:
            df = pd.read_csv(cache_path, parse_dates=["date"])
            df = df.set_index("date").sort_index()
            if not df.empty:
                mtime = cache_path.stat().st_mtime
                # 全量缓存 7 天刷新一次即可
                if (time.time() - mtime) < 7 * 24 * 3600:
                    return df
        except Exception:
            pass

    ak = None
    try:
        ak = _akshare_safe_import()
    except Exception:
        pass

    df = pd.DataFrame()
    # 新浪源返回全量历史,不截断
    if ak is not None:
        try:
            sym = _code_to_sina_symbol(code)
            raw = ak.fund_etf_hist_sina(symbol=sym)
            df = _normalize_kline(raw)
        except Exception:
            df = pd.DataFrame()

    # 兜底:用默认的 3 年缓存(可能不是完整上市历史)
    if df.empty:
        fallback = fetch_kline(code, years=20)
        if not fallback.empty:
            df = fallback

    if not df.empty:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(cache_path, index_label="date")
        return df

    # 一切失败时尝试旧全量缓存
    if cache_path.exists():
        try:
            return pd.read_csv(cache_path, parse_dates=["date"]).set_index("date").sort_index()
        except Exception:
            pass
    return pd.DataFrame()


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
def attach_klines(pool_df: pd.DataFrame, years: int = 3,
                  max_workers: int = 8,
                  progress_cb=None) -> pd.DataFrame:
    """把每只 ETF 的 K 线作为 'kline' 列挂到 pool_df 上,方便筛选模块直接吃。

    已缓存的 ETF 用线程池并发读本地 CSV,避免 1500+ 文件串行等待;
    未缓存的 ETF 单独串行拉取,避免集中并发网络请求被数据源限流。
    progress_cb(done, total):每读一只回调一次,用于前端进度展示。
    """
    pool_df = pool_df.copy()
    codes = pool_df["code"].astype(str).str.zfill(6).tolist()
    cached = set(list_cached_codes())
    total = len(codes)

    # 1) 并发读已缓存的本地 K 线
    def _get_cached(c: str) -> pd.DataFrame | None:
        if c not in cached:
            return None
        return fetch_kline(c, years=years)

    klines: list[pd.DataFrame | None] = [None] * len(codes)
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for i, df in enumerate(ex.map(_get_cached, codes)):
            klines[i] = df
            if progress_cb:
                progress_cb(i + 1, total)

    # 2) 未缓存的单独串行拉取(网络友好)
    for i, c in enumerate(codes):
        if klines[i] is None:
            klines[i] = fetch_kline(c, years=years)
            if progress_cb:
                progress_cb(i + 1, total)

    pool_df["kline"] = klines
    return pool_df


if __name__ == "__main__":
    # 命令行直接跑: 快速验证拉取
    df = list_etfs()
    print(f"ETF 列表共 {len(df)} 条")
    print(df.head().to_string(index=False))
    sample = df.iloc[0]["code"]
    k = fetch_kline(sample)
    print(f"\n{sample} K线 {len(k)} 行,最近:\n{k.tail().to_string()}")
