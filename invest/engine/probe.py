#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""取数探针（原 _probe.py CLI，已重构为函数）。

原实现是一个 argv 驱动的脚本，落盘到 tools/probe_{code}.json；
重构后为 probe_stock(code) -> dict，落盘路径由 invest.paths 统一给出。
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from invest import paths
from invest.engine import akshare_ds as ds


def default_start(days: int = 500) -> str:
    """默认取数起点：约 500 个自然日之前，够算 MA60 + 52 周高低。"""
    return (date.today() - timedelta(days=days)).strftime("%Y%m%d")


def probe_stock(code: str, start: str | None = None, use_cache: bool = True,
                on_progress=None) -> dict:
    """拉取单只股票的日K + 财务摘要 + 财务指标，返回原始 dict 并落盘。

    use_cache=True 时，若本地已有当天生成的 probe 文件则直接复用，
    避免批量跑批时对同一只股票反复打接口触发限流。
    """
    def _p(msg):
        if on_progress:
            try:
                on_progress(msg)
            except Exception:
                pass

    paths.ensure_dirs()
    out_path = paths.probe_path(code)
    start = start or default_start()

    if use_cache and os.path.exists(out_path):
        try:
            cached = json.load(open(out_path, encoding="utf-8"))
            if cached.get("daily") and cached.get("_start") == start:
                _p(f"取数命中本地缓存（{len(cached['daily'])} 根日K）")
                return cached
        except Exception:
            pass

    out: dict = {"code": code, "_start": start}

    _p("取数 1/3：日K 行情")
    try:
        recs, src = ds.get_stock_daily_with_source(code, start_date=start, adjust="qfq")
        out["daily_source"] = src
        out["daily_count"] = len(recs)
        out["daily"] = recs
        _p(f"  日K {len(recs)} 根（源={src}）")
    except Exception as e:
        out["daily_error"] = repr(e)
        _p(f"  日K 取数失败：{str(e)[:120]}")

    _p("取数 2/3：财务摘要（按年度）")
    try:
        fin = ds.get_financial_abstract(code, "按年度")
        out["fin_count"] = len(fin)
        out["fin"] = fin
        _p(f"  财务摘要 {len(fin)} 条")
    except Exception as e:
        out["fin_error"] = repr(e)
        _p(f"  财务摘要取数失败：{str(e)[:120]}")

    _p("取数 3/3：财务指标")
    try:
        ind = ds.get_financial_indicators(code, str(date.today().year - 5))
        out["ind_count"] = len(ind)
        out["ind"] = ind
        _p(f"  财务指标 {len(ind)} 条")
    except Exception as e:
        out["ind_error"] = repr(e)
        _p(f"  财务指标取数失败：{str(e)[:120]}")

    try:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    except Exception:
        pass

    if not out.get("daily"):
        raise RuntimeError(
            f"取数失败，无日K数据：{out.get('daily_error') or '接口返回空'}")

    return out


if __name__ == "__main__":
    c = sys.argv[1] if len(sys.argv) > 1 else "600519"
    d = probe_stock(c, on_progress=lambda m: print("[probe]", m))
    print(f"日K {len(d.get('daily', []))} 根 -> {paths.probe_path(c)}")
