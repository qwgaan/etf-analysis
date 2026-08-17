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

import datetime as dt
import re
import urllib.error
import urllib.request
from typing import Any

import akshare as ak
import pandas as pd

from . import data_source as ds


SINA_HQ_URL = "https://hq.sinajs.cn/list={}"


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


def fetch_realtime_prices(codes: list[str], timeout: float = 10.0) -> dict[str, float | None]:
    """
    批量查询新浪实时行情,返回 {code: price}。

    - 价格取最新成交价(parts[3])。
    - 未开盘、停牌或接口异常时,对应 code 返回 None。
    - 只要有一个 code 查询成功,就不会抛异常,失败项以 None 表示。
    """
    if not codes:
        return {}

    codes = [str(c).zfill(6) for c in codes]
    sina_codes = [_sina_list_code(c) for c in codes]
    url = SINA_HQ_URL.format(",".join(sina_codes))

    try:
        req = urllib.request.Request(url)
        # 新浪行情接口需要 Referer,否则可能返回空
        req.add_header("Referer", "https://finance.sina.com.cn")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("gbk", errors="ignore")
    except urllib.error.HTTPError as e:
        print(f"[intraday] HTTP {e.code}: 实时行情查询失败")
        return {c: None for c in codes}
    except Exception as e:
        print(f"[intraday] 实时行情查询失败: {e}")
        return {c: None for c in codes}

    result: dict[str, float | None] = {c: None for c in codes}
    for line in text.splitlines():
        m = re.match(r'var hq_str_([a-z]{2})(\d{6})="([^"]*)"', line.strip())
        if not m:
            continue
        _, code, data = m.groups()
        parts = data.split(",")
        # 新浪返回字段: 0=name,1=open,2=yesterday_close,3=current/last,...
        if len(parts) < 4:
            continue
        try:
            price = float(parts[3])
            if price <= 0:
                continue
            result[code] = price
        except (ValueError, IndexError):
            continue

    return result


def fetch_realtime_prices_once(codes: list[str]) -> dict[str, float | None]:
    """兼容别名,语义同 fetch_realtime_prices。"""
    return fetch_realtime_prices(codes)


def fetch_realtime_quotes(codes: list[str], timeout: float = 10.0) -> dict[str, dict[str, float | None]]:
    """
    批量查询新浪实时行情,返回更完整字段。

    返回结构: {code: {"price": 最新价, "prev_close": 昨收, "open": 今开}}
    失败/停牌项对应字段为 None,不会抛异常。
    """
    result: dict[str, dict[str, float | None]] = {
        c: {"price": None, "prev_close": None, "open": None} for c in codes
    }
    if not codes:
        return result

    codes = [str(c).zfill(6) for c in codes]
    sina_codes = [_sina_list_code(c) for c in codes]
    url = SINA_HQ_URL.format(",".join(sina_codes))

    try:
        req = urllib.request.Request(url)
        req.add_header("Referer", "https://finance.sina.com.cn")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("gbk", errors="ignore")
    except Exception as e:
        print(f"[intraday] 实时行情详情查询失败: {e}")
        return result

    for line in text.splitlines():
        m = re.match(r'var hq_str_([a-z]{2})(\d{6})="([^"]*)"', line.strip())
        if not m:
            continue
        _, code, data = m.groups()
        parts = data.split(",")
        # 字段: 0=name,1=open,2=yesterday_close,3=current/last,...
        if len(parts) < 4:
            continue
        try:
            price = float(parts[3]) if float(parts[3]) > 0 else None
            prev_close = float(parts[2]) if float(parts[2]) > 0 else None
            open_ = float(parts[1]) if float(parts[1]) > 0 else None
            result[code] = {"price": price, "prev_close": prev_close, "open": open_}
        except (ValueError, IndexError):
            continue
    return result


def fetch_today_kline(code: str, date: dt.date | None = None) -> pd.DataFrame | None:
    """
    拉取指定代码当天 1 分钟 K 线。

    - ETF 走 fund_etf_hist_min_em, A 股走 stock_zh_a_hist_min_em(均来自东方财富);
    - 若东方财富接口异常,回退到新浪 stock_zh_a_minute(可能仅返回最近约 2000 根)。
    - 只返回指定日期的分钟 bar;非交易日/无数据返回 None。
    - 列名: time, open, high, low, close, volume, amount。
    """
    code = str(code).zfill(6)
    date = date or dt.date.today()
    date_str = date.strftime("%Y-%m-%d")
    start_dt = f"{date_str} 09:30:00"
    end_dt = f"{date_str} 17:00:00"

    df: pd.DataFrame | None = None
    try:
        if ds.is_stock_code(code):
            df = ak.stock_zh_a_hist_min_em(symbol=code, period="1", adjust="", start_date=start_dt, end_date=end_dt)
        else:
            df = ak.fund_etf_hist_min_em(symbol=code, period="1", adjust="", start_date=start_dt, end_date=end_dt)
    except Exception as e:
        print(f"[intraday] {code} 东财当天分时拉取失败,尝试新浪: {e}")

    if df is None or df.empty:
        # 新浪回退
        try:
            df = ak.stock_zh_a_minute(symbol=_sina_list_code(code), period="1", adjust="")
        except Exception as e2:
            print(f"[intraday] {code} 新浪当天分时拉取失败: {e2}")
            return None

    if df is None or df.empty:
        return None

    # 统一列名(东财: 时间/成交额; 新浪: day/amount)
    if "时间" in df.columns:
        df = df.rename(columns={"时间": "time"})
    elif "day" in df.columns:
        df = df.rename(columns={"day": "time"})
    else:
        return None

    if "成交额" in df.columns:
        df = df.rename(columns={"成交额": "amount"})

    # 若仍无成交额,用 close*volume 近似(不影响主图,仅用于均价线)
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
