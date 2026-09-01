#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""投资分析模块的异步任务管理。

设计要点（与 ETF 主体隔离，不依赖 app.py）：
  - 单只/批量跑批都在后台线程执行，不阻塞 waitress（waitress threads=4）。
  - 全局互斥锁 _AKSHARE_LOCK 串行化所有人对 akshare 的访问，避免互相踩限流
    （批量本身串行，锁主要防止多个并发单只任务 + ETF 后台任务叠加打爆接口）。
  - 每个任务有 job_id，前端轮询 /api/invest/jobs/<id> 取进度（百分比 + 日志流 + 状态）。
  - 任务状态全部存内存（JOBS 字典）；进程重启即清空，符合「分析任务一次性」语义。
"""
from __future__ import annotations

import threading
import uuid
import time
import traceback
from datetime import date

# 全局互斥锁：串行化 akshare 访问
_AKSHARE_LOCK = threading.Lock()

# 任务存储：job_id -> dict
JOBS: dict = {}
_JOBS_LOCK = threading.Lock()

# 最大保留任务数，防止内存无限增长
_MAX_JOBS = 50


def _now() -> float:
    return time.time()


def _new_job(kind: str, meta: dict) -> str:
    jid = uuid.uuid4().hex[:12]
    with _JOBS_LOCK:
        JOBS[jid] = {
            "id": jid,
            "kind": kind,                 # "single" | "batch"
            "status": "running",         # running | done | error
            "progress": 0,               # 0~100
            "stage": "",
            "total": meta.get("total", 1),
            "done": 0,
            "log": [],
            "created": _now(),
            "finished": None,
            "result": None,              # single: dict | batch: {items, summary}
            "error": None,
            "meta": meta,
        }
        # 超出上限时丢弃最旧的任务
        if len(JOBS) > _MAX_JOBS:
            oldest = sorted(JOBS.items(), key=lambda kv: kv[1]["created"])[:len(JOBS) - _MAX_JOBS]
            for old_id, _ in oldest:
                JOBS.pop(old_id, None)
    return jid


def _append_log(job: dict, msg: str) -> None:
    job["log"].append(msg)
    # 只保留最近 600 行，避免内存膨胀
    if len(job["log"]) > 600:
        job["log"] = job["log"][-600:]


def get_job(jid: str):
    with _JOBS_LOCK:
        return JOBS.get(jid)


def append_log(job: dict, msg: str) -> None:
    """向任务追加一行日志（线程安全）。"""
    with _JOBS_LOCK:
        _append_log(job, msg)


def set_stage(job: dict, i: int, total: int, label: str) -> None:
    """更新阶段标签与进度（取数/计算占 0~70，证据/LLM 占 70~95）。"""
    with _JOBS_LOCK:
        job["stage"] = label
        job["progress"] = min(95, int((i - 1) / total * 70))


def set_progress(job: dict, pct: int) -> None:
    """直接设置进度百分比（取较大值，避免回退）。"""
    with _JOBS_LOCK:
        job["progress"] = max(job["progress"], min(100, int(pct)))


def list_jobs(limit: int = 20) -> list:
    with _JOBS_LOCK:
        items = sorted(JOBS.values(), key=lambda j: j["created"], reverse=True)
    return items[:limit]


def _run_in_background(jid: str, target, *args, **kwargs) -> None:
    def _wrapper():
        job = get_job(jid)
        if job is None:
            return
        try:
            result = target(job, *args, **kwargs)
            with _JOBS_LOCK:
                job["status"] = "done"
                job["progress"] = 100
                job["result"] = result
                job["finished"] = _now()
        except Exception as e:  # noqa: BLE001
            with _JOBS_LOCK:
                job["status"] = "error"
                job["error"] = str(e)
                job["log"].append("任务异常:\n" + traceback.format_exc()[-1500:])
                job["finished"] = _now()
    t = threading.Thread(target=_wrapper, daemon=True)
    t.start()


def start_single(code: str, name: str, options: dict) -> str:
    """启动单只跑批，返回 job_id。"""
    from invest import batch as batch_mod
    meta = {"code": code, "name": name, "options": options, "total": 1}
    jid = _new_job("single", meta)
    _run_in_background(jid, batch_mod._run_single_locked, code, name, options)
    return jid


def start_batch(items: list, options: dict) -> str:
    """启动批量跑批，items = [(code, name), ...]，返回 job_id。"""
    from invest import batch as batch_mod
    meta = {"count": len(items), "total": len(items), "items": items, "options": options}
    jid = _new_job("batch", meta)
    _run_in_background(jid, batch_mod._run_batch_locked, items, options)
    return jid


def akshare_lock():
    """暴露全局锁，供 run_single 串行化 akshare 调用。"""
    return _AKSHARE_LOCK
