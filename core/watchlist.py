"""
自选 ETF 池管理(多组)。
- 数据存储: data/watchlist.json
- 数据结构(新):
    [
      {
        "name": "组名",
        "codes": [
          {
            "code": "510300",
            "alerts": {"bias20": true, "bias60": false, "dd": true},
            "thresholds": {"bias20_levels": [12], "bias60_levels": [22], "ytd_levels": [12]}
          },
          ...
        ]
      }, ...
    ]
  - alerts 字段:每只包含 3 个订阅开关
      bias20 : 是否订阅 BIAS20 警戒
      bias60 : 是否订阅 BIAS60 警戒
      dd     : 是否订阅「年度最大回撤」警戒档
  - thresholds 字段(可选):每只 ETF 独立阈值,未设置则沿用全局阈值
- 兼容:
  - 旧版单列表 [{code, name, added_at}] 自动迁移到「默认组」。
  - 组内 codes 旧版纯字符串写法自动规范成 {code, alerts, thresholds}。

提供: 组 CRUD + 按组增删 ETF + 按组跑 Mark 模板 + 每只 ETF 的警戒订阅/阈值读写。
"""
from __future__ import annotations

import json
from pathlib import Path

from . import data_source as ds

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WL_PATH = PROJECT_ROOT / "data" / "watchlist.json"

DEFAULT_GROUP = "默认组"

# 每只 ETF 的订阅开关默认值
DEFAULT_ALERTS: dict[str, bool] = {"bias20": False, "bias60": False, "dd": False}
ALERT_KEYS = tuple(DEFAULT_ALERTS.keys())

# 每只 ETF 可独立覆盖的阈值键
THRESHOLD_KEYS = ("bias20_levels", "bias60_levels", "ytd_levels")


# ---------- 底层读写 ----------
def _detect_kind(code: str) -> str:
    """按代码前缀判断类型:股票 stock / 基金 ETF。"""
    return "stock" if ds.is_stock_code(code) else "etf"


def _normalize_code(raw) -> dict | None:
    """把单个 code 项(字符串或 dict)规范成 {code, kind, name, alerts, thresholds}。无法识别返回 None。"""
    if isinstance(raw, str):
        code = raw.zfill(6)
        return {"code": code, "kind": _detect_kind(code), "name": None,
                "alerts": dict(DEFAULT_ALERTS), "thresholds": {}}
    if isinstance(raw, dict) and "code" in raw:
        code = str(raw["code"]).zfill(6)
        kind = raw.get("kind") or _detect_kind(code)
        name = raw.get("name") or None
        alerts = dict(DEFAULT_ALERTS)
        raw_alerts = raw.get("alerts") or {}
        for k in ALERT_KEYS:
            alerts[k] = bool(raw_alerts.get(k, False))
        thresholds: dict[str, list[float]] = {}
        raw_th = raw.get("thresholds") or {}
        for k in THRESHOLD_KEYS:
            if k in raw_th and isinstance(raw_th[k], list):
                thresholds[k] = [float(v) for v in raw_th[k] if isinstance(v, (int, float, str)) and v != ""]
        return {"code": code, "kind": kind, "name": name, "alerts": alerts, "thresholds": thresholds}
    return None


def _normalize_codes(raw_codes) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for c in (raw_codes or []):
        obj = _normalize_code(c)
        if obj is None:
            continue
        if obj["code"] in seen:
            continue
        seen.add(obj["code"])
        out.append(obj)
    return out


def _load() -> list[dict]:
    """读取原始 JSON,返回 groups 列表,自动做旧格式迁移。"""
    if not WL_PATH.exists():
        return []
    try:
        with WL_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []

    # 新格式: [{"name": ..., "codes": [...]}, ...]
    if isinstance(data, list) and all(isinstance(g, dict) and "codes" in g for g in data):
        return [{"name": g["name"], "codes": _normalize_codes(g.get("codes", []))} for g in data]

    # 旧格式: [{"code": ..., "name": ..., "added_at": ...}, ...]
    if isinstance(data, list):
        codes = [str(it["code"]).zfill(6) for it in data if isinstance(it, dict) and "code" in it]
        if codes:
            return [{"name": DEFAULT_GROUP, "codes": _normalize_codes(codes)}]
    return []


def _save(groups: list[dict]) -> None:
    WL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with WL_PATH.open("w", encoding="utf-8") as f:
        json.dump(groups, f, ensure_ascii=False, indent=2)


# ---------- 组操作 ----------
def list_groups() -> list[dict]:
    """返回所有组 [{name, codes, count}];codes 为代码字符串列表(兼容前端)。"""
    groups = _load()
    return [{"name": g["name"], "codes": [c["code"] for c in g["codes"]], "count": len(g["codes"])} for g in groups]


def create_group(name: str) -> list[dict]:
    name = (name or "").strip()
    if not name:
        raise ValueError("组名不能为空")
    groups = _load()
    if any(g["name"] == name for g in groups):
        raise ValueError(f"组「{name}」已存在")
    groups.append({"name": name, "codes": []})
    _save(groups)
    return list_groups()


def delete_group(name: str) -> list[dict]:
    groups = _load()
    new_groups = [g for g in groups if g["name"] != name]
    _save(new_groups)
    return list_groups()


def rename_group(old_name: str, new_name: str) -> list[dict]:
    new_name = (new_name or "").strip()
    if not new_name:
        raise ValueError("新组名不能为空")
    groups = _load()
    found = False
    for g in groups:
        if g["name"] == old_name:
            g["name"] = new_name
            found = True
    if not found:
        raise ValueError(f"组「{old_name}」不存在")
    _save(groups)
    return list_groups()


# ---------- 组内 ETF/股票 操作 ----------
def add_to_group(code: str, group: str = DEFAULT_GROUP, kind: str | None = None, name: str | None = None) -> list[dict]:
    code = str(code).zfill(6)
    if kind is None:
        kind = _detect_kind(code)
    groups = _load()
    # 目标组不存在则自动建
    target = next((g for g in groups if g["name"] == group), None)
    if target is None:
        target = {"name": group, "codes": []}
        groups.append(target)
    if any(c["code"] == code for c in target["codes"]):
        return list_groups()
    target["codes"].append({"code": code, "kind": kind, "name": name or None,
                            "alerts": dict(DEFAULT_ALERTS), "thresholds": {}})
    _save(groups)
    return list_groups()


def remove_from_group(code: str, group: str = DEFAULT_GROUP) -> list[dict]:
    code = str(code).zfill(6)
    groups = _load()
    for g in groups:
        if g["name"] == group:
            g["codes"] = [c for c in g["codes"] if c["code"] != code]
            break
    _save(groups)
    return list_groups()


# ---------- 警戒订阅 ----------
def get_group_alerts(group: str = DEFAULT_GROUP) -> dict[str, dict[str, bool]]:
    """返回该组每只 ETF 的订阅开关 {code: {bias20, bias60, dd}}。"""
    groups = _load()
    for g in groups:
        if g["name"] == group:
            return {c["code"]: dict(c["alerts"]) for c in g["codes"]}
    return {}


def set_alerts(group: str, code: str, alerts: dict) -> list[dict]:
    """更新某 code 的订阅开关(只更新 alerts 里出现的键),返回 groups。"""
    code = str(code).zfill(6)
    groups = _load()
    target = next((g for g in groups if g["name"] == group), None)
    if target is None:
        return list_groups()
    for c in target["codes"]:
        if c["code"] == code:
            for k in ALERT_KEYS:
                if k in (alerts or {}):
                    c["alerts"][k] = bool(alerts[k])
            break
    _save(groups)
    return list_groups()


def get_code_thresholds(group: str, code: str) -> dict[str, list[float]]:
    """返回某 code 的独立阈值 {} 或 {bias20_levels, bias60_levels, ytd_levels}。"""
    code = str(code).zfill(6)
    for g in _load():
        if g["name"] == group:
            for c in g["codes"]:
                if c["code"] == code:
                    return dict(c.get("thresholds", {}))
    return {}


def get_group_thresholds(group: str = DEFAULT_GROUP) -> dict[str, dict[str, list[float]]]:
    """返回该组每只 ETF 的独立阈值 {code: {bias20_levels, ...}}。"""
    for g in _load():
        if g["name"] == group:
            return {c["code"]: dict(c.get("thresholds", {})) for c in g["codes"]}
    return {}


def set_thresholds(group: str, code: str, thresholds: dict) -> list[dict]:
    """更新某 code 的独立阈值(只保存非空且与全局不同的档位)。"""
    code = str(code).zfill(6)
    groups = _load()
    target = next((g for g in groups if g["name"] == group), None)
    if target is None:
        return list_groups()
    for c in target["codes"]:
        if c["code"] != code:
            continue
        new_th: dict[str, list[float]] = {}
        for k in THRESHOLD_KEYS:
            if k not in thresholds:
                continue
            vals = thresholds[k]
            if vals is None or vals == []:
                continue
            if isinstance(vals, str):
                vals = [v.strip() for v in vals.split(",") if v.strip() != ""]
            parsed = []
            for v in vals:
                try:
                    parsed.append(float(v))
                except Exception:
                    pass
            if parsed:
                new_th[k] = parsed
        c["thresholds"] = new_th
        break
    _save(groups)
    return list_groups()


def all_subscriptions() -> list[dict]:
    """返回所有组里「至少订阅了一项」的 ETF/股票: [{group, code, kind, name, alerts, thresholds}]。供定时扫描使用。"""
    out: list[dict] = []
    for g in _load():
        for c in g["codes"]:
            if any(c["alerts"].values()):
                code = c["code"]
                out.append({
                    "group": g["name"],
                    "code": code,
                    "kind": c.get("kind") or _detect_kind(code),
                    "name": c.get("name") or ds.resolve_name(code),
                    "alerts": dict(c["alerts"]),
                    "thresholds": dict(c.get("thresholds", {})),
                })
    return out


# ---------- 组内条目详情(带类型/名称) ----------
def group_items(group: str = DEFAULT_GROUP) -> list[dict]:
    """返回该组内每只的 [{code, kind, name, alerts, thresholds}]。名称统一解析(优先存储名,否则按类型查)。"""
    for g in _load():
        if g["name"] == group:
            codes = [c["code"] for c in g["codes"]]
            names = ds.resolve_names(codes)
            items: list[dict] = []
            for c in g["codes"]:
                code = c["code"]
                kind = c.get("kind") or _detect_kind(code)
                name = c.get("name") or names.get(code, code)
                items.append({
                    "code": code,
                    "kind": kind,
                    "name": name,
                    "alerts": dict(c["alerts"]),
                    "thresholds": dict(c.get("thresholds", {})),
                })
            return items
    return []


# ---------- 工具: 组内 ETF 详情 ----------
def _resolve_name(code: str) -> str:
    return ds.resolve_name(code)


def group_codes(group: str = DEFAULT_GROUP) -> list[str]:
    groups = _load()
    for g in groups:
        if g["name"] == group:
            return [c["code"] for c in g["codes"]]
    return []


# ---------- 导出 / 导入 ----------
def export_groups(names: list[str] | None = None) -> list[dict]:
    """返回完整分组数据(含每只 ETF 的 alerts/thresholds)。
    不传 names 则导出全部。用于导出备份 / 跨设备迁移。
    """
    groups = _load()
    if names:
        names_set = set(names)
        groups = [g for g in groups if g["name"] in names_set]
    return [
        {
            "name": g["name"],
            "codes": [
                {
                    "code": c["code"],
                    "kind": c.get("kind") or _detect_kind(c["code"]),
                    "name": c.get("name"),
                    "alerts": dict(c["alerts"]),
                    "thresholds": dict(c.get("thresholds", {})),
                }
                for c in g["codes"]
            ],
        }
        for g in groups
    ]


def import_groups(groups_payload: list[dict], modes: dict[str, str] | None = None) -> list[dict]:
    """导入分组。
    groups_payload: [{name, codes:[{code, alerts, thresholds}]}, ...]
    modes: {组名: "merge" | "overwrite"}；未指定的组名默认 "merge"。
      - 组名存在 + merge    : 保留现有 code 与设置,导入中新增的 code 追加(同名不覆盖当前订阅)
      - 组名存在 + overwrite: 整组替换为导入内容
      - 组名不存在          : 直接追加新组
    返回导入后的所有组(供前端刷新)。
    """
    if not isinstance(groups_payload, list):
        raise ValueError("groups 必须是数组")
    modes = modes or {}
    groups = _load()
    existing_idx = {g["name"]: i for i, g in enumerate(groups)}
    for gp in groups_payload:
        if not isinstance(gp, dict) or not gp.get("name"):
            continue
        name = str(gp["name"]).strip()
        if not name:
            continue
        new_codes = _normalize_codes(gp.get("codes", []))
        if name in existing_idx:
            mode = modes.get(name, "merge")
            idx = existing_idx[name]
            if mode == "overwrite":
                groups[idx]["codes"] = new_codes
            else:  # merge:现有 code 保留,导入新增的 code 追加
                seen = {c["code"] for c in groups[idx]["codes"]}
                for c in new_codes:
                    if c["code"] not in seen:
                        groups[idx]["codes"].append(c)
                        seen.add(c["code"])
        else:
            groups.append({"name": name, "codes": new_codes})
    _save(groups)
    return list_groups()
