"""
技术指标模块
- MA(N): N日简单移动平均
- BIAS(N): N日乖离率 = (close - MA(N)) / MA(N) * 100
- drawdown_ytd: 年度最大回撤(每年初清零,公众号文章 2 用法)
- period_high_low: 区间内最高/最低(用于 52周 / 今年内 选项)
- distance_to_high/low: 距区间高/低点的百分比距离
- ma200_slope: MA200 在最近 M 日内的最小二乘斜率,用于判断"持续上升"

设计原则:
1. 全部接受 pandas Series/DataFrame,返回 Series 或 dict,便于链式处理。
2. 任何指标值未达到最小样本数时,标注为 NaN,绝不假装算出来。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# ---------- 基础均线 ----------
def ma(series: pd.Series, n: int) -> pd.Series:
    """N 日简单移动平均。样本不足 N 日时为 NaN。"""
    return series.rolling(window=n, min_periods=n).mean()


def ema(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(span=n, adjust=False, min_periods=n).mean()


# ---------- BIAS ----------
def bias(close: pd.Series, n: int) -> pd.Series:
    """N 日乖离率(%),形如 BIAS20 / BIAS60。
    公众号文章 1 中 BIAS20>10% / >15% 减仓,BIAS60>20% 再减仓。
    """
    m = ma(close, n)
    return (close - m) / m * 100.0


# ---------- 年度最大回撤 ----------
def drawdown_ytd(close: pd.Series) -> pd.Series:
    """年度最大回撤(从年初清零)系列。
    每个自然年内,series 累计记录从年初最高点起的回撤百分比(负值)。
    公众号 2: '每年初清零,只算当年的数据',避免 2015 行情那种长期压制。
    """
    if close.empty:
        return close
    year_start_max = close.groupby(close.index.year).cummax()
    dd = (close - year_start_max) / year_start_max * 100.0
    return dd


def drawdown_ytd_current(close: pd.Series) -> float:
    """当前时点的年度最大回撤(单值,负数或 0)。
    也返回年内到目前为止的最高点位置,便于 UI 展示。
    """
    if close.empty:
        return float("nan")
    year_start_max = close.groupby(close.index.year).cummax()
    dd = (close - year_start_max) / year_start_max * 100.0
    return float(dd.iloc[-1])


# ---------- 区间高低点(52周 / 今年) ----------
def period_high(close: pd.Series, window: int | None = None) -> pd.Series:
    """滚动窗口最大值。window=None 表示截至当前的全部历史最大值。"""
    if window is None:
        return close.cummax()
    return close.rolling(window=window, min_periods=1).max()


def period_low(close: pd.Series, window: int | None = None) -> pd.Series:
    if window is None:
        return close.cummin()
    return close.rolling(window=window, min_periods=1).min()


def distance_to_high(close: pd.Series, window: int | None = None) -> pd.Series:
    """距区间最高点的距离(%,负数)。close 已经接近高点时接近 0。"""
    h = period_high(close, window)
    return (close - h) / h * 100.0


def distance_to_low(close: pd.Series, window: int | None = None) -> pd.Series:
    """距区间最低点的距离(%,正数)。close 比最低点高越多越大。"""
    lo = period_low(close, window)
    return (close - lo) / lo * 100.0


# ---------- MA200 斜率(用于"持续上升"判定) ----------
def ma200_slope(close: pd.Series, lookback: int = 20) -> pd.Series:
    """MA200 在最近 lookback 日内的最小二乘斜率(每日一个值)。
    斜率 > 0 视为"上升中"。etfwin: '200日均线保持上升趋势至少 1 个月'。
    """
    ma200 = ma(close, 200)
    slope = pd.Series(index=close.index, dtype="float64")
    vals = ma200.values
    n = len(vals)
    if n < lookback:
        return slope
    xs = np.arange(lookback, dtype="float64")
    xs_mean = xs.mean()
    denom = ((xs - xs_mean) ** 2).sum()
    for i in range(lookback - 1, n):
        seg = vals[i - lookback + 1 : i + 1]
        # NaN 处理:整段都有效才计算
        if np.isnan(seg).any():
            slope.iat[i] = np.nan
            continue
        y_mean = seg.mean()
        num = ((xs - xs_mean) * (seg - y_mean)).sum()
        slope.iat[i] = num / denom if denom != 0 else 0.0
    return slope


# ---------- 工具 ----------
def safe_last(series: pd.Series) -> float:
    """取 series 最后一个值,空/全 NaN 时返回 NaN。"""
    if series is None or len(series) == 0:
        return float("nan")
    v = series.iloc[-1]
    if pd.isna(v):
        return float("nan")
    return float(v)


def last_n_business_days(df_or_index, n: int) -> int:
    """最近 n 个交易日内,给定 df/index 的有效行数下限检查辅助。"""
    if hasattr(df_or_index, "__len__"):
        return len(df_or_index)
    return 0
