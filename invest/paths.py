#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""投资分析模块的路径管理（隔离的唯一真相源）。

设计原则：本模块与 ETF 原有功能**零共享**——
  - 配置不走 config/user.json，走 invest/config/
  - 中间态不落 tools/，落 invest/data/
  - 报告产物不落 outputs/，落 invest/outputs/

任何引擎代码需要落盘时，都必须从这里取路径，禁止自己拼 os.path.dirname(__file__)。
新增子目录时在此登记，并同步更新 .gitignore。
"""
from __future__ import annotations

import os
from pathlib import Path

# invest 包根目录：.../ETF/invest
INVEST_ROOT = Path(__file__).resolve().parent

# 配置目录：LLM 端点/密钥、投资者画像、批量清单（含密钥，严禁提交）
CONFIG_DIR = INVEST_ROOT / "config"

# 数据目录：取数中间态、证据缓存、批量原始日志
DATA_DIR = INVEST_ROOT / "data"
PROBE_DIR = DATA_DIR / "probe"
EVIDENCE_DIR = DATA_DIR / "evidence"
LOG_DIR = DATA_DIR / "logs"

# 产物目录：尽调报告 HTML
OUTPUT_DIR = INVEST_ROOT / "outputs"

# 引擎代码目录
ENGINE_DIR = INVEST_ROOT / "engine"


def ensure_dirs() -> None:
    """确保运行期目录都存在（幂等，可重复调用）。"""
    for d in (CONFIG_DIR, DATA_DIR, PROBE_DIR, EVIDENCE_DIR, LOG_DIR, OUTPUT_DIR):
        d.mkdir(parents=True, exist_ok=True)


def probe_path(code: str) -> str:
    """取数中间态：invest/data/probe/probe_{code}.json"""
    return str(PROBE_DIR / f"probe_{code}.json")


def computed_path(code: str) -> str:
    """指标计算结果：invest/data/probe/computed_{code}.json"""
    return str(PROBE_DIR / f"computed_{code}.json")


def evidence_path(code: str) -> str:
    """外部证据缓存：invest/data/evidence/evidence_{code}.json"""
    return str(EVIDENCE_DIR / f"evidence_{code}.json")


def batch_log_path(code: str, date_str: str) -> str:
    """批量跑批的原始输出日志：invest/data/logs/batch_{code}_{date}.log"""
    return str(LOG_DIR / f"batch_{code}_{date_str}.log")


def report_path(code: str, name: str, date_str: str, suffix: str = "") -> str:
    """报告产物：invest/outputs/{code}-{name}-{date}[-{suffix}].html"""
    sfx = f"-{suffix}" if suffix else ""
    return str(OUTPUT_DIR / f"{code}-{name}-{date_str}{sfx}.html")


def safe_name(name: str) -> str:
    """清洗股票名称，避免路径穿越与非法文件名字符。"""
    bad = '<>:"/\\|?*'
    cleaned = "".join("_" if c in bad else c for c in str(name or "").strip())
    return cleaned or "unknown"


def rel_to_root(path: str) -> str:
    """把绝对路径转成相对 ETF 项目根的路径，便于前端展示。"""
    try:
        root = INVEST_ROOT.parent
        return os.path.relpath(path, root).replace("\\", "/")
    except Exception:
        return str(path)
