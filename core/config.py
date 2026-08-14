"""
配置管理
- 默认配置: config/defaults.json,产品自带只读
- 用户配置: config/user.json,前端可改并回写
- 用 dict diff + 轻量校验合并,不引入额外依赖

支持的内容:
- Mark 模板阈值(rule1_strict, rule2_lookback, rule2_min_slope, rule3/4 距离阈值)
- BIAS 三档警戒(10/15/20),用户可调
- 年度回撤防御档(单只 10% / 15% / 20% 三档,代表红利/创业板等不同品种)
- 默认展示窗口(52 weeks / YTD)
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
DEFAULTS_PATH = CONFIG_DIR / "defaults.json"
USER_PATH = CONFIG_DIR / "user.json"


DEFAULTS: dict = {
    "mark_filter": {
        "rule1_enabled": True,
        "rule1_strict_alignment": True,
        "rule2_enabled": True,
        "rule2_lookback": 20,
        "rule2_min_slope": 0.0,
        "rule3_enabled": True,
        "rule3_window_weeks": 52,
        "rule3_min_distance_pct": 25.0,
        "rule4_enabled": True,
        "rule4_window_weeks": 52,
        "rule4_max_distance_pct": 25.0,
    },
    "bias_thresholds": {
        "bias20_levels": [10.0, 15.0],
        "bias60_levels": [20.0],
    },
    "drawdown_thresholds": {
        "ytd_levels": [10.0, 15.0, 20.0],
        "ytd_level_tags": ["红利档", "中性档", "创业板档"],
    },
    "display": {
        "default_range": "year",     # "year" 或 "week52"
        "kline_years": 3,
        "auto_refresh_seconds": 86400,
    },
    "export": {
        "last_export_dir": "",
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并 override 到 base,不修改原 dict。"""
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_defaults() -> dict:
    if DEFAULTS_PATH.exists():
        try:
            with DEFAULTS_PATH.open("r", encoding="utf-8") as f:
                return _deep_merge(DEFAULTS, json.load(f))
        except Exception:
            pass
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with DEFAULTS_PATH.open("w", encoding="utf-8") as f:
        json.dump(DEFAULTS, f, ensure_ascii=False, indent=2)
    return DEFAULTS


def load_user(allow_corrupt_reset: bool = True) -> dict:
    """加载当前生效配置(defaults + user overlay),失败则返回纯 default。"""
    defaults = load_defaults()
    if not USER_PATH.exists():
        return defaults
    try:
        with USER_PATH.open("r", encoding="utf-8") as f:
            overlay = json.load(f)
    except Exception:
        if allow_corrupt_reset:
            return defaults
        raise
    return _deep_merge(defaults, overlay)


def save_user(config: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with USER_PATH.open("w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def reset_user() -> dict:
    """重置为默认配置,返回默认 dict;同时删除 user.json。"""
    if USER_PATH.exists():
        USER_PATH.unlink()
    return load_defaults()


def diff_for_ui(defaults: dict, current: dict) -> dict:
    """生成前端用于展示 '和默认不一样' 的高亮。"""
    diff: dict = {}
    def walk(a, b, path):
        keys = set(a.keys()) | set(b.keys())
        for k in keys:
            sub_a = a.get(k)
            sub_b = b.get(k)
            full = path + [k]
            if isinstance(sub_a, dict) and isinstance(sub_b, dict):
                walk(sub_a, sub_b, full)
            elif sub_a != sub_b:
                diff[".".join(full)] = {"default": sub_a, "current": sub_b}
    walk(defaults, current, [])
    return diff
