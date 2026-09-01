#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""投资分析模块（invest）—— 独立于 ETF 原有功能的第二个业务模块。

边界约定：
  - 配置：invest/config/   （不与 config/user.json 互读写）
  - 数据：invest/data/     （取数中间态、证据缓存、批次日志）
  - 产物：invest/outputs/  （尽调报告 HTML）
  - 路由：/api/invest/*    （Flask Blueprint，受 invest.enabled 开关控制）

依赖：仅复用 ETF 已有的 akshare / pandas / numpy，零新增 pip 包。
"""
__version__ = "0.5.0"
