"""
ETF 分析可视化服务 - Flask 后端
提供:
- GET  /                    -> 页面
- GET  /api/etfs            -> ETF 列表
- GET  /api/config          -> 当前生效配置
- POST /api/config          -> 保存用户配置
- POST /api/config/reset    -> 重置为默认
- GET  /api/screen          -> 全市场 Mark 模板筛选(取所有列表 ETF)
- GET  /api/chart/<code>    -> 单只ETF的所有时间序列明细(给前端 ECharts)
- GET  /api/export/<fmt>    -> 导出筛选结果 csv|json
"""
from __future__ import annotations

import csv
import io
import json
import math
import sys
from pathlib import Path

import pandas as pd

# 让 import core 可工作
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flask import Flask, Response, jsonify, render_template, request

from core import config as cfg_mod
from core import data_source as ds
from core import filters as filt
from core import indicators as ind
from core import prewarm
from core import watchlist as wl

app = Flask(__name__, static_folder="static", template_folder="templates")


# ---------- 工具: NaN -> None ----------
def _sanitize(obj):
    """把 numpy/pandas 中的 NaN 转成 None,JSON 才能序列化。"""
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    if isinstance(obj, tuple):
        return [_sanitize(v) for v in obj]
    return obj


def _df_to_kline(df) -> dict:
    """把 K 线 df 序列化为 ECharts 直接吃的数据结构。"""
    if df.empty:
        return {"categories": [], "open": [], "high": [], "low": [],
                "close": [], "volume": [], "ma20": [], "ma50": [],
                "ma150": [], "ma200": [], "bias20": [], "bias60": []}
    close = df["close"]
    ma20 = ind.ma(close, 20)
    ma50 = ind.ma(close, 50)
    ma150 = ind.ma(close, 150)
    ma200 = ind.ma(close, 200)
    bias20 = ind.bias(close, 20)
    bias60 = ind.bias(close, 60)
    dd = ind.drawdown_ytd(close)
    hi52 = ind.period_high(close, window=260)
    lo52 = ind.period_low(close, window=260)
    # 用 cummax-by-year 的年内最高/最低
    yearly = close.groupby(close.index.year).cummax()
    yearly_low = close.groupby(close.index.year).cummin()

    def fmt(s):
        return [None if (isinstance(v, float) and (math.isnan(v) or math.isinf(v))) else v for v in s]

    dates = [d.strftime("%Y-%m-%d") for d in df.index]

    return {
        "categories": dates,
        "open": fmt(df["open"]),
        "high": fmt(df["high"]),
        "low": fmt(df["low"]),
        "close": fmt(close),
        "volume": fmt(df["volume"]),
        "ma20": fmt(ma20),
        "ma50": fmt(ma50),
        "ma150": fmt(ma150),
        "ma200": fmt(ma200),
        "bias20": fmt(bias20),
        "bias60": fmt(bias60),
        "drawdown_ytd": fmt(dd),
        "high_52w": fmt(hi52),
        "low_52w": fmt(lo52),
        "yearly_high": fmt(yearly),
        "yearly_low": fmt(yearly_low),
    }


# ---------- 路由: 页面 ----------
@app.get("/")
def index():
    return render_template("index.html")


# ---------- 路由: ETF 列表 ----------
@app.get("/api/etfs")
def api_etfs():
    df = ds.list_etfs()
    items = [{"code": str(r["code"]), "name": str(r["name"])} for _, r in df.iterrows()]
    return jsonify({"count": len(items), "items": items})


# ---------- 路由: 配置 ----------
@app.get("/api/config")
def api_config():
    return jsonify({
        "current": cfg_mod.load_user(),
        "diff": cfg_mod.diff_for_ui(cfg_mod.load_defaults(), cfg_mod.load_user()),
    })


@app.post("/api/config")
def api_config_save():
    try:
        payload = request.get_json(force=True)
    except Exception:
        return jsonify({"ok": False, "error": "JSON 解析失败"}), 400
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "顶层必须是对象"}), 400
    cfg_mod.save_user(payload)
    return jsonify({"ok": True, "current": cfg_mod.load_user()})


@app.post("/api/config/reset")
def api_config_reset():
    return jsonify({"ok": True, "current": cfg_mod.reset_user()})


# ---------- 路由: 全市场筛选 ----------
@app.get("/api/screen")
def api_screen():
    """对全市场 ETF 跑 Mark 模板筛选。

    参数:
        limit:     最多拉取/扫描的 ETF 数量(默认 30,防止首次全量拉取卡死)。
        use_cache: 1=仅用本地已缓存 K 线(秒级返回,推荐),0=现场拉取。
        only_pass: 1=仅返回 4 条规则全过的。
    """
    cfg = cfg_mod.load_user()
    fcfg = filt.FilterConfig(**cfg["mark_filter"])
    years = int(cfg["display"].get("kline_years", 3))

    pool = ds.list_etfs()
    total = len(pool)

    use_cache = request.args.get("use_cache", "1") == "1"
    try:
        limit = int(request.args.get("limit", "30"))
    except ValueError:
        limit = 30

    # 先按缓存过滤(命中缓存的优先),再截断扫描范围
    if use_cache:
        cached = ds.list_cached_codes()
        cached_pool = pool[pool["code"].isin(cached)]
        rest_pool = pool[~pool["code"].isin(cached)]
        # 有缓存的排前面,没缓存但数量不足时用现场拉取补齐
        pool = pd.concat([cached_pool, rest_pool], ignore_index=True)
    pool = pool.head(limit)
    pool = ds.attach_klines(pool, years=years)

    results = filt.evaluate_pool(pool, fcfg)
    # 已启用的规则数(用于 "符合条件 N 只" 计数)
    enabled_count = fcfg.enabled_count
    # matched = 扫描中通过所有已启用规则的数量
    matched = sum(1 for r in results if r.passed_count == enabled_count)
    results.sort(key=lambda r: (
        -int(r.passed_count == enabled_count),
        -r.passed_count,
        -(r.bias20 if not math.isnan(r.bias20) else -1e9),
        r.code,
    ))

    only_pass = request.args.get("only_pass") == "1"
    if only_pass:
        results = [r for r in results if r.passed_count == enabled_count]

    out = []
    for r in results:
        out.append({
            "code": r.code,
            "name": r.name,
            "close": r.close,
            "ma50": r.ma50,
            "ma150": r.ma150,
            "ma200": r.ma200,
            "bias20": r.bias20,
            "bias60": r.bias60,
            "ytd_drawdown": r.ytd_drawdown,
            "passed_count": r.passed_count,
            "fully_passed": r.fully_passed,
            "rules": {
                "rule1": {"ok": r.rule1[0], "reason": r.rule1[1]},
                "rule2": {"ok": r.rule2[0], "reason": r.rule2[1]},
                "rule3": {"ok": r.rule3[0], "reason": r.rule3[1]},
                "rule4": {"ok": r.rule4[0], "reason": r.rule4[1]},
                "rule5": {"ok": r.rule5[0], "reason": r.rule5[1]},
                "rule6": {"ok": r.rule6[0], "reason": r.rule6[1]},
            },
        })
    return jsonify(_sanitize({
        "count": len(out),
        "total": total,
        "scanned": len(pool),
        "enabled_count": enabled_count,
        "enabled_rules": fcfg.enabled_rules,
        "matched": matched,
        "items": out,
    }))


# ---------- 路由: 单只 ETF 图表数据 ----------
@app.get("/api/chart/<code>")
def api_chart(code: str):
    cfg = cfg_mod.load_user()
    years = int(cfg["display"].get("kline_years", 3))
    df = ds.fetch_kline(code, years=years)
    if df.empty:
        return jsonify({"ok": False, "error": f"{code} 历史数据为空(网络问题或代码错误)"}), 404

    # 给前端展示用的关键摘要
    close = df["close"]
    hi52 = ind.period_high(close, window=260)
    lo52 = ind.period_low(close, window=260)
    # YTD 基准: 当年自然年的 cummax / cummin,索引已和 close 对齐。
    ytd_high_series = close.groupby(close.index.year).cummax()
    ytd_low_series = close.groupby(close.index.year).cummin()

    bias20_now = ind.safe_last(ind.bias(close, 20))
    bias60_now = ind.safe_last(ind.bias(close, 60))
    ytd_dd = ind.drawdown_ytd_current(close)

    # 52 周距离:对 close 整体算滚动 260 日的最高/最低
    last_close_val = ind.safe_last(close)
    hi52_now = ind.safe_last(hi52)
    lo52_now = ind.safe_last(lo52)
    if hi52_now:
        dist_52w_hi = (last_close_val - hi52_now) / hi52_now * 100.0
    else:
        dist_52w_hi = None
    if lo52_now:
        dist_52w_lo = (last_close_val - lo52_now) / lo52_now * 100.0
    else:
        dist_52w_lo = None

    # YTD 距离:不能用 close 全局的 cummax,必须直接取当年的 cummax 末值作基准
    ytd_high_now = ytd_high_series.iloc[-1] if len(ytd_high_series) else None
    ytd_low_now = ytd_low_series.iloc[-1] if len(ytd_low_series) else None
    dist_ytd_hi = None
    dist_ytd_lo = None
    if ytd_high_now and last_close_val:
        dist_ytd_hi = (last_close_val - ytd_high_now) / ytd_high_now * 100.0
    if ytd_low_now and last_close_val:
        dist_ytd_lo = (last_close_val - ytd_low_now) / ytd_low_now * 100.0

    chart = _df_to_kline(df)
    summary = {
        "code": code,
        "name": ds.list_etfs().query("code == @code")["name"].iloc[0]
                if (ds.list_etfs()["code"] == code).any() else code,
        "last_close": last_close_val,
        "bias20_now": bias20_now,
        "bias60_now": bias60_now,
        "ytd_drawdown": ytd_dd,
        "dist_52w_high_pct": dist_52w_hi,
        "dist_52w_low_pct": dist_52w_lo,
        "dist_ytd_high_pct": dist_ytd_hi,
        "dist_ytd_low_pct": dist_ytd_lo,
        "high_52w": hi52_now,
        "low_52w": lo52_now,
        "ytd_high": ytd_high_now if ytd_high_now is not None else float("nan"),
        "ytd_low": ytd_low_now if ytd_low_now is not None else float("nan"),
        "total_days": len(df),
        "last_date": df.index[-1].strftime("%Y-%m-%d"),
    }
    return jsonify(_sanitize({"ok": True, "summary": summary, "chart": chart}))


# ---------- 路由: 导出筛选结果 ----------
@app.get("/api/export/<fmt>")
def api_export(fmt: str):
    fmt = fmt.lower()
    if fmt not in ("csv", "json"):
        return jsonify({"ok": False, "error": "仅支持 csv/json"}), 400
    # 复用筛选结果:默认用缓存模式 + 上限,避免全量现场拉取卡死
    cfg = cfg_mod.load_user()
    fcfg = filt.FilterConfig(**cfg["mark_filter"])
    years = int(cfg["display"].get("kline_years", 3))
    pool = ds.list_etfs()
    cached = ds.list_cached_codes()
    cached_pool = pool[pool["code"].isin(cached)]
    rest_pool = pool[~pool["code"].isin(cached)]
    pool = pd.concat([cached_pool, rest_pool], ignore_index=True)
    pool = ds.attach_klines(pool, years=years)

    results = filt.evaluate_pool(pool, fcfg)
    rows = []
    for r in results:
        rows.append({
            "code": r.code, "name": r.name,
            "close": r.close, "ma50": r.ma50, "ma150": r.ma150, "ma200": r.ma200,
            "bias20": r.bias20, "bias60": r.bias60, "ytd_drawdown": r.ytd_drawdown,
            "rule1_pass": r.rule1[0], "rule2_pass": r.rule2[0],
            "rule3_pass": r.rule3[0], "rule4_pass": r.rule4[0],
            "passed_count": r.passed_count,
        })

    if fmt == "json":
        payload = json.dumps(_sanitize({"items": rows}), ensure_ascii=False, indent=2)
        return Response(payload, mimetype="application/json",
                        headers={"Content-Disposition": "attachment; filename=etf_screen.json"})
    # csv
    buf = io.StringIO()
    if rows:
        writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    data = buf.getvalue().encode("utf-8-sig")  # 加 BOM 让 Excel 直接打开
    return Response(data, mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=etf_screen.csv"})


# ---------- 路由: 历史数据预热 ----------
@app.get("/api/warmup/status")
def api_warmup_status():
    """返回当前预热进度 + 当前缓存统计。"""
    cached = ds.list_cached_codes()
    return jsonify({
        "preheat": prewarm.get_status(),
        "cache_count": len(cached),
        "total": len(ds.list_etfs()),
    })


@app.post("/api/warmup/start")
def api_warmup_start():
    """
    启动后台预热。
    body: {"limit": int=0, "years": int=3, "sleep": float=0.05}
    - limit=0 表示全市场;>0 表示前 N 只
    - 已运行时重复调用直接返回当前状态
    """
    try:
        body = request.get_json(force=True, silent=True) or {}
    except Exception:
        body = {}
    limit = int(body.get("limit", 0))
    years = int(body.get("years", 3))
    sleep = float(body.get("sleep", 0.05))
    result = prewarm.start_preheat(limit=limit, years=years, sleep=sleep)
    return jsonify(result)


# ---------- 路由: 自选 ETF 池(多组) ----------
@app.get("/api/watchlist")
def api_watchlist_get():
    """返回所有自选组 [{name, codes, count}]。"""
    return jsonify({"groups": wl.list_groups()})


@app.post("/api/watchlist/group/create")
def api_watchlist_group_create():
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    try:
        groups = wl.create_group(name)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "groups": groups})


@app.post("/api/watchlist/group/delete")
def api_watchlist_group_delete():
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "组名必填"}), 400
    groups = wl.delete_group(name)
    return jsonify({"ok": True, "groups": groups})


@app.post("/api/watchlist/group/rename")
def api_watchlist_group_rename():
    body = request.get_json(force=True, silent=True) or {}
    old = (body.get("old_name") or "").strip()
    new = (body.get("new_name") or "").strip()
    try:
        groups = wl.rename_group(old, new)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "groups": groups})


@app.post("/api/watchlist/add")
def api_watchlist_add():
    body = request.get_json(force=True, silent=True) or {}
    code = (body.get("code") or "").strip()
    group = (body.get("group") or wl.DEFAULT_GROUP).strip() or wl.DEFAULT_GROUP
    if not code or not code.isdigit() or len(code) != 6:
        return jsonify({"ok": False, "error": "code 必须是 6 位数字"}), 400
    groups = wl.add_to_group(code, group)
    return jsonify({"ok": True, "groups": groups})


@app.post("/api/watchlist/remove")
def api_watchlist_remove():
    body = request.get_json(force=True, silent=True) or {}
    code = (body.get("code") or "").strip()
    group = (body.get("group") or wl.DEFAULT_GROUP).strip() or wl.DEFAULT_GROUP
    if not code:
        return jsonify({"ok": False, "error": "code 必填"}), 400
    groups = wl.remove_from_group(code, group)
    return jsonify({"ok": True, "groups": groups})


@app.get("/api/watchlist/screen")
def api_watchlist_screen():
    """对自选池里指定组的 ETF 跑 Mark 模板 + BIAS 当前值。"""
    group = request.args.get("group", wl.DEFAULT_GROUP)
    cfg = cfg_mod.load_user()
    fcfg = filt.FilterConfig(**cfg["mark_filter"])
    years = int(cfg["display"].get("kline_years", 3))

    codes = wl.group_codes(group)
    if not codes:
        return jsonify({"count": 0, "items": [], "cached": 0, "uncached": [], "group": group})

    pool = ds.list_etfs()
    name_map = dict(zip(pool["code"].astype(str).str.zfill(6), pool["name"].astype(str)))

    sub_pool = pool[pool["code"].isin(codes)].copy()
    sub_pool["name"] = sub_pool["code"].map(lambda c: name_map.get(str(c).zfill(6), c))
    sub_pool = ds.attach_klines(sub_pool, years=years)

    cached_codes = set(ds.list_cached_codes())
    uncached = [c for c in codes if c not in cached_codes]

    results = filt.evaluate_pool(sub_pool, fcfg)
    enabled_count = fcfg.enabled_count
    matched = sum(1 for r in results if r.passed_count == enabled_count)
    order = {c: i for i, c in enumerate(codes)}
    results.sort(key=lambda r: order.get(r.code, 9999))

    out = []
    for r in results:
        out.append({
            "code": r.code,
            "name": r.name,
            "close": r.close,
            "ma50": r.ma50,
            "ma150": r.ma150,
            "ma200": r.ma200,
            "bias20": r.bias20,
            "bias60": r.bias60,
            "ytd_drawdown": r.ytd_drawdown,
            "passed_count": r.passed_count,
            "fully_passed": r.fully_passed,
            "rules": {
                "rule1": {"ok": r.rule1[0], "reason": r.rule1[1]},
                "rule2": {"ok": r.rule2[0], "reason": r.rule2[1]},
                "rule3": {"ok": r.rule3[0], "reason": r.rule3[1]},
                "rule4": {"ok": r.rule4[0], "reason": r.rule4[1]},
                "rule5": {"ok": r.rule5[0], "reason": r.rule5[1]},
                "rule6": {"ok": r.rule6[0], "reason": r.rule6[1]},
            },
            "cached": r.code in cached_codes,
        })
    return jsonify(_sanitize({
        "count": len(out),
        "items": out,
        "cached": len(codes) - len(uncached),
        "uncached": uncached,
        "group": group,
        "enabled_count": enabled_count,
        "enabled_rules": fcfg.enabled_rules,
        "matched": matched,
    }))


if __name__ == "__main__":
    # 使用 waitress 作为生产级 WSGI 服务器(比 Flask 内置 dev server 稳定,
    # 避免长时间运行卡死、多线程并发更稳)。Windows/Linux 都支持。
    try:
        from waitress import serve
        # 绑定 0.0.0.0 让 docker 容器外可以访问。
        # threads=4 让多个 HTTP 请求并发;当一个请求阻塞时,其他请求仍能处理。
        serve(app, host="0.0.0.0", port=5001, threads=4, ident="etf-analysis")
    except ImportError:
        # 没有 waitress 时回退到 Flask dev server(开发用)
        app.run(host="0.0.0.0", port=5001, debug=False)
