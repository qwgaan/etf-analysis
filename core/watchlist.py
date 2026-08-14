"""
自选 ETF 池管理(多组)。
- 数据存储: data/watchlist.json
- 数据结构: [{"name": "组名", "codes": ["510300", ...]}, ...]
- 旧版单列表 [{code, name, added_at}] 自动迁移到「默认组」。

提供: 组 CRUD + 按组增删 ETF + 按组跑 Mark 模板。
"""
from __future__ import annotations

import json
from pathlib import Path

from . import data_source as ds

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WL_PATH = PROJECT_ROOT / "data" / "watchlist.json"

DEFAULT_GROUP = "默认组"


# ---------- 底层读写 ----------
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
        return data

    # 旧格式: [{"code": ..., "name": ..., "added_at": ...}, ...]
    if isinstance(data, list):
        codes = []
        for it in data:
            if isinstance(it, dict) and "code" in it:
                codes.append(str(it["code"]).zfill(6))
        if codes:
            return [{"name": DEFAULT_GROUP, "codes": codes}]
    return []


def _save(groups: list[dict]) -> None:
    WL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with WL_PATH.open("w", encoding="utf-8") as f:
        json.dump(groups, f, ensure_ascii=False, indent=2)


# ---------- 组操作 ----------
def list_groups() -> list[dict]:
    """返回所有组 [{name, codes, count}]。"""
    groups = _load()
    return [{"name": g["name"], "codes": g["codes"], "count": len(g["codes"])} for g in groups]


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


# ---------- 组内 ETF 操作 ----------
def add_to_group(code: str, group: str = DEFAULT_GROUP) -> list[dict]:
    code = str(code).zfill(6)
    groups = _load()
    # 目标组不存在则自动建
    target = next((g for g in groups if g["name"] == group), None)
    if target is None:
        target = {"name": group, "codes": []}
        groups.append(target)
    if code in target["codes"]:
        return list_groups()
    target["codes"].append(code)
    _save(groups)
    return list_groups()


def remove_from_group(code: str, group: str = DEFAULT_GROUP) -> list[dict]:
    code = str(code).zfill(6)
    groups = _load()
    for g in groups:
        if g["name"] == group and code in g["codes"]:
            g["codes"] = [c for c in g["codes"] if c != code]
            break
    _save(groups)
    return list_groups()


# ---------- 工具: 组内 ETF 详情 ----------
def _resolve_name(code: str) -> str:
    try:
        pool = ds.list_etfs()
        row = pool[pool["code"] == code]
        if not row.empty:
            return str(row.iloc[0]["name"])
    except Exception:
        pass
    return code


def group_codes(group: str = DEFAULT_GROUP) -> list[str]:
    groups = _load()
    for g in groups:
        if g["name"] == group:
            return list(g["codes"])
    return []
