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
        "rule5_enabled": True,
        "rule5_min_distance_pct": 15.0,
        "rule6_enabled": True,
        "rule6_max_distance_pct": 25.0,
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
        "screen_limit": 0,           # 0 = 全量,>0 = 最多扫描 N 只
        "auto_refresh_seconds": 86400,
    },
    "export": {
        "last_export_dir": "",
    },
    "wxpusher": {
        "spt_token": "",
    },
    "alert_schedule": ["10:00", "13:30", "17:00"],  # 交易日自动推送时间(可改)
    "alert_holidays": [],                              #  extra 节假日(YYYY-MM-DD),跳过推送
    "offline_refresh_schedule": ["07:30", "16:15"],  # 交易日全量离线 K 线拉取时间(可改)
    "alert_dedup_scope": "persist",                  # 推送去重范围: persist=跨交易日有效 / day=当天有效
    # 数据源配置: 每个用途是一个数组,数组顺序=优先级/回退顺序,在数组中=启用
    "data_sources": {
        "realtime": ["tdx", "sina", "em"],   # 实时价: 通达信 → 新浪 → 东财
        "intraday": ["tdx", "em", "sina"],   # 当天分时: 通达信 → 东财 → 新浪
        "kline": ["sina", "em", "tdx"],      # 日K离线: 新浪 → 东财 → 通达信
    },
    "tdx_source": {
        "timeout": 8,                   # 单行情服务器连接超时(秒)
        "min_interval": 0.34,           # 节流间隔(秒),满足单 IP ≤3 次/秒硬限制
        "best_ip": False,               # 启动时是否 best_ip 探测(较慢,默认关,用内置候选列表)
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并 override 到 base,不修改原 dict。

    注意: override 中的 None 值(如历史遗留的 "tdx_source": null)不覆盖默认,
    避免把有效默认值抹成 null。这是有意的——前端未保存过的字段不应被 null 清空。
    """
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if v is None:
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _migrate_config(cfg: dict) -> dict:
    """把历史配置迁移到当前结构,并清理已废弃字段。"""
    # 1) 从旧的 intraday_source / tdx_source.enabled / tdx_source.kline_fallback 推导数组
    if "data_sources" not in cfg:
        tdx = cfg.get("tdx_source") or {}
        tdx_enabled = tdx.get("enabled", True)
        kline_fallback = tdx.get("kline_fallback", True)
        intraday_src = cfg.get("intraday_source", "em_first")

        ds = copy.deepcopy(DEFAULTS["data_sources"])
        if not tdx_enabled:
            ds["realtime"] = [s for s in ds["realtime"] if s != "tdx"]
            ds["intraday"] = [s for s in ds["intraday"] if s != "tdx"]
        if not kline_fallback:
            ds["kline"] = [s for s in ds["kline"] if s != "tdx"]
        if intraday_src == "sina_only":
            ds["intraday"] = [s for s in ds["intraday"] if s != "em"]
        cfg["data_sources"] = ds

    # 2) 把旧版 dict 格式(如 {"tdx":true,"sina":true})迁移为数组格式
    ds_default = DEFAULTS["data_sources"]
    for purpose in ds_default:
        val = cfg.get("data_sources", {}).get(purpose)
        if isinstance(val, dict):
            # 按默认顺序保留勾选项,未勾选的不在数组中
            cfg["data_sources"][purpose] = [s for s in ds_default[purpose] if val.get(s)]
        elif not isinstance(val, list):
            cfg["data_sources"][purpose] = copy.deepcopy(ds_default[purpose])

    # 3) 确保每个用途的数组都是合法源(去重、补全缺失源)
    all_srcs = ["tdx", "sina", "em"]
    for purpose, default_order in ds_default.items():
        arr = cfg.setdefault("data_sources", {}).setdefault(purpose, copy.deepcopy(default_order))
        seen = set()
        clean = []
        for s in arr:
            if s in all_srcs and s not in seen:
                clean.append(s); seen.add(s)
        # 默认未勾选但不在列表中的,按默认顺序追加(允许用户只保留部分源)
        # 注意:这里只补"当前数组为空"的兜底,保持用户可自由选择关闭某些源
        if not clean:
            clean = copy.deepcopy(default_order)
        cfg["data_sources"][purpose] = clean

    # 4) 清理废弃字段
    cfg.pop("intraday_source", None)
    tdx_cfg = cfg.get("tdx_source")
    if isinstance(tdx_cfg, dict):
        tdx_cfg.pop("enabled", None)
        tdx_cfg.pop("kline_fallback", None)

    return cfg


def load_defaults() -> dict:
    if DEFAULTS_PATH.exists():
        try:
            with DEFAULTS_PATH.open("r", encoding="utf-8") as f:
                cfg = _deep_merge(DEFAULTS, json.load(f))
                return _migrate_config(cfg)
        except Exception:
            pass
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with DEFAULTS_PATH.open("w", encoding="utf-8") as f:
        json.dump(DEFAULTS, f, ensure_ascii=False, indent=2)
    return _migrate_config(copy.deepcopy(DEFAULTS))


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
    return _migrate_config(_deep_merge(defaults, overlay))


def save_user(config: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with USER_PATH.open("w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def reset_user() -> dict:
    """重置为默认配置,返回默认 dict;清空 user.json(避免沙箱 unlink 失败)。"""
    save_user({})
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
