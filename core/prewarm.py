"""
后台预热任务管理(线程安全,Web 和 CLI 共用)。
提供:
- start_preheat(limit, years, sleep): 启动后台预热线程(幂等,重复调用无副作用)
- get_status(): 返回当前进度 dict
- is_running(): 是否在运行

预热结果实时写入 disk cache,中断后下次启动可续传。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from . import data_source as ds


@dataclass
class PreheatStatus:
    running: bool = False
    total: int = 0
    done: int = 0
    ok: int = 0
    fail: int = 0
    current: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    error: str = ""
    years: int = 3
    added: int = 0  # 实际新增缓存数
    initial_cached: set = field(default_factory=set)


_lock = threading.Lock()
_thread: threading.Thread | None = None
_status = PreheatStatus()


def is_running() -> bool:
    with _lock:
        return _status.running


def get_status() -> dict:
    with _lock:
        s = _status
        elapsed = (time.time() - s.started_at) if s.started_at else 0.0
        if s.running:
            elapsed_str = f"{elapsed:.0f}s"
        elif s.finished_at and s.started_at:
            elapsed_str = f"{s.finished_at - s.started_at:.0f}s"
        else:
            elapsed_str = "—"
        return {
            "running": s.running,
            "total": s.total,
            "done": s.done,
            "ok": s.ok,
            "fail": s.fail,
            "current": s.current,
            "years": s.years,
            "elapsed": elapsed_str,
            "started_at": s.started_at,
            "finished_at": s.finished_at,
            "error": s.error,
            "added": s.added,
        }


def start_preheat(limit: int = 0, years: int = 3, sleep: float = 0.05) -> dict:
    """
    启动后台预热。已运行时直接返回当前状态,不重复启动。
    参数:
        limit: 0=全市场,>0=前 N 只
        years: 回看年数
        sleep: 每次请求间隔(秒)
    """
    with _lock:
        if _status.running:
            return {"ok": False, "reason": "已在运行中", "status": get_status()}

    # 在新线程里跑预热
    def _run():
        try:
            pool = ds.list_etfs()
            codes = pool["code"].tolist()
            if limit > 0:
                codes = codes[: limit]
            initial_cached = set(ds.list_cached_codes())
            codes_to_fetch = [c for c in codes if c not in initial_cached]

            with _lock:
                _status.running = True
                _status.total = len(codes_to_fetch)
                _status.done = 0
                _status.ok = 0
                _status.fail = 0
                _status.current = ""
                _status.started_at = time.time()
                _status.finished_at = 0.0
                _status.error = ""
                _status.years = years
                _status.added = 0
                _status.initial_cached = initial_cached

            t0 = time.time()
            for i, code in enumerate(codes_to_fetch, 1):
                with _lock:
                    _status.current = code
                try:
                    df = ds.fetch_kline(code, years=years)
                    with _lock:
                        if df.empty:
                            _status.fail += 1
                        else:
                            _status.ok += 1
                except Exception as e:
                    with _lock:
                        _status.fail += 1
                        _status.error = str(e)[:120]
                with _lock:
                    _status.done = i
                if sleep > 0:
                    time.sleep(sleep)

            cached_after = set(ds.list_cached_codes())
            with _lock:
                _status.added = len(cached_after - initial_cached)
                _status.running = False
                _status.finished_at = time.time()
        except Exception as e:
            with _lock:
                _status.running = False
                _status.error = str(e)[:200]
                _status.finished_at = time.time()

    t = threading.Thread(target=_run, daemon=True, name="preheat")
    t.start()
    with _lock:
        global _thread
        _thread = t
    return {"ok": True, "status": get_status()}