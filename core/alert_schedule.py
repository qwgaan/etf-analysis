"""
自选 ETF 警戒定时自动推送调度器。

- 仅在「交易日」运行(周一~周五,且不在 holidays 列表)。
- 在配置 alert_schedule 指定的时间(默认 10:00 / 13:30 / 16:00)各扫描一次。
- 到点后回调 run_callback()(由 app.py 注入,执行真正的订阅扫描 + WxPusher 推送)。
- 每次(日期, 时间)组合只触发一次;配合 alert.py 的「当天已推送」去重,避免重复打扰。
"""
from __future__ import annotations

import datetime as dt
import threading
import time

# ---------------------------------------------------------------------------
# A 股法定休市日(工作日里的休市,不含周六周日)。
#
# 为什么内置:交易所休市日(春节/国庆/中秋等)在「工作日」里,单靠 weekday()<5 判断不出来。
# 若不剔除,程序会认为「今天/该日应有 K 线」,而行情源根本不会返回该日数据,
# 于是缓存永远被判为「不新鲜」→ 每个代码反复重抓全部数据源 → 日志刷屏且页面数据陈旧。
# 用户配置 alert_holidays 仍可追加/覆盖(见 merge_holidays)。
#
# 来源:沪深北交易所公告。2026 年见上证公告〔2026〕22 号等。
# ---------------------------------------------------------------------------
BUILTIN_HOLIDAYS: dict[int, list[str]] = {
    2026: [
        # 元旦 1/1(四)~1/3(六)  (1/2 为工作日休市)
        "2026-01-01", "2026-01-02",
        # 春节 2/15(日)~2/23(一) (2/16~2/20、2/23 为工作日休市)
        "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20", "2026-02-23",
        # 清明 4/4(六)~4/6(一)   (4/6 为工作日休市)
        "2026-04-06",
        # 劳动节 5/1(五)~5/5(二) (5/1、5/4、5/5 为工作日休市)
        "2026-05-01", "2026-05-04", "2026-05-05",
        # 端午 6/19(五)~6/21(日) (6/19 为工作日休市)
        "2026-06-19",
        # 中秋 9/25(五)~9/27(日) (9/25 为工作日休市)
        "2026-09-25",
        # 国庆 10/1(四)~10/7(三) (10/1、10/2、10/5、10/6、10/7 为工作日休市)
        "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07",
    ],
}


def builtin_holidays_for(year: int) -> list[str]:
    """返回内置的某年休市日列表(YYYY-MM-DD);未收录的年份返回空列表。"""
    return list(BUILTIN_HOLIDAYS.get(year, []))


def merge_holidays(user_holidays: list[str] | None, years: tuple[int, ...] | None = None) -> list[str]:
    """把内置休市日与用户自定义 holiday 合并去重。

    years 为 None 时,取内置表里所有年份(当前仅 2026)。用户配置优先级不冲突——
    合并是并集,用户只需追加交易所临时休市(如台风停市)。
    """
    out: set[str] = set()
    if years is None:
        for ys in BUILTIN_HOLIDAYS.values():
            out.update(ys)
    else:
        for y in years:
            out.update(builtin_holidays_for(y))
    for h in (user_holidays or []):
        if isinstance(h, str) and h.strip():
            out.add(h.strip())
    return sorted(out)


def is_trade_day(d: dt.date | None = None, holidays: list[str] | None = None) -> bool:
    """判断是否为交易日:工作日且不在 holidays(YYYY-MM-DD 字符串列表)中。

    注意:传入的 holidays 应已包含内置休市日(用 merge_holidays 合成)。
    若只想用内置表判断,传 None 也可——此时会退化为「仅按星期判断」以保持向后兼容,
    因此**推荐调用方显式传 merge_holidays(...)**。
    """
    d = d or dt.date.today()
    if d.weekday() >= 5:  # 5=周六, 6=周日
        return False
    if holidays:
        if d.isoformat() in set(holidays):
            return False
    return True


def parse_times(times: list) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for t in (times or []):
        try:
            hh, mm = str(t).split(":")
            out.append((int(hh), int(mm)))
        except Exception:
            continue
    return out


class AlertScheduler:
    """后台守护线程:周期性检查是否到达配置中的推送时间点。

    可选支持「提前预热」:
    - pre_warm_callback 会在每个配置时间点前 pre_warm_minutes 分钟触发一次;
    - 用于盘中实时价警戒场景,提前拉取行情,减少正式推送时阻塞。
    """

    def __init__(
        self,
        run_callback,
        interval: int = 30,
        pre_warm_callback=None,
        pre_warm_minutes: int = 3,
    ):
        self.run_callback = run_callback
        self.pre_warm_callback = pre_warm_callback
        self.pre_warm_minutes = pre_warm_minutes
        self.interval = interval
        self._running = False
        self._last_keys: set[str] = set()
        # 记录每个时间点预热已调用到的分钟,避免同一分钟内重复触发
        self._pre_warm_last: dict[tuple[int, int], int] = {}
        self._thread: threading.Thread | None = None

    def _check(self, get_schedule, get_holidays) -> None:
        schedule = parse_times(get_schedule())
        if not schedule:
            return
        now = dt.datetime.now()
        d = now.date()
        if not is_trade_day(d, get_holidays()):
            return

        # 1) 正式触发
        key = f"{d.isoformat()} {now.hour:02d}:{now.minute:02d}"
        if (now.hour, now.minute) in schedule and key not in self._last_keys:
            self._last_keys.add(key)
            try:
                self.run_callback()
            except Exception as e:  # 调度的异常不应拖垮主线程
                print(f"[alert-scheduler] 执行回调异常: {e}")
            return

        # 2) 提前预热(仅在交易时段内)
        if not self.pre_warm_callback:
            return
        for hh, mm in schedule:
            target = dt.datetime.combine(d, dt.time(hh, mm))
            warm_start = target - dt.timedelta(minutes=self.pre_warm_minutes)
            if not (warm_start <= now < target):
                continue
            # 同一分钟内只触发一次(因为 interval 可能小于 60s)
            last_min = self._pre_warm_last.get((hh, mm), -1)
            if now.minute == last_min:
                continue
            self._pre_warm_last[(hh, mm)] = now.minute
            try:
                self.pre_warm_callback((hh, mm))
            except Exception as e:
                print(f"[alert-scheduler] 预热回调异常: {e}")
            break

    def _tick(self, get_schedule, get_holidays) -> None:
        while self._running:
            try:
                self._check(get_schedule, get_holidays)
            except Exception:
                pass
            time.sleep(self.interval)

    def start(self, get_schedule, get_holidays) -> None:
        """启动调度线程。get_schedule/get_holidays 为无参可调用,每次 tick 实时读取最新配置。"""
        if self._running:
            return
        self._running = True
        self._get_schedule = get_schedule
        self._get_holidays = get_holidays
        self._thread = threading.Thread(
            target=self._tick, args=(get_schedule, get_holidays), daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False
