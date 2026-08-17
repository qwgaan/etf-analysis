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


def is_trade_day(d: dt.date | None = None, holidays: list[str] | None = None) -> bool:
    """判断是否为交易日:工作日且不在 holidays(YYYY-MM-DD 字符串列表)中。"""
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
