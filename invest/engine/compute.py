#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""指标计算（原 _compute.py CLI，已重构为函数）。

原实现是顶层脚本：读 probe_{code}.json -> 计算 -> 写 computed_{code}.json。
重构后为 compute_all(probe: dict) -> dict，落盘路径由 invest.paths 给出。
计算逻辑与原版保持一致（技术面 + 基本面 + CAGR），未改动任何公式。
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from invest import paths


def _num(x):
    try:
        return float(x)
    except Exception:
        return None


def compute_all(probe: dict, on_progress=None) -> dict:
    """由取数结果计算技术面 + 基本面指标。"""
    def _p(msg):
        if on_progress:
            try:
                on_progress(msg)
            except Exception:
                pass

    paths.ensure_dirs()
    code = probe.get("code", "unknown")
    res: dict = {}

    # ========================= 技术面 =========================
    _p("计算 1/2：技术面指标（均线/MACD/RSI/52周高低/量能）")
    df = pd.DataFrame(probe["daily"])
    for c in ["open", "high", "low", "close", "volume", "amount"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.sort_values("date").reset_index(drop=True)

    def ma(s, n):
        return s.rolling(n).mean()

    def ema(s, n):
        return s.ewm(span=n, adjust=False).mean()

    close = df["close"]
    vol = df["volume"]
    df["ma5"] = ma(close, 5)
    df["ma10"] = ma(close, 10)
    df["ma20"] = ma(close, 20)
    df["ma60"] = ma(close, 60)

    # MACD
    dif = ema(close, 12) - ema(close, 26)
    dea = ema(dif, 9)
    macd = (dif - dea) * 2

    # RSI(14)
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / (loss + 1e-9)
    rsi = 100 - 100 / (1 + rs)

    last = df.iloc[-1]
    prev = df.iloc[-2]
    last_close = float(last["close"])
    prev_close = float(prev["close"])

    # 52周(约250交易日)高低
    window = df.tail(250)
    hi52 = float(window["high"].max())
    lo52 = float(window["low"].min())
    hi20 = float(df.tail(20)["high"].max())
    lo20 = float(df.tail(20)["low"].min())

    # 量能：近5日均量 vs 前5日均量
    vol5 = float(df.tail(5)["volume"].mean())
    vol5_prev = float(df.tail(10).head(5)["volume"].mean())
    vol_ratio = vol5 / vol5_prev if vol5_prev else None

    ma_vals = {k: float(last[k]) for k in ["ma5", "ma10", "ma20", "ma60"] if pd.notna(last[k])}

    # 金叉/死叉判定
    macd_last = float(macd.iloc[-1])
    dif_last = float(dif.iloc[-1])
    dea_last = float(dea.iloc[-1])
    macd_prev = float(macd.iloc[-2])
    cross = "无"
    if macd_prev <= 0 < macd_last:
        cross = "MACD金叉(柱由负转正)"
    elif macd_prev >= 0 > macd_last:
        cross = "MACD死叉(柱由正转负)"
    if ma_vals.get("ma5") and ma_vals.get("ma10"):
        if df["ma5"].iloc[-2] <= df["ma10"].iloc[-2] and ma_vals["ma5"] > ma_vals["ma10"]:
            cross += " + MA5上穿MA10"
        elif df["ma5"].iloc[-2] >= df["ma10"].iloc[-2] and ma_vals["ma5"] < ma_vals["ma10"]:
            cross += " + MA5下穿MA10"

    chg = (last_close - prev_close) / prev_close * 100

    tech = {
        "date_last": last["date"],
        "close": round(last_close, 2),
        "prev_close": round(prev_close, 2),
        "chg_pct": round(chg, 2),
        "ma5": round(ma_vals.get("ma5"), 2), "ma10": round(ma_vals.get("ma10"), 2),
        "ma20": round(ma_vals.get("ma20"), 2), "ma60": round(ma_vals.get("ma60"), 2),
        "hi52": round(hi52, 2), "lo52": round(lo52, 2),
        "hi20": round(hi20, 2), "lo20": round(lo20, 2),
        "from_hi52_pct": round((last_close / hi52 - 1) * 100, 2),
        "from_lo52_pct": round((last_close / lo52 - 1) * 100, 2),
        "macd": {"dif": round(dif_last, 3), "dea": round(dea_last, 3),
                 "bar": round(macd_last, 3), "dif_prev": round(float(dif.iloc[-2]), 3)},
        "rsi14": round(float(rsi.iloc[-1]), 1),
        "vol5": int(vol5), "vol5_prev": int(vol5_prev),
        "vol_ratio": round(vol_ratio, 2) if vol_ratio else None,
        "ma_below_close": {k: round(last_close - v, 2) for k, v in ma_vals.items()},
        "bull_arrangement": (ma_vals.get("ma5", 0) >= ma_vals.get("ma10", 0)
                             >= ma_vals.get("ma20", 0) >= ma_vals.get("ma60", 0)),
        "cross": cross,
    }
    res["tech"] = tech

    # ========================= 基本面 =========================
    _p("计算 2/2：基本面（年度财务/CAGR/最新指标）")
    fin = pd.DataFrame(probe.get("fin") or [])   # 按年度，已降序（最新在前）
    ind = pd.DataFrame(probe.get("ind") or [])   # 指标，已降序（最新在前）

    fund: dict = {}
    fin_cols = list(fin.columns)

    annual = fin[fin["报告期"].astype(str).str.contains("年")] if "报告期" in fin.columns else fin
    annual_years = []
    if "报告期" in fin.columns:
        for _, r in fin.iterrows():
            rp = str(r["报告期"])
            if "年" in rp and ("12-31" in rp or rp.endswith("年")):
                annual_years.append(r)
    annual_df = pd.DataFrame(annual_years) if annual_years else fin.head(5)

    if not annual_df.empty:
        def pick(row, *names):
            for n in names:
                for c in row.index:
                    if n in str(c):
                        v = row[c]
                        if v not in (None, "", "-"):
                            return v
            return None

        rows = []
        for _, r in annual_df.iterrows():
            rows.append({
                "报告期": r.get("报告期"),
                "净利润": pick(r, "净利润"),
                "营业总收入": pick(r, "营业总收入", "营业收入"),
                "基本每股收益": pick(r, "基本每股收益", "每股收益"),
                "净资产收益率": pick(r, "净资产收益率", "ROE"),
                "销售毛利率": pick(r, "销售毛利率", "毛利率"),
                "每股净资产": pick(r, "每股净资产"),
            })
        fund["annual"] = rows

        # CAGR
        try:
            yrs = [r for r in rows if r["营业总收入"] is not None]
            if len(yrs) >= 2:
                v0 = _num(yrs[-1]["营业总收入"])
                v1 = _num(yrs[0]["营业总收入"])
                n = len(yrs) - 1
                if v0 and v1:
                    fund["rev_cagr"] = round((v1 / v0) ** (1 / n) - 1, 4)
            nets = [r for r in rows if r["净利润"] is not None]
            if len(nets) >= 2:
                v0 = _num(nets[-1]["净利润"])
                v1 = _num(nets[0]["净利润"])
                n = len(nets) - 1
                if v0 and v1:
                    fund["np_cagr"] = round((v1 / v0) ** (1 / n) - 1, 4)
        except Exception as e:
            fund["cagr_err"] = repr(e)

    # 最近报告期（ind 最新）
    if not ind.empty and "日期" in ind.columns:
        latest_ind = ind.iloc[0].to_dict()
        fund["latest_ind_date"] = latest_ind.get("日期")

        def g(name):
            for c in latest_ind:
                if name in str(c):
                    return latest_ind[c]
            return None

        fund["latest"] = {
            "资产负债率": g("资产负债率"),
            "销售毛利率": g("销售毛利率"),
            "净资产收益率": g("净资产收益率"),
            "营业收入同比增长率": g("营业收入同比增长"),
            "净利率": g("净利率"),
            "存货周转率": g("存货周转率"),
            "流动比率": g("流动比率"),
            "研发费用率": g("研发费用率"),
        }

    res["fund"] = fund
    res["fin_columns"] = fin_cols

    try:
        with open(paths.computed_path(code), "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=2, default=str)
    except Exception:
        pass

    return res


if __name__ == "__main__":
    c = sys.argv[1] if len(sys.argv) > 1 else "600519"
    pr = json.load(open(paths.probe_path(c), encoding="utf-8"))
    r = compute_all(pr, on_progress=lambda m: print("[compute]", m))
    print(json.dumps(r.get("tech", {}), ensure_ascii=False, indent=2)[:600])
