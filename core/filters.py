"""
Mark Minervini 趋势模板(参考 etfwin.com)的判定函数。
每条规则对应 etfwin "收窄筛选逻辑" 中的一格,全部可阈值化。

输入:DataFrame 至少有 close 列(index 为日期),以及按需计算后的均线/高低点 Series。
输出:每个函数返回 (passed: bool, reason: str)

四条规则:
1. 均线多头排列:
   close > MA50 > MA150 > MA200
   (etfwin 写:"当前价格和 50 日均线均高于 150 日和 200 日均线,且 MA50>MA150>MA200。
   一个简洁的强弱近似。")
2. MA200 持续上升:MA200 最近 N 日最小二乘斜率 > 0
3. 远离 52 周低点:close 距 52 周最低点的涨幅 >= 阈值(默认 25%)
4. 接近 52 周新高:close 距 52 周最高点的跌幅 <= 阈值(默认 25%)
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from . import indicators as ind


@dataclass
class FilterConfig:
    """4 条 Mark 模板规则的可调阈值。所有阈值可由前端改写后存到 user.json。"""

    # 规则 1:均线多头排列
    rule1_enabled: bool = True
    rule1_strict_alignment: bool = True  # True 要求 MA50>MA150>MA200;False 仅要求 close>MA200

    # 规则 2:MA200 持续上升
    rule2_enabled: bool = True
    rule2_lookback: int = 20  # 用最近 20 日 (≈ 1 个月交易日) 算斜率
    rule2_min_slope: float = 0.0  # 斜率必须 > 此值;0 = 严格向上

    # 规则 3:远离 52 周低点
    rule3_enabled: bool = True
    rule3_window_weeks: int = 52
    rule3_min_distance_pct: float = 25.0

    # 规则 4:接近 52 周新高
    rule4_enabled: bool = True
    rule4_window_weeks: int = 52
    rule4_max_distance_pct: float = 25.0

    # 规则 5:远离今年低点(年初至今最低点)
    rule5_enabled: bool = True
    rule5_min_distance_pct: float = 15.0  # 现价距年内最低点的涨幅 ≥ 阈值

    # 规则 6:接近今年高点(年初至今最高点)
    rule6_enabled: bool = True
    rule6_max_distance_pct: float = 25.0  # 现价距年内最高点的回撤 ≤ 阈值

    @property
    def enabled_rules(self) -> list[int]:
        """返回当前启用的规则编号列表 [1..6]。"""
        out = []
        if self.rule1_enabled: out.append(1)
        if self.rule2_enabled: out.append(2)
        if self.rule3_enabled: out.append(3)
        if self.rule4_enabled: out.append(4)
        if self.rule5_enabled: out.append(5)
        if self.rule6_enabled: out.append(6)
        return out

    @property
    def enabled_count(self) -> int:
        return len(self.enabled_rules)


@dataclass
class FilterResult:
    code: str
    name: str
    close: float
    ma50: float
    ma150: float
    ma200: float
    rule1: tuple[bool, str]
    rule2: tuple[bool, str]
    rule3: tuple[bool, str]
    rule4: tuple[bool, str]
    rule5: tuple[bool, str]
    rule6: tuple[bool, str]
    bias20: float
    bias60: float
    ytd_drawdown: float
    dd52w: float

    @property
    def passed_count(self) -> int:
        return sum(int(p[0]) for p in (self.rule1, self.rule2, self.rule3,
                                       self.rule4, self.rule5, self.rule6))

    @property
    def fully_passed(self) -> bool:
        return all(p[0] for p in (self.rule1, self.rule2, self.rule3,
                                    self.rule4, self.rule5, self.rule6))


def _to_pct(x: float) -> str:
    return "n/a" if pd.isna(x) else f"{x:.2f}%"


def evaluate_one(
    code: str,
    name: str,
    df: pd.DataFrame,
    cfg: FilterConfig,
) -> FilterResult:
    """对单个 ETF 应用所有规则,返回结果对象。"""
    close = df["close"]
    ma50 = ind.ma(close, 50)
    ma150 = ind.ma(close, 150)
    ma200 = ind.ma(close, 200)
    bias20 = ind.bias(close, 20)
    bias60 = ind.bias(close, 60)

    last_close = ind.safe_last(close)
    last_ma50 = ind.safe_last(ma50)
    last_ma150 = ind.safe_last(ma150)
    last_ma200 = ind.safe_last(ma200)

    # ---- 规则 1:均线多头排列 ----
    if cfg.rule1_enabled:
        if cfg.rule1_strict_alignment:
            ok = (
                not pd.isna(last_close)
                and not pd.isna(last_ma50)
                and not pd.isna(last_ma150)
                and not pd.isna(last_ma200)
                and last_close > last_ma50 > last_ma150 > last_ma200
            )
            reason = (
                f"close={last_close:.3f} > MA50={last_ma50:.3f} > "
                f"MA150={last_ma150:.3f} > MA200={last_ma200:.3f}" if ok
                else f"未完全多头 close={last_close:.3f} MA50={last_ma50:.3f} "
                     f"MA150={last_ma150:.3f} MA200={last_ma200:.3f}"
            )
        else:
            ok = (
                not pd.isna(last_close)
                and not pd.isna(last_ma200)
                and last_close > last_ma200
            )
            reason = (
                f"close {last_close:.3f} 高于 MA200 {last_ma200:.3f}" if ok
                else f"close {last_close:.3f} 未高于 MA200 {last_ma200:.3f}"
            )
    else:
        ok = True
        reason = "已关闭"

    r1 = (ok, reason)

    # ---- 规则 2:MA200 持续上升 ----
    if cfg.rule2_enabled:
        last_slope = ind.ma200_slope_last(close, lookback=cfg.rule2_lookback)
        if pd.isna(last_slope):
            ok = False
            reason = f"MA200 样本不足 {cfg.rule2_lookback} 日"
        else:
            ok = last_slope > cfg.rule2_min_slope
            reason = f"MA200 近 {cfg.rule2_lookback} 日斜率={last_slope:.4f} (阈值 {cfg.rule2_min_slope})"
    else:
        ok = True
        reason = "已关闭"

    r2 = (ok, reason)

    # ---- 规则 3:远离 52 周低点(默认 25%) ----
    if cfg.rule3_enabled:
        days = cfg.rule3_window_weeks * 5  # 52 周 ≈ 260 个交易日
        dist_low = ind.distance_to_low(close, window=days)
        last_dl = ind.safe_last(dist_low)
        if pd.isna(last_dl):
            ok = False
            reason = "数据不足"
        else:
            ok = last_dl >= cfg.rule3_min_distance_pct
            reason = f"距 {cfg.rule3_window_weeks} 周低点 {_to_pct(last_dl)} (阈值 ≥{cfg.rule3_min_distance_pct:.0f}%)"
    else:
        ok = True
        reason = "已关闭"

    r3 = (ok, reason)

    # ---- 规则 4:接近 52 周新高(默认 25%) ----
    if cfg.rule4_enabled:
        days = cfg.rule4_window_weeks * 5
        dist_hi = ind.distance_to_high(close, window=days)
        last_dh = ind.safe_last(dist_hi)
        if pd.isna(last_dh):
            ok = False
            reason = "数据不足"
        else:
            ok = last_dh >= -cfg.rule4_max_distance_pct  # 距高点回撤 <=25%,即 dist >= -25%
            reason = f"距 {cfg.rule4_window_weeks} 周高点 {_to_pct(last_dh)} (阈值 ≥-{cfg.rule4_max_distance_pct:.0f}%)"
    else:
        ok = True
        reason = "已关闭"

    r4 = (ok, reason)

    # ---- 规则 5:远离今年低点(年初至今最低点,默认 ≥15%) ----
    if cfg.rule5_enabled:
        dist_y_low = ind.distance_to_year_low(close)
        last_dyl = ind.safe_last(dist_y_low)
        if pd.isna(last_dyl):
            ok = False
            reason = "数据不足"
        else:
            ok = last_dyl >= cfg.rule5_min_distance_pct
            reason = f"距今年低点 {_to_pct(last_dyl)} (阈值 ≥{cfg.rule5_min_distance_pct:.0f}%)"
    else:
        ok = True
        reason = "已关闭"
    r5 = (ok, reason)

    # ---- 规则 6:接近今年高点(年初至今最高点,默认回撤 ≤25%) ----
    if cfg.rule6_enabled:
        dist_y_hi = ind.distance_to_year_high(close)
        last_dyh = ind.safe_last(dist_y_hi)
        if pd.isna(last_dyh):
            ok = False
            reason = "数据不足"
        else:
            ok = last_dyh >= -cfg.rule6_max_distance_pct
            reason = f"距今年高点 {_to_pct(last_dyh)} (阈值 ≥-{cfg.rule6_max_distance_pct:.0f}%)"
    else:
        ok = True
        reason = "已关闭"
    r6 = (ok, reason)

    return FilterResult(
        code=code,
        name=name,
        close=last_close,
        ma50=last_ma50,
        ma150=last_ma150,
        ma200=last_ma200,
        rule1=r1,
        rule2=r2,
        rule3=r3,
        rule4=r4,
        rule5=r5,
        rule6=r6,
        bias20=ind.safe_last(bias20),
        bias60=ind.safe_last(bias60),
        ytd_drawdown=ind.drawdown_ytd_current(close),
        dd52w=ind.drawdown_52w_current(close),
    )


def evaluate_pool(pool_df: pd.DataFrame, cfg: FilterConfig | None = None,
                  progress_cb=None) -> list[FilterResult]:
    """
    pool_df:必须包含列 [code, name],以及一个 'kline' 列存该 ETF 的 K 线 DataFrame
    (由 data_source 填充)。其余可不带。
    cfg=None 时使用默认 FilterConfig。
    progress_cb(done, total):每处理一只回调一次,用于前端进度展示。
    """
    if cfg is None:
        cfg = FilterConfig()
    out: list[FilterResult] = []
    total = len(pool_df)
    done = 0
    for _, row in pool_df.iterrows():
        df = row.get("kline")
        done += 1
        if df is None or df.empty or len(df) < 60:
            if progress_cb:
                progress_cb(done, total)
            continue
        out.append(evaluate_one(str(row["code"]), str(row["name"]), df, cfg))
        if progress_cb:
            progress_cb(done, total)
    return out
