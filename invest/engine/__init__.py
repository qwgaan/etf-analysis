#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""投资分析引擎：从 F:\\workbuddy\\投资分析\\tools 迁移并按函数化重构后的代码。

模块清单：
  akshare_ds      —— 数据层（日K / 财务摘要 / 财务指标），沿用新浪+东财双源策略
  probe           —— 取数（原 _probe.py CLI，已改为 probe_stock() 函数）
  compute         —— 指标计算（原 _compute.py CLI，已改为 compute_all() 函数）
  web_evidence    —— 外部证据采集（研报/估值分位/新闻/股东户数/资金流/北向/两融/东财语义检索）
  llm_client      —— OpenAI 兼容 LLM 客户端（零三方依赖，纯 stdlib urllib）
  llm_modules     —— 模块③多维交叉验证 / 模块④私董会的 LLM 生成与规则回落
  build_report    —— 报告 HTML 渲染
  profile_setup   —— 投资者画像（加载/校验/权重解释）
  pipeline        —— 单只流水线编排（原 run_report.py，已改为 run_single() 函数）
  batch           —— 批量跑批（原 run_batch.py，已改为 run_batch() 函数）

所有落盘路径一律经 invest.paths 取得，禁止模块内自行拼接目录。
"""
