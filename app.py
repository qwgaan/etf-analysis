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
import threading
from pathlib import Path

import pandas as pd

# 让 import core 可工作
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flask import Flask, Response, jsonify, render_template, request

from core import alert
from core import alert_schedule
from core import config as cfg_mod
from core import data_source as ds
from core import filters as filt
from core import indicators as ind
from core import prewarm
from core import screen_cache
from core import watchlist as wl

app = Flask(__name__, static_folder="static", template_folder="templates")


# ---------- 全市场筛选进度(单进程内存共享,前端轮询) ----------
screen_progress = {
    "running": False,
    "phase": "",        # 当前阶段文案:读取K线缓存 / 计算Mark模板 / 生成筛选缓存
    "done": 0,          # 当前阶段已完成数
    "total": 0,         # 当前阶段总量
    "matched": 0,       # 已匹配(全过)数量,实时累计
}
_screen_lock = threading.Lock()


def _screen_set(done: int, total: int, phase: str | None = None) -> None:
    """供 progress_cb 调用,线程安全地更新进度。"""
    with _screen_lock:
        screen_progress["done"] = done
        screen_progress["total"] = total
        if phase:
            screen_progress["phase"] = phase


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


@app.get("/api/etfs/search")
def api_etfs_search():
    """按代码前缀或名称关键字搜索 ETF,用于自选输入框下拉提示。

    参数: q(必填),limit(默认 15)
    - 纯数字: 按 code 前缀匹配,如 510 -> 510300/510500...
    - 其他字符: 按 name 模糊匹配,如 有色 -> 有色50ETF/有色ETF...
    """
    q = (request.args.get("q") or "").strip().lower()
    try:
        limit = int(request.args.get("limit", "15"))
    except ValueError:
        limit = 15
    limit = max(1, min(limit, 100))

    if not q:
        return jsonify({"count": 0, "items": []})

    df = ds.list_etfs()
    if q.isdigit():
        matched = df[df["code"].astype(str).str.startswith(q)]
    else:
        matched = df[df["name"].astype(str).str.lower().str.contains(q, na=False)]

    items = [{"code": str(r["code"]), "name": str(r["name"])} for _, r in matched.head(limit).iterrows()]
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
    # 与现有用户配置合并,避免只传部分字段时抹掉其他设置(如 wxpusher token)
    merged = cfg_mod._deep_merge(cfg_mod.load_user(allow_corrupt_reset=False), payload)
    cfg_mod.save_user(merged)
    # mark_filter 变化会使全过筛选缓存失效
    screen_cache.clear(merged.get("mark_filter"))
    return jsonify({"ok": True, "current": cfg_mod.load_user()})


@app.post("/api/config/reset")
def api_config_reset():
    screen_cache.clear()  # 重置配置后清除所有筛选缓存
    return jsonify({"ok": True, "current": cfg_mod.reset_user()})


# ---------- 路由: 全市场筛选 ----------
@app.get("/api/screen")
def api_screen():
    """对全市场 ETF 跑 Mark 模板筛选。

    参数:
        limit:     最多拉取/扫描的 ETF 数量(默认读取 display.screen_limit,0=全量)。
        use_cache: 1=仅用本地已缓存 K 线(秒级返回,推荐),0=现场拉取。
        only_pass: 1=仅返回 6 条规则全过的。
    """
    cfg = cfg_mod.load_user()
    fcfg = filt.FilterConfig(**cfg["mark_filter"])
    years = int(cfg["display"].get("kline_years", 3))

    pool = ds.list_etfs()
    total = len(pool)

    use_cache = request.args.get("use_cache", "1") == "1"
    default_limit = int(cfg["display"].get("screen_limit", 0) or 0)
    limit_raw = request.args.get("limit")
    try:
        limit = int(limit_raw) if limit_raw is not None else default_limit
    except ValueError:
        limit = default_limit

    only_pass = request.args.get("only_pass") == "1"

    # only_pass=1 且使用本地缓存全量时,优先读筛选缓存,命中则直接返回(跳过 attach_klines)
    if only_pass and use_cache and limit <= 0:
        cache = screen_cache.load(cfg["mark_filter"])
        if cache is not None:
            passed_codes = set(cache.get("passed_codes", []))
            passed_pool = pool[pool["code"].isin(passed_codes)].copy()
            out = []
            for _, row in passed_pool.iterrows():
                out.append({
                    "code": str(row["code"]),
                    "name": str(row["name"]),
                    "close": None, "ma50": None, "ma150": None, "ma200": None,
                    "bias20": None, "bias60": None, "ytd_drawdown": None, "dd52w": None,
                    "passed_count": cache.get("enabled_count", fcfg.enabled_count),
                    "fully_passed": True,
                    "rules": {},
                })
            return jsonify(_sanitize({
                "count": len(out),
                "total": total,
                "scanned": cache.get("scanned", total),
                "enabled_count": cache.get("enabled_count", fcfg.enabled_count),
                "enabled_rules": cache.get("enabled_rules", fcfg.enabled_rules),
                "matched": cache.get("matched", len(out)),
                "items": out,
                "cached": True,
            }))

    # 先按缓存过滤(命中缓存的优先),再截断扫描范围
    if use_cache:
        cached = ds.list_cached_codes()
        cached_pool = pool[pool["code"].isin(cached)]
        rest_pool = pool[~pool["code"].isin(cached)]
        # 有缓存的排前面,没缓存但数量不足时用现场拉取补齐
        pool = pd.concat([cached_pool, rest_pool], ignore_index=True)
    if limit > 0:
        pool = pool.head(limit)

    # 阶段 1:读取 K 线(已缓存走本地,未缓存走网络)
    with _screen_lock:
        screen_progress["running"] = True
        screen_progress["phase"] = "读取K线缓存"
        screen_progress["done"] = 0
        screen_progress["total"] = len(pool)
        screen_progress["matched"] = 0
    pool = ds.attach_klines(
        pool, years=years,
        progress_cb=lambda d, t: _screen_set(d, t, "读取K线缓存"),
    )

    # 阶段 2:计算 Mark 模板
    with _screen_lock:
        screen_progress["phase"] = "计算Mark模板"
        screen_progress["done"] = 0
        screen_progress["total"] = len(pool)
    results = filt.evaluate_pool(
        pool, fcfg,
        progress_cb=lambda d, t: _screen_set(d, t, "计算Mark模板"),
    )
    # 已启用的规则数(用于 "符合条件 N 只" 计数)
    enabled_count = fcfg.enabled_count
    # matched = 扫描中通过所有已启用规则的数量
    matched = sum(1 for r in results if r.passed_count == enabled_count)
    with _screen_lock:
        screen_progress["matched"] = matched
    results.sort(key=lambda r: (
        -int(r.passed_count == enabled_count),
        -r.passed_count,
        -(r.bias20 if not math.isnan(r.bias20) else -1e9),
        r.code,
    ))

    if only_pass:
        results = [r for r in results if r.passed_count == enabled_count]
        # 全量 only_pass 且缓存未命中时,生成缓存供下次使用
        if use_cache and limit <= 0:
            with _screen_lock:
                screen_progress["phase"] = "生成筛选缓存"
                screen_progress["done"] = 0
                screen_progress["total"] = len(pool)
            screen_cache.compute_and_save(
                pool, fcfg, cfg["mark_filter"],
                progress_cb=lambda d, t: _screen_set(d, t, "生成筛选缓存"),
            )

    with _screen_lock:
        screen_progress["running"] = False
        screen_progress["phase"] = "完成"
        screen_progress["done"] = screen_progress["total"]

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
            "dd52w": r.dd52w,
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


@app.get("/api/screen/progress")
def api_screen_progress():
    """返回全市场筛选的实时进度,供前端轮询展示。"""
    with _screen_lock:
        return jsonify(dict(screen_progress))


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

    # 真正的今年最大回撤(含最低点日期与价格)
    ytd_max_dd, ytd_max_dd_date, ytd_max_dd_price = ind.ytd_max_drawdown_with_low(close)

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
        "ytd_max_drawdown": ytd_max_dd,
        "ytd_max_drawdown_date": ytd_max_dd_date,
        "ytd_max_drawdown_price": ytd_max_dd_price,
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


# ---------- 路由: 单只 ETF 逐年表现 ----------
@app.get("/api/yearly/<code>")
def api_yearly(code: str):
    """返回 ETF 上市以来的逐年收益和年内最大回撤。"""
    df = ds.fetch_kline_full(code)
    if df.empty:
        return jsonify({"ok": False, "error": f"{code} 历史数据为空"}), 404

    close = df["close"]
    rows = ind.yearly_performance(close)
    name = ""
    pool = ds.list_etfs()
    matched = pool[pool["code"] == code]
    if not matched.empty:
        name = str(matched.iloc[0]["name"])

    return jsonify(_sanitize({
        "ok": True,
        "code": code,
        "name": name,
        "count": len(rows),
        "items": rows,
    }))


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
            "bias20": r.bias20, "bias60": r.bias60,
            "ytd_drawdown": r.ytd_drawdown, "dd52w": r.dd52w,
            "rule1_pass": r.rule1[0], "rule2_pass": r.rule2[0],
            "rule3_pass": r.rule3[0], "rule4_pass": r.rule4[0],
            "rule5_pass": r.rule5[0], "rule6_pass": r.rule6[0],
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
def _get_cache_latest_date() -> str | None:
    """扫描本地 K 线缓存,返回最晚的交易日日期(只读每文件最后一行,快)。"""
    latest = None
    if not ds.CACHE_DIR.exists():
        return None
    for p in ds.CACHE_DIR.glob("*_hfq.csv"):
        try:
            with open(p, "r", encoding="utf-8") as f:
                lines = f.readlines()
            # 跳过表头,取最后一行非空数据
            for line in reversed(lines):
                line = line.strip()
                if not line or line.startswith("date"):
                    continue
                date_str = line.split(",")[0]
                d = pd.to_datetime(date_str)
                if latest is None or d > latest:
                    latest = d
                break
        except Exception:
            continue
    if latest is None:
        return None
    return latest.strftime("%Y-%m-%d")


@app.get("/api/warmup/status")
def api_warmup_status():
    """返回当前预热进度 + 当前缓存统计 + 缓存最新日期。"""
    cached = ds.list_cached_codes()
    return jsonify({
        "preheat": prewarm.get_status(),
        "cache_count": len(cached),
        "total": len(ds.list_etfs()),
        "cache_latest_date": _get_cache_latest_date(),
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
            "dd52w": r.dd52w,
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


# ---------- 自选 ETF 警戒推送(BIAS 三档逃顶 + 年度最大回撤) ----------
def _alert_thresholds(body: dict, cfg: dict) -> dict:
    """合并请求中的阈值与配置默认值。"""
    thresholds = {
        "bias20_levels": cfg.get("bias_thresholds", {}).get("bias20_levels", [10.0, 15.0]),
        "bias60_levels": cfg.get("bias_thresholds", {}).get("bias60_levels", [20.0]),
        "ytd_levels": cfg.get("drawdown_thresholds", {}).get("ytd_levels", [10.0, 15.0, 20.0]),
        "ytd_level_tags": cfg.get("drawdown_thresholds", {}).get("ytd_level_tags", ["红利档", "中性档", "创业板档"]),
    }
    if isinstance(body.get("thresholds"), dict):
        for k in thresholds:
            if k in body["thresholds"]:
                thresholds[k] = body["thresholds"][k]
    return thresholds


@app.post("/api/watchlist/alert/preview")
def api_watchlist_alert_preview():
    """预览自选组内每只 ETF 的触发状态(不推送)。带每只订阅开关、hot 标记、独立阈值。"""
    body = request.get_json(force=True, silent=True) or {}
    group = (body.get("group") or wl.DEFAULT_GROUP).strip() or wl.DEFAULT_GROUP
    cfg = cfg_mod.load_user()
    years = int(cfg["display"].get("kline_years", 3))
    thresholds = _alert_thresholds(body, cfg)
    codes = wl.group_codes(group)
    subs = wl.get_group_alerts(group)
    code_th = wl.get_group_thresholds(group)
    items = alert.scan_group(codes, thresholds, years=years, subscriptions=subs, code_thresholds=code_th)
    return jsonify(_sanitize({
        "ok": True,
        "group": group,
        "thresholds": thresholds,
        "code_thresholds": code_th,
        "items": items,
        "subs": subs,
        "triggered_count": sum(1 for it in items if it.get("triggered_any")),
    }))


@app.post("/api/watchlist/alert/subscribe")
def api_watchlist_alert_subscribe():
    """保存某只 ETF 的订阅开关 {bias20, bias60, dd}。勾选即自动保存。"""
    body = request.get_json(force=True, silent=True) or {}
    group = (body.get("group") or wl.DEFAULT_GROUP).strip() or wl.DEFAULT_GROUP
    code = (body.get("code") or "").strip()
    alerts = body.get("alerts") or {}
    if not code or not code.isdigit() or len(code) != 6:
        return jsonify({"ok": False, "error": "code 必须是 6 位数字"}), 400
    # 只允许合法键
    clean = {k: bool(alerts.get(k)) for k in ("bias20", "bias60", "dd")}
    groups = wl.set_alerts(group, code, clean)
    return jsonify({"ok": True, "groups": groups})


@app.post("/api/watchlist/alert/thresholds")
def api_watchlist_alert_thresholds():
    """保存某只 ETF 的独立阈值 {bias20_levels, bias60_levels, ytd_levels}。"""
    body = request.get_json(force=True, silent=True) or {}
    group = (body.get("group") or wl.DEFAULT_GROUP).strip() or wl.DEFAULT_GROUP
    code = (body.get("code") or "").strip()
    thresholds = body.get("thresholds") or {}
    if not code or not code.isdigit() or len(code) != 6:
        return jsonify({"ok": False, "error": "code 必须是 6 位数字"}), 400
    clean = {}
    for k in ("bias20_levels", "bias60_levels", "ytd_levels"):
        if k in thresholds:
            clean[k] = thresholds[k]
    groups = wl.set_thresholds(group, code, clean)
    return jsonify({"ok": True, "groups": groups})


@app.get("/api/watchlist/alert/subs")
def api_watchlist_alert_subs():
    """返回所有组的订阅开关 {group: {code: {bias20,bias60,dd}}}。"""
    out = {}
    for g in wl.list_groups():
        out[g["name"]] = wl.get_group_alerts(g["name"])
    return jsonify({"ok": True, "groups": out})


@app.post("/api/watchlist/alert/test")
def api_watchlist_alert_test():
    """精简测试推送:立即按本组已勾选的订阅条件触发推送(忽略「今天已推送」去重)。"""
    body = request.get_json(force=True, silent=True) or {}
    group = (body.get("group") or wl.DEFAULT_GROUP).strip() or wl.DEFAULT_GROUP
    cfg = cfg_mod.load_user()
    token = (cfg.get("wxpusher", {}) or {}).get("spt_token", "")
    if not token:
        return jsonify({"ok": False, "error": "未配置 WxPusher SPT_TOKEN,请在参数配置中填写。"}), 400

    years = int(cfg["display"].get("kline_years", 3))
    thresholds = _alert_thresholds(body, cfg)

    subs_map = wl.get_group_alerts(group)
    code_th = wl.get_group_thresholds(group)
    subscriptions = [
        {"code": c, "alerts": a, "thresholds": code_th.get(c, {})}
        for c, a in subs_map.items()
        if any(a.values())
    ]
    if not subscriptions:
        return jsonify(_sanitize({
            "ok": True,
            "sent": False,
            "message": "本组没有勾选任何警戒条件,无法测试推送。",
            "items": [],
        }))

    result = alert.run_subscription_scan(thresholds, subscriptions, years=years, force=True, token=token)
    return jsonify(_sanitize({
        "ok": True,
        "sent": bool(result["items"]),
        "triggered_count": len(result["items"]),
        "wxpusher": result["wxpusher"],
        "markdown": result["markdown"],
        "items": result["items"],
    }))


@app.post("/api/watchlist/alert/push")
def api_watchlist_alert_push():
    """(保留)对自选组触发警戒的 ETF 推送 WxPusher 消息。供定时调度/兼容调用。"""
    body = request.get_json(force=True, silent=True) or {}
    group = (body.get("group") or wl.DEFAULT_GROUP).strip() or wl.DEFAULT_GROUP
    cfg = cfg_mod.load_user()
    token = (cfg.get("wxpusher", {}) or {}).get("spt_token", "")
    if not token:
        return jsonify({"ok": False, "error": "未配置 WxPusher SPT_TOKEN,请在参数配置中填写。"}), 400

    years = int(cfg["display"].get("kline_years", 3))
    thresholds = _alert_thresholds(body, cfg)

    selected_codes = body.get("codes")
    if isinstance(selected_codes, list) and selected_codes:
        codes = [str(c).zfill(6) for c in selected_codes]
    else:
        codes = wl.group_codes(group)

    subs = wl.get_group_alerts(group) if not selected_codes else None
    code_th = wl.get_group_thresholds(group) if not selected_codes else None
    items = alert.scan_group(codes, thresholds, years=years, subscriptions=subs, code_thresholds=code_th)
    triggered = [it for it in items if it.get("triggered_any")]

    if not triggered:
        return jsonify(_sanitize({
            "ok": True,
            "sent": False,
            "message": "没有 ETF 触发当前警戒档,未执行推送。",
            "selected_count": len(codes),
            "triggered_count": 0,
            "items": items,
        }))

    markdown = alert.build_markdown(triggered, thresholds)
    wx_resp = alert.push_wxpusher(token, markdown)
    return jsonify(_sanitize({
        "ok": bool(wx_resp.get("ok") or wx_resp.get("success") or wx_resp.get("code") == 1000),
        "sent": True,
        "triggered_count": len(triggered),
        "selected_count": len(codes),
        "wxpusher": wx_resp,
        "markdown": markdown,
        "items": triggered,
    }))


# ---------- 自选 ETF 警戒定时自动推送(后台线程) ----------
def _scheduled_alert_run() -> None:
    """定时调度回调:扫描所有订阅,触发则推送 WxPusher(自带当天去重)。"""
    try:
        import time as _t
        cfg = cfg_mod.load_user()
        token = (cfg.get("wxpusher", {}) or {}).get("spt_token", "")
        if not token:
            return
        thresholds = _alert_thresholds({}, cfg)
        subs = wl.all_subscriptions()
        if not subs:
            return
        years = int(cfg["display"].get("kline_years", 3))
        result = alert.run_subscription_scan(thresholds, subs, years=years, force=False, token=token)
        print(f"[alert-scheduler] {_t.strftime('%Y-%m-%d %H:%M')} 扫描 {len(subs)} 只订阅, "
              f"本次推送 {len(result.get('items', []))} 只")
    except Exception as e:
        print(f"[alert-scheduler] 执行异常: {e}")


def _get_alert_schedule():
    """返回用户配置的推送时间列表;显式设置为空数组时返回空(不自动推送)。"""
    cfg = cfg_mod.load_user()
    # 如果用户从未保存,使用默认 3 个时间
    if "alert_schedule" not in cfg:
        return ["10:00", "13:30", "16:00"]
    sched = cfg.get("alert_schedule") or []
    # 过滤空字符串,最多 3 个有效时间
    return [s for s in sched if isinstance(s, str) and s.strip()][:3]


def _get_alert_holidays():
    return cfg_mod.load_user().get("alert_holidays") or []


if __name__ == "__main__":
    # 启动定时自动推送调度器(守护线程)
    _scheduler = alert_schedule.AlertScheduler(_scheduled_alert_run, interval=30)
    _scheduler.start(_get_alert_schedule, _get_alert_holidays)

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
