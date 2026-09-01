#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""投资分析模块配置管理（与 ETF 的 config/user.json 完全隔离）。

所有配置落 invest/config/：
  - llm_config.json     LLM 端点 / 密钥（含密钥，严禁提交，已由 .gitignore 排除）
  - invest_profile.json 投资者画像（风险/周期/仓位上限/股息偏好）

本模块提供：
  - get_llm_status / save_llm_config   读取与保存 LLM 配置（api_key 回显时打码）
  - get_profile / save_profile         读取与保存画像
  - profile_weights                    由画像推导权重与仓位映射，供前端预览
  - list_reports / report_path         报告产物清单与路径
  - build_options                      把前端表单整理成 run_single 所需的 options
"""
from __future__ import annotations

import os
import json

from invest import paths
from invest.engine import llm_client, profile_setup


LLM_CFG = str(paths.CONFIG_DIR / "llm_config.json")
PROFILE_CFG = str(paths.CONFIG_DIR / "invest_profile.json")


# 运行选项的默认值（前端表单初值 + 后端兜底）
DEFAULT_OPTIONS = {
    "use_llm": True,
    "use_web": True,
    "use_search": True,
    "fresh": False,
    "start": "20250901",
    "suffix": "",
    # 每股分红(元)，用于估算股息率；留空则自动
    "div": None,
}

RISK_OPTIONS = ["保守", "平衡", "激进", "自定义"]
HORIZON_OPTIONS = ["短线", "中线", "长线"]


def ensure_defaults() -> None:
    """确保配置目录与默认画像存在。"""
    paths.ensure_dirs()
    if not os.path.exists(PROFILE_CFG):
        profile_setup.save(profile_setup.load(), PROFILE_CFG)


# ============================================================
# LLM 配置
# ============================================================
def get_llm_status() -> dict:
    cfg = llm_client.load_cfg()
    return {
        "configured": llm_client.available(),
        "base_url": cfg.get("base_url", ""),
        "model": cfg.get("model", ""),
        "api_key_masked": llm_client.mask(cfg.get("api_key")),
        "has_key": bool(cfg.get("api_key")),
        "env_present": bool(os.environ.get("LLM_API_KEY")),
        "timeout": cfg.get("timeout", 120),
        "temperature": cfg.get("temperature", 0.4),
        "max_retries": cfg.get("max_retries", 3),
    }


def save_llm_config(data: dict) -> dict:
    """保存 LLM 配置；api_key 仅在用户填写时才更新（避免把掩码写回）。"""
    cfg = {}
    if os.path.exists(LLM_CFG):
        try:
            cfg = json.load(open(LLM_CFG, encoding="utf-8"))
        except Exception:
            cfg = {}
    for k in ("base_url", "model", "timeout", "max_retries", "temperature"):
        if k in data and data[k] not in (None, ""):
            try:
                cfg[k] = float(data[k]) if k in ("timeout", "temperature", "max_retries") else data[k]
            except (TypeError, ValueError):
                cfg[k] = data[k]
    if data.get("api_key"):
        cfg["api_key"] = str(data["api_key"]).strip()
    paths.ensure_dirs()
    json.dump(cfg, open(LLM_CFG, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    return get_llm_status()


# ============================================================
# 投资者画像
# ============================================================
def get_profile() -> dict:
    return profile_setup.load(PROFILE_CFG)


def _norm_weights(v):
    """把前端传来的四维权重 dict 归一化到和为 1；缺省维度补 0.25。"""
    if not isinstance(v, dict):
        return None
    clean = {d: float(v.get(d, 0) or 0) for d in ("技术面", "基本面", "估值", "资金面")}
    tot = sum(clean.values())
    if tot <= 0:
        return None
    return {d: round(clean[d] / tot, 4) for d in clean}


def save_profile(data: dict) -> dict:
    prof = profile_setup.load(PROFILE_CFG)
    for k in ("name", "risk", "horizon", "max_position", "dividend_focus", "notes", "weights"):
        if k in data:
            v = data[k]
            if k == "max_position":
                try:
                    fv = float(str(v).replace("%", ""))
                    v = round(fv / 100 if fv > 1 else fv, 4)
                except (TypeError, ValueError):
                    continue
            if k == "dividend_focus":
                v = bool(v)
            if k == "weights":
                nw = _norm_weights(v)
                if nw is not None and data.get("risk") == "自定义":
                    prof["weights"] = nw
                else:
                    prof.pop("weights", None)
                continue
            prof[k] = v
    paths.ensure_dirs()
    profile_setup.save(prof, PROFILE_CFG)
    return prof


def profile_weights(prof: dict = None) -> dict:
    """由画像推导四维权重与综合分→建议映射，供前端预览。"""
    from invest.engine.pipeline import adaptive, position_note
    prof = prof or get_profile()
    base = {"技术面": 4.0, "基本面": 4.0, "估值": 4.0, "资金面": 4.0}
    _, w = adaptive(base, prof)
    mapping = []
    for s in (4.5, 4.0, 3.6, 3.3, 3.1, 2.8, 2.0):
        st, size = position_note(s, prof)
        mapping.append({"score": s, "stance": st, "position": size})
    return {"weights": {k: round(v, 4) for k, v in w.items()},
            "mapping": mapping,
            "profile": prof}


# ============================================================
# 运行选项
# ============================================================
def build_options(form: dict) -> dict:
    """把前端表单整理成 run_single / 批量所需的 options dict。"""
    opt = dict(DEFAULT_OPTIONS)
    for k in ("use_llm", "use_web", "use_search", "fresh"):
        if k in form:
            opt[k] = bool(form[k])
    for k in ("start", "suffix"):
        if k in form and form[k] not in (None, ""):
            opt[k] = form[k]
    if form.get("div") not in (None, ""):
        try:
            opt["div"] = float(form["div"])
        except (TypeError, ValueError):
            pass
    return opt


# ============================================================
# 报告产物
# ============================================================
def list_reports(limit: int = 10) -> list:
    """列出 invest/outputs 下的报告 HTML，按修改时间倒序。limit=None 返回全部。"""
    out = paths.OUTPUT_DIR
    if not out.exists():
        return []
    items = []
    for p in out.glob("*.html"):
        try:
            st = p.stat()
            items.append({
                "file": p.name,
                "path": paths.rel_to_root(str(p)),
                "size": st.st_size,
                "mtime": st.st_mtime,
            })
        except Exception:
            continue
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items[:limit]


def report_abspath(filename: str):
    """把文件名安全解析为 invest/outputs 下的绝对路径（防穿越 + 兼容中文名）。

    不用 os.path.basename/resolve 拼接，避免非 ASCII 文件名在 Windows 下的
    编码/比对问题；改为在 outputs 目录内按文件名精确匹配，天然杜绝路径穿越。
    """
    out = paths.OUTPUT_DIR
    if not out.exists():
        return None
    name = os.path.basename(filename)  # 兜底去路径前缀
    for p in out.glob("*.html"):
        if p.name == name:
            return str(p)
    return None
