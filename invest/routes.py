#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""投资分析模块的 Flask 路由（Blueprint，url_prefix=/api/invest）。

完全独立于 ETF 原有路由与配置：
  - 配置读写只走 invest/config/（llm_config.json / invest_profile.json）
  - 数据与产物只走 invest/data/ 与 invest/outputs/
  - 长任务（单只/批量）全部异步后台执行，前端轮询 job 进度
"""
from __future__ import annotations

import traceback
from flask import Blueprint, request, jsonify, Response

from invest import jobs, config as cfg_mod
from invest import batch as batch_mod
from invest.config import (
    RISK_OPTIONS, HORIZON_OPTIONS, DEFAULT_OPTIONS,
)

bp = Blueprint("invest", __name__, url_prefix="/api/invest")


@bp.get("/config")
def api_config():
    try:
        cfg_mod.ensure_defaults()
        return jsonify({
            "ok": True,
            "enabled": True,
            "llm": cfg_mod.get_llm_status(),
            "profile": cfg_mod.get_profile(),
            "weights": cfg_mod.profile_weights(),
            "options_defaults": DEFAULT_OPTIONS,
            "risk_options": RISK_OPTIONS,
            "horizon_options": HORIZON_OPTIONS,
        })
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.post("/llm-config")
def api_llm_config():
    data = request.get_json(force=True, silent=True) or {}
    try:
        return jsonify({"ok": True, **cfg_mod.save_llm_config(data)})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.post("/profile")
def api_profile():
    data = request.get_json(force=True, silent=True) or {}
    try:
        return jsonify({"ok": True, "profile": cfg_mod.save_profile(data),
                        "weights": cfg_mod.profile_weights()})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.post("/profile-preview")
def api_profile_preview():
    """不落盘，仅按提交的画像计算权重预览。"""
    data = request.get_json(force=True, silent=True) or {}
    try:
        return jsonify({"ok": True, "weights": cfg_mod.profile_weights(data)})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.post("/llm-test")
def api_llm_test():
    """测试 LLM 连通性：发一条极简请求，返回模型回包前 100 字。"""
    try:
        from invest.engine.llm_client import chat
        out = chat("你是简洁的助手。", "只回复两个字：连通", verbose=False)
        return jsonify({"ok": True, "reply": (out or "")[:100]})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.post("/run")
def api_run():
    data = request.get_json(force=True, silent=True) or {}
    code = str(data.get("code", "")).strip()
    name = str(data.get("name", "")).strip()
    if not code:
        return jsonify({"ok": False, "error": "股票代码 code 必填"}), 400
    options = cfg_mod.build_options(data.get("options", {}) or {})
    try:
        jid = jobs.start_single(code, name, options)
        return jsonify({"ok": True, "job_id": jid})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.post("/batch")
def api_batch():
    data = request.get_json(force=True, silent=True) or {}
    options = cfg_mod.build_options(data.get("options", {}) or {})
    items = data.get("items") or []
    if not items:
        text = data.get("text", "")
        items = batch_mod.parse_items(text)
    if not items:
        return jsonify({"ok": False, "error": "批量清单为空，请至少填写一只代码"}), 400
    if len(items) > 50:
        return jsonify({"ok": False, "error": "单次批量最多 50 只，请分批运行"}), 400
    try:
        jid = jobs.start_batch(items, options)
        return jsonify({"ok": True, "job_id": jid, "count": len(items)})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.get("/jobs/<jid>")
def api_job(jid):
    job = jobs.get_job(jid)
    if not job:
        return jsonify({"ok": False, "error": "任务不存在或已过期"}), 404
    return jsonify({"ok": True, **job})


@bp.get("/jobs")
def api_jobs():
    return jsonify({"ok": True, "jobs": jobs.list_jobs(20)})


@bp.get("/reports")
def api_reports():
    try:
        raw = request.args.get("limit", "10")
        if raw == "all":
            limit = None
        else:
            try:
                limit = int(raw)
                if limit < 1:
                    limit = 10
            except (TypeError, ValueError):
                limit = 10
        return jsonify({"ok": True, "reports": cfg_mod.list_reports(limit=limit)})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.get("/report/<path:filename>")
def api_report(filename):
    abspath = cfg_mod.report_abspath(filename)
    if not abspath:
        return jsonify({"ok": False, "error": "报告不存在或路径非法"}), 404
    try:
        with open(abspath, "rb") as f:
            data = f.read()
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500
    return Response(data, mimetype="text/html")
