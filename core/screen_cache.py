"""全市场筛选结果缓存。

用途:ETF 列表页左侧「仅显示所有启用规则全过的」复选框需要快速拿到
passed_codes,但全量 1576 只 ETF 现场计算可能耗时 1~3 分钟。
本模块把最近一次全量筛选的结果缓存到本地,命中时毫秒级返回。

缓存失效条件:
1. 数据日期变化(以 K 线缓存最新交易日为准)。
2. Mark 规则配置(mark_filter)发生变化。
3. K 线缓存数量变化(新增/删除 ETF)。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

import pandas as pd

from . import data_source as ds
from . import indicators as ind
from .filters import FilterConfig, evaluate_one

CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "screen_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _config_hash(mark_filter: dict) -> str:
    """对 mark_filter 配置做稳定哈希,用于缓存键。"""
    return hashlib.md5(json.dumps(mark_filter, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]


def _cache_path(mark_filter: dict) -> Path:
    return CACHE_DIR / f"passed_{_config_hash(mark_filter)}.json"


def _market_date() -> str:
    """以 K 线缓存最新交易日作为市场数据日期;拿不到则用今天。"""
    codes = ds.list_cached_codes()
    latest = None
    for code in codes:
        try:
            df = ds.load_cached_kline(code, years=10)
            if df is not None and not df.empty:
                d = df.index[-1]
                if isinstance(d, pd.Timestamp):
                    d = d.strftime("%Y-%m-%d")
                if latest is None or d > latest:
                    latest = d
        except Exception:
            continue
    return latest or datetime.now().strftime("%Y-%m-%d")


def load(mark_filter: dict) -> dict | None:
    """读取缓存。若缓存不存在/过期/配置不匹配,返回 None。"""
    path = _cache_path(mark_filter)
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            cache = json.load(f)
    except Exception:
        return None

    # 检查缓存是否过期
    if cache.get("date") != _market_date():
        return None
    if cache.get("config_hash") != _config_hash(mark_filter):
        return None
    if cache.get("cache_count") != len(ds.list_cached_codes()):
        return None
    return cache


def save(mark_filter: dict, passed_codes: list[str], total: int, scanned: int,
         enabled_count: int, enabled_rules: list[int], matched: int) -> None:
    """保存筛选结果缓存。"""
    path = _cache_path(mark_filter)
    cache = {
        "date": _market_date(),
        "config_hash": _config_hash(mark_filter),
        "cache_count": len(ds.list_cached_codes()),
        "total": total,
        "scanned": scanned,
        "enabled_count": enabled_count,
        "enabled_rules": enabled_rules,
        "matched": matched,
        "passed_codes": passed_codes,
        "generated_at": datetime.now().isoformat(),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def clear(mark_filter: dict | None = None) -> None:
    """清除缓存。mark_filter 为 None 时清除所有筛选缓存。"""
    if mark_filter is None:
        for p in CACHE_DIR.glob("passed_*.json"):
            try:
                p.unlink()
            except Exception:
                pass
    else:
        path = _cache_path(mark_filter)
        if path.exists():
            try:
                path.unlink()
            except Exception:
                pass


def compute_and_save(pool: pd.DataFrame, fcfg: FilterConfig, mark_filter: dict,
                     progress_cb=None) -> dict:
    """执行全量计算并保存缓存,返回缓存 dict。"""
    results = []
    total = len(pool)
    done = 0
    for _, row in pool.iterrows():
        df = row.get("kline")
        done += 1
        if df is None or df.empty:
            if progress_cb:
                progress_cb(done, total)
            continue
        try:
            results.append(evaluate_one(str(row["code"]), str(row["name"]), df, fcfg))
        except Exception:
            pass
        if progress_cb:
            progress_cb(done, total)

    enabled_count = fcfg.enabled_count
    enabled_rules = fcfg.enabled_rules
    passed = [r for r in results if r.passed_count == enabled_count]
    cache = {
        "date": _market_date(),
        "config_hash": _config_hash(mark_filter),
        "cache_count": len(ds.list_cached_codes()),
        "total": len(ds.list_etfs()),
        "scanned": len(pool),
        "enabled_count": enabled_count,
        "enabled_rules": enabled_rules,
        "matched": len(passed),
        "passed_codes": [r.code for r in passed],
        "generated_at": datetime.now().isoformat(),
    }
    path = _cache_path(mark_filter)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
    return cache
