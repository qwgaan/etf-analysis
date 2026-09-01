#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""投资分析批量跑批 + 单只任务封装。

说明：
  - _run_single_locked / _run_batch_locked 由 invest/jobs.py 在后台线程调用。
  - 单只直接复用 engine/pipeline.run_single（已含取数→计算→证据→LLM→组装→渲染）。
  - 批量串行执行，每只要么成功返回结果 dict，要么记录 error，互不影响。
  - 批量结束产出一张对比汇总表（综合分 / 建议 / 仓位上限 / 模块来源）。
"""
from __future__ import annotations

import os
from datetime import date

from invest import jobs, paths
from invest.engine import pipeline


def _run_single_locked(job: dict, code: str, name: str, options: dict) -> dict:
    """单只任务：持有全局 akshare 锁调用 run_single，回传结果 dict。"""
    lock = jobs.akshare_lock()

    def on_stage(i, total, label):
        jobs.set_stage(job, i, total, label)

    def on_progress(msg):
        jobs.append_log(job, msg)

    result = pipeline.run_single(
        code, name,
        profile=options.get("profile"),
        start=options.get("start"),
        div=options.get("div"),
        use_llm=options.get("use_llm", True),
        use_web=options.get("use_web", True),
        use_search=options.get("use_search", True),
        fresh=options.get("fresh", False),
        suffix=options.get("suffix", ""),
        on_stage=on_stage,
        on_progress=on_progress,
        akshare_lock=lock,
    )
    jobs.set_progress(job, 100)
    return result


def _run_batch_locked(job: dict, items: list, options: dict) -> dict:
    """批量任务：串行跑每只，产出对比汇总。

    items: [(code, name), ...]
    """
    total = max(1, len(items))
    results = []
    for idx, (code, name) in enumerate(items):
        jobs.set_progress(job, int(idx / total * 100))
        jobs.append_log(job, f"▶ 第 {idx + 1}/{total} 只：{code} {name}")
        try:
            res = _run_single_locked(job, code, name, options)
            results.append(res)
            jobs.append_log(
                job,
                f"  ✓ {code} {name} 完成 · 综合分 {res.get('adj_score')} · "
                f"{res.get('stance')} · 来源 {res.get('m3_source')}",
            )
        except Exception as e:  # noqa: BLE001
            results.append({"code": code, "name": name, "error": str(e)})
            jobs.append_log(job, f"  ✗ {code} {name} 失败：{str(e)[:200]}")

    summary = _build_summary(results, options)
    jobs.set_progress(job, 100)
    jobs.append_log(job, f"批量完成：成功 {summary['ok']} / 共 {summary['total']}")
    return {"items": results, "summary": summary}


def _build_summary(results: list, options: dict) -> dict:
    """把每只结果聚合成对比表（按综合分降序）。"""
    rows = []
    for r in results:
        if r.get("error"):
            rows.append({
                "code": r.get("code"), "name": r.get("name"),
                "error": r["error"], "ok": False,
            })
            continue
        rows.append({
            "code": r.get("code"),
            "name": r.get("name"),
            "close": r.get("close"),
            "adj_score": r.get("adj_score"),
            "base_avg": r.get("base_avg"),
            "weights": r.get("weights"),
            "stance": r.get("stance"),
            "position": r.get("position"),
            "m3_source": r.get("m3_source"),
            "evidence_used": r.get("evidence_used"),
            "html_rel": r.get("html_rel"),
            "html_size": r.get("html_size"),
            "ok": True,
        })
    rows.sort(key=lambda x: (x.get("adj_score") or -1), reverse=True)
    ok = sum(1 for r in rows if r.get("ok"))
    return {
        "total": len(results),
        "ok": ok,
        "failed": len(results) - ok,
        "use_llm": options.get("use_llm", True),
        "use_web": options.get("use_web", True),
        "rows": rows,
        "date": date.today().isoformat(),
    }


def parse_items(text: str):
    """解析批量输入框：每行 `代码 名称`，名称可省略（用空字符串占位）。"""
    items = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        code = parts[0]
        name = parts[1].strip() if len(parts) > 1 else ""
        if code:
            items.append((code, name))
    return items
