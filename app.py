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
import datetime as dt
import io
import json
import math
import random
import sys
import threading
import time
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
from core import intraday_price as ip
from core import prewarm
from core import screen_cache
from core import watchlist as wl

# 当前应用版本(与 GitHub Release tag 对应)。每次发版时同步更新。
APP_VERSION = "0.5.3"
REPO_SLUG = "qwgaan/etf-analysis"

app = Flask(__name__, static_folder="static", template_folder="templates")

# ---- 投资分析独立模块（可选功能，受 config 的 invest.enabled 控制）----
# 与 ETF 原有功能零耦合：配置走 invest/config/，数据走 invest/data/，产物走
# invest/outputs/。设为 false 时不注册蓝图、不渲染 tab，行为与 0.3.26 完全一致，
# 可作为「用得不顺手」时的回退开关。
try:
    _invest_enabled = bool(cfg_mod.load_user().get("invest", {}).get("enabled", True))
except Exception:
    _invest_enabled = True
INVEST_ENABLED = _invest_enabled
if INVEST_ENABLED:
    try:
        from invest.routes import bp as _invest_bp
        app.register_blueprint(_invest_bp)
        print("[invest] 投资分析模块已启用 (Blueprint /api/invest 已注册)", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        INVEST_ENABLED = False
        print(f"[invest] 模块加载失败，已禁用：{e}", file=sys.stderr)


@app.errorhandler(404)
def _handle_404(e):
    """所有未匹配路由返回 JSON,避免前端拿到 HTML 错误页解析失败。"""
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": f"接口不存在: {request.path}"}), 404
    return e


@app.errorhandler(500)
def _handle_500(e):
    """服务端异常统一返回 JSON,方便前端 toast 提示。"""
    import traceback
    traceback.print_exc()
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "服务器内部错误,请查看日志"}), 500
    return e


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
    return render_template("index.html", invest_enabled=INVEST_ENABLED, version=APP_VERSION)


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


@app.get("/api/stocks/search")
def api_stocks_search():
    """按代码前缀或名称关键字搜索 A 股,用于自选输入框下拉提示(与 ETF 搜索并列)。"""
    q = (request.args.get("searchText") or request.args.get("q") or "").strip()
    try:
        limit = int(request.args.get("limit", "15"))
    except ValueError:
        limit = 15
    limit = max(1, min(limit, 100))
    items = ds.search_stocks(q, limit=limit)
    return jsonify({"count": len(items), "items": items})


@app.get("/api/stocks/list/status")
def api_stocks_list_status():
    """返回 A 股全市场股票名称缓存的预热状态,供前端轮询展示进度。"""
    return jsonify(ds.stock_list_status())


@app.post("/api/stocks/list/refresh")
def api_stocks_list_refresh():
    """手动强制刷新 A 股全市场股票名称缓存。删除旧缓存并启动后台重新拉取。"""
    try:
        cache = ds.PROJECT_ROOT / "data" / "stock_list.csv"
        if cache.exists():
            cache.unlink()
        ds.start_stock_list_warmup()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


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
    try:
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
            "name": ds.resolve_name(code),
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
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        app.logger.error("api_chart %s error:\n%s", code, tb)
        return jsonify({"ok": False, "error": str(e), "traceback": tb}), 500


@app.get("/api/chart/intraday/<code>")
def api_chart_intraday(code: str):
    """返回指定代码当天 1 分钟分时数据(按需实时拉取,不预热缓存)。

    若当天无 1 分钟 K 线数据,自动回退到上一交易日的 1 分钟 K 线,
    并标记 is_fallback,供前端展示回退提示。
    """
    df = ip.fetch_today_kline(code)
    is_fallback = False
    fallback_date = None
    if df is None or df.empty:
        fallback_ts = ds._expected_bar_date()
        fallback_date = fallback_ts.strftime("%Y-%m-%d")
        try:
            df = ip.fetch_today_kline(code, date=fallback_ts.date())
            is_fallback = df is not None and not df.empty
        except Exception as e:
            app.logger.warning("%s 回退到 %s 1 分钟 K 线失败: %s", code, fallback_date, e)
            df = None

    if df is None or df.empty:
        return jsonify({
            "ok": False,
            "error": f"{code} 当天暂无 1 分钟 K 线数据(非交易日/未开盘/接口异常)",
            "fallback_date": fallback_date,
        }), 404

    times = df["time"].dt.strftime("%H:%M").tolist()
    closes = df["close"].astype(float).tolist()
    volumes = df["volume"].astype(int).tolist()

    # 均价线(VWAP): 累计成交额 / 累计成交量
    amounts = df["amount"].astype(float).fillna(0.0)
    vols = df["volume"].astype(float).fillna(0.0)
    cum_amount = amounts.cumsum()
    cum_volume = vols.cumsum()
    vwap_series = cum_amount / cum_volume.replace(0, pd.NA)
    vwap = [None if pd.isna(x) else round(float(x), 4) for x in vwap_series]

    # 昨收(用于分时参考线):回退数据用第一根开盘价近似;当天数据优先新浪实时行情
    quote = ip.fetch_realtime_quotes([code]).get(code, {})
    prev_close = quote.get("prev_close")
    if not prev_close:
        prev_close = float(df["open"].iloc[0])

    summary = {
        "code": code,
        "name": ds.resolve_name(code),
        "date": df["time"].iloc[0].strftime("%Y-%m-%d"),
        "count": len(df),
        "last_close": float(df["close"].iloc[-1]),
        "prev_close": float(prev_close),
        "day_high": float(df["high"].max()),
        "day_low": float(df["low"].min()),
        "total_volume": int(df["volume"].sum()),
        "total_amount": round(float(df["amount"].sum()), 2) if "amount" in df.columns else None,
        "is_fallback": is_fallback,
        "fallback_date": fallback_date if is_fallback else None,
    }
    return jsonify(_sanitize({
        "ok": True,
        "summary": summary,
        "times": times,
        "close": closes,
        "vwap": vwap,
        "volume": volumes,
        "candle": df[["open", "close", "low", "high"]].values.tolist(),
    }))


# ---------- 路由: 单只 ETF 逐年表现 ----------
@app.get("/api/yearly/<code>")
def api_yearly(code: str):
    """返回 ETF 上市以来的逐年收益和年内最大回撤。"""
    df = ds.fetch_kline_full(code)
    if df.empty:
        return jsonify({"ok": False, "error": f"{code} 历史数据为空"}), 404

    close = df["close"]
    rows = ind.yearly_performance(close)
    name = ds.resolve_name(code)

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


# ---------- 路由: 版本检查 ----------
@app.get("/api/version")
def api_version():
    """返回当前运行版本。"""
    return jsonify({"version": APP_VERSION, "repo": REPO_SLUG})


@app.get("/api/version/latest")
def api_version_latest():
    """查询 GitHub 最新 Release tag,判断当前是否最新。失败时返回 ok=False。"""
    import urllib.request
    url = f"https://api.github.com/repos/{REPO_SLUG}/releases/latest"
    try:
        req = urllib.request.Request(url, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "etf-analysis-version-check",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.loads(resp.read().decode("utf-8"))
        tag = (d.get("tag_name") or "").lstrip("vV")
        return jsonify({"ok": True, "latest": tag, "url": d.get("html_url", ""), "name": d.get("name", "")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


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
    """加入自选(ETF 或 A 股股票)。
    - 自动按代码前缀识别类型;股票会解析名称并立即下载历史 K 线。
    - 返回 {ok, groups, download:{code,kind,name,rows,error}}。
    """
    body = request.get_json(force=True, silent=True) or {}
    code = (body.get("code") or "").strip()
    group = (body.get("group") or wl.DEFAULT_GROUP).strip() or wl.DEFAULT_GROUP
    if not code or not code.isdigit() or len(code) != 6:
        return jsonify({"ok": False, "error": "code 必须是 6 位数字"}), 400
    kind = (body.get("kind") or "").strip() or ("stock" if ds.is_stock_code(code) else "etf")
    name = (body.get("name") or "").strip() or ds.resolve_name(code)

    groups = wl.add_to_group(code, group, kind=kind, name=name)

    # 加入的同时下载该代码历史数据(股票走独立源,ETF 走既有源)
    download = {"code": code, "kind": kind, "name": name, "rows": 0, "error": None}
    try:
        years = int(cfg_mod.load_user()["display"].get("kline_years", 3))
        df = ds.fetch_kline(code, years=years)
        download["rows"] = len(df)
    except Exception as e:
        download["error"] = str(e)

    return jsonify(_sanitize({"ok": True, "groups": groups, "download": download}))


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

    # 用自选条目(含类型/名称)构建池,股票/ETF 都能覆盖
    items = wl.group_items(group)
    sub_pool = pd.DataFrame([{"code": it["code"], "name": it["name"]} for it in items])
    sub_pool = ds.attach_klines(sub_pool, years=years)

    cached_codes = set(ds.list_cached_codes())
    uncached = [c for c in codes if c not in cached_codes]

    results = filt.evaluate_pool(sub_pool, fcfg)
    enabled_count = fcfg.enabled_count
    matched = sum(1 for r in results if r.passed_count == enabled_count)
    order = {c: i for i, c in enumerate(codes)}
    results.sort(key=lambda r: order.get(r.code, 9999))
    result_by_code = {r.code: r for r in results}

    out = []
    for c in codes:
        r = result_by_code.get(c)
        if r is None:
            # 数据不足(<60 日)或 K 线为空,仍保留显示,指标置空
            name = next((it["name"] for it in items if it["code"] == c), ds.resolve_name(c))
            out.append({
                "code": c,
                "name": name,
                "close": None,
                "ma50": None,
                "ma150": None,
                "ma200": None,
                "bias20": None,
                "bias60": None,
                "ytd_drawdown": None,
                "dd52w": None,
                "passed_count": 0,
                "fully_passed": False,
                "rules": {
                    "rule1": {"ok": False, "reason": "数据不足(历史 K 线少于 60 日或为空)"},
                    "rule2": {"ok": False, "reason": "数据不足"},
                    "rule3": {"ok": False, "reason": "数据不足"},
                    "rule4": {"ok": False, "reason": "数据不足"},
                    "rule5": {"ok": False, "reason": "数据不足"},
                    "rule6": {"ok": False, "reason": "数据不足"},
                },
                "cached": c in cached_codes,
            })
            continue
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


@app.get("/api/watchlist/export")
def api_watchlist_export():
    """导出自选分组为 JSON 文件(可指定 groups 参数勾选部分分组,逗号分隔;不传则全部)。"""
    raw = (request.args.get("groups") or "").split(",")
    names = [n.strip() for n in raw if n.strip()]
    data = wl.export_groups(names or None)
    payload = json.dumps(
        _sanitize({"app": "etf-analysis", "version": APP_VERSION, "groups": data}),
        ensure_ascii=False, indent=2,
    )
    return Response(payload, mimetype="application/json",
                    headers={"Content-Disposition": "attachment; filename=watchlist_export.json"})


@app.post("/api/watchlist/import")
def api_watchlist_import():
    """导入自选分组。
    请求体: {groups:[{name, codes:[{code, alerts, thresholds}]}], modes:{组名:"merge"|"overwrite"}}
    - 组名存在 + merge    : 保留现有,追加导入中新增的 code
    - 组名存在 + overwrite: 整组替换
    - 组名不存在          : 直接追加
    """
    body = request.get_json(force=True, silent=True) or {}
    groups = body.get("groups") or []
    modes = body.get("modes") or {}
    if not isinstance(groups, list) or not groups:
        return jsonify({"ok": False, "error": "groups 不能为空"}), 400
    for g in groups:
        if not isinstance(g, dict) or not g.get("name"):
            return jsonify({"ok": False, "error": "分组格式错误(缺少 name)"}), 400
    try:
        new_groups = wl.import_groups(groups, modes)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "groups": new_groups})


@app.post("/api/watchlist/refresh-all")
def api_watchlist_refresh_all():
    """手动强制刷新所有离线 K 线缓存(后台线程执行,立即返回)。

    默认同时刷新「全市场 ETF + 自选(ETF + 股票)」。可选 body:
      { "full_market": false }  -> 仅刷新自选。
    进度用 GET /api/offline-refresh/status 轮询。
    """
    try:
        body = request.get_json(force=True, silent=True) or {}
    except Exception:
        body = {}
    full_market = bool(body.get("full_market", True))
    with _offline_lock:
        if offline_refresh_progress["running"]:
            return jsonify({"ok": False, "error": "已有离线刷新在运行", "running": True,
                            "refreshed": 0, "failed": 0, "items": []})
    # 后台线程执行,避免请求长时间挂起(全市场约 1500+ 只,耗时较长)
    threading.Thread(
        target=_run_offline_refresh_loop,
        args=(full_market, True),
        daemon=True,
        name="offline-refresh",
    ).start()
    return jsonify({"ok": True, "started": True, "full_market": full_market,
                    "message": "已在后台启动离线刷新,可轮询 /api/offline-refresh/status 查看进度"})


@app.get("/api/offline-refresh/status")
def api_offline_refresh_status():
    """返回收盘后离线刷新的实时进度(供前端轮询)。"""
    with _offline_lock:
        p = dict(offline_refresh_progress)
    if p["started_at"]:
        if p["running"] and not p["finished_at"]:
            p["elapsed"] = f"{(time.time() - p['started_at']):.0f}s"
        elif p["finished_at"]:
            p["elapsed"] = f"{(p['finished_at'] - p['started_at']):.0f}s"
        else:
            p["elapsed"] = "—"
    else:
        p["elapsed"] = "—"
    return jsonify({"ok": True, **p})


# ---------- 自选 ETF 警戒推送(BIAS 三档逃顶 + 年度最大回撤) ----------
def _alert_thresholds(body: dict, cfg: dict) -> dict:
    """合并请求中的阈值与配置默认值。对配置类型做防御,防止旧/异常配置导致 'str' object has no attribute 'get'。"""
    bias_cfg = cfg.get("bias_thresholds") if isinstance(cfg.get("bias_thresholds"), dict) else {}
    dd_cfg = cfg.get("drawdown_thresholds") if isinstance(cfg.get("drawdown_thresholds"), dict) else {}
    thresholds = {
        "bias20_levels": bias_cfg.get("bias20_levels", [10.0, 15.0]),
        "bias60_levels": bias_cfg.get("bias60_levels", [20.0]),
        "ytd_levels": dd_cfg.get("ytd_levels", [10.0, 15.0, 20.0]),
        "ytd_level_tags": dd_cfg.get("ytd_level_tags", ["红利档", "中性档", "创业板档"]),
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


@app.post("/api/watchlist/alert/test/code")
def api_watchlist_alert_test_code():
    """对单只 ETF 做测试推送,使用其已保存的订阅开关和独立阈值。

    - 若该代码已触发已订阅条件,发送真实内容;
    - 若未触发,也发送一张测试卡片(标注为手动测试),供验证 WxPusher 链路。
    """
    body = request.get_json(force=True, silent=True) or {}
    group = (body.get("group") or wl.DEFAULT_GROUP).strip() or wl.DEFAULT_GROUP
    code = (body.get("code") or "").strip()
    if not code or not code.isdigit() or len(code) != 6:
        return jsonify({"ok": False, "error": "code 必须是 6 位数字"}), 400

    cfg = cfg_mod.load_user()
    token = (cfg.get("wxpusher", {}) or {}).get("spt_token", "")
    if not token:
        return jsonify({"ok": False, "error": "未配置 WxPusher SPT_TOKEN,请在参数配置中填写。"}), 400

    years = int(cfg["display"].get("kline_years", 3))
    thresholds = _alert_thresholds(body, cfg)

    subs_map = wl.get_group_alerts(group)
    code_th = wl.get_group_thresholds(group)
    alerts = subs_map.get(code) or {}
    if not any(alerts.values()):
        return jsonify(_sanitize({
            "ok": True,
            "sent": False,
            "message": "该代码未勾选任何警戒条件,无法测试推送。",
            "code": code,
        }))

    df = ds.fetch_kline(code, years=years)
    if df.empty:
        return jsonify({"ok": False, "error": f"{code} 无 K 线数据,无法测试推送"}), 400

    name = ds.resolve_name(code)
    result = alert.test_push_code(
        code, name, df, thresholds,
        subscribed=alerts,
        code_thresholds=code_th.get(code, {}),
        token=token,
    )
    return jsonify(_sanitize({
        "ok": True,
        "sent": True,
        "real_trigger": result["real_trigger"],
        "code": code,
        "name": name,
        "wxpusher": result["wxpusher"],
        "markdown": result["markdown"],
        "item": result["item"],
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

# 盘中实时价缓存:每个警戒时间点预热前会重置,仅用于交易时段内的警戒评估。
_intraday_prices: dict[str, float | None] = {}
_intraday_warm_thread: threading.Thread | None = None


def _pre_warm_realtime_prices(scheduled_time: tuple[int, int]) -> None:
    """在配置推送时间点前 3 分钟窗口内,随机生成最多 3 个查询时刻,查到即停。

    设计思路:
    - 固定 60 秒间隔容易被行情接口识别为规律请求,改为 3 分钟内随机时间和随机间隔。
    - 最多 3 次查询,任一时刻拿到全部订阅代码的有效实时价即提前结束。
    - 仅在工作日的交易时段前才需要预热,非交易时段直接用日 K 收盘价。
    """
    global _intraday_prices, _intraday_warm_thread

    # 仅在工作日的交易时段前才需要预热(非交易时段直接用日 K)
    now = dt.datetime.now()
    if not ip.is_market_open_day(now.date()) or not alert_schedule.is_trade_day(now.date(), _get_alert_holidays()):
        return

    subs = wl.all_subscriptions()
    codes = sorted({str(s["code"]).zfill(6) for s in subs})
    if not codes:
        return

    # 已拿到全部代码实时价时,不再重复查询(查到即停)
    if all(_intraday_prices.get(c) is not None for c in codes):
        return

    # 避免同一时间点并发启动多个预热线程
    if _intraday_warm_thread and _intraday_warm_thread.is_alive():
        return

    def _warm_loop(target_dt: dt.datetime, codes_to_warm: list[str]) -> None:
        global _intraday_prices
        window_start = target_dt - dt.timedelta(minutes=3)
        total_seconds = int((target_dt - window_start).total_seconds())

        # 在 3 分钟窗口内随机生成 3 个查询时刻,保证至少有几秒间隔避免过于集中
        raw_points = sorted(random.sample(range(0, total_seconds), min(3, total_seconds)))
        schedule = [window_start + dt.timedelta(seconds=int(s)) for s in raw_points]
        print(f"[intraday-warm] 目标 {target_dt.strftime('%H:%M:%S')}, "
              f"随机查询时刻: {[t.strftime('%H:%M:%S') for t in schedule]}")

        for i, scheduled_at in enumerate(schedule, start=1):
            now = dt.datetime.now()
            if now >= target_dt:
                print(f"[intraday-warm] 已到目标时间,停止预热")
                break

            # 已拿到全部代码实时价,提前结束
            if all(_intraday_prices.get(c) is not None for c in codes_to_warm):
                print(f"[intraday-warm] 已齐全,提前结束")
                break

            # 等待到随机查询时刻
            if now < scheduled_at:
                wait = (scheduled_at - now).total_seconds()
                print(f"[intraday-warm] 等待 {wait:.1f} 秒后第 {i} 次查询")
                time.sleep(wait)

            prices = ip.fetch_realtime_prices(codes_to_warm)
            if prices:
                for c, p in prices.items():
                    if p is not None:
                        _intraday_prices[c] = p
                # 所有代码都拿到有效价格则提前结束
                if all(_intraday_prices.get(c) is not None for c in codes_to_warm):
                    print(f"[intraday-warm] 第 {i} 次查询完成,全部 {len(codes_to_warm)} 只拿到实时价")
                    break
                else:
                    missing = [c for c in codes_to_warm if _intraday_prices.get(c) is None]
                    print(f"[intraday-warm] 第 {i} 次查询完成,缺失 {missing}")
            else:
                print(f"[intraday-warm] 第 {i} 次查询失败")

    target = dt.datetime.combine(dt.date.today(), dt.time(scheduled_time[0], scheduled_time[1]))
    _intraday_warm_thread = threading.Thread(
        target=_warm_loop, args=(target, codes), daemon=True, name="intraday-warm"
    )
    _intraday_warm_thread.start()


def _scheduled_alert_run() -> None:
    """定时调度回调:扫描所有订阅,触发则推送 WxPusher(自带当天去重)。"""
    try:
        cfg = cfg_mod.load_user()
        token = (cfg.get("wxpusher", {}) or {}).get("spt_token", "")
        if not token:
            return
        thresholds = _alert_thresholds({}, cfg)
        subs = wl.all_subscriptions()
        if not subs:
            return
        years = int(cfg["display"].get("kline_years", 3))

        # 交易时段内且已有预热实时价时,用实时价评估;否则回退到日 K 收盘价
        now = dt.datetime.now()
        use_rt = ip.is_trading_hours(now) and bool(_intraday_prices)
        rt_prices = _intraday_prices if use_rt else None
        if use_rt:
            print(f"[alert-scheduler] {now.strftime('%H:%M')} 使用盘中实时价评估警戒")

        result = alert.run_subscription_scan(
            thresholds, subs, years=years, force=False, token=token, realtime_prices=rt_prices,
            dedup_scope=cfg.get("alert_dedup_scope") or "persist",
        )
        print(f"[alert-scheduler] {now.strftime('%Y-%m-%d %H:%M')} 扫描 {len(subs)} 只订阅, "
              f"实时价={use_rt}, 本次推送 {len(result.get('items', []))} 只")
    except Exception as e:
        print(f"[alert-scheduler] 执行异常: {e}")


def _get_alert_schedule():
    """返回用户配置的推送时间列表;显式设置为空数组时返回空(不自动推送)。"""
    cfg = cfg_mod.load_user()
    # 如果用户从未保存,使用默认 3 个时间
    if "alert_schedule" not in cfg:
        return ["10:00", "13:30", "17:00"]
    sched = cfg.get("alert_schedule") or []
    # 过滤空字符串,最多 3 个有效时间
    return [s for s in sched if isinstance(s, str) and s.strip()][:3]


def _get_alert_holidays():
    return cfg_mod.load_user().get("alert_holidays") or []


def _get_offline_refresh_schedule() -> list[str]:
    """返回用户配置的全量离线 K 线拉取时间列表;未配置用默认 07:30 + 16:15。"""
    cfg = cfg_mod.load_user()
    if "offline_refresh_schedule" not in cfg:
        return ["07:30", "16:15"]
    sched = cfg.get("offline_refresh_schedule") or []
    return [s for s in sched if isinstance(s, str) and s.strip()][:2]


# ---------- 收盘后自动下载所有离线 K 线 ----------
OFFLINE_REFRESH_STATE = ROOT / "data" / ".last_offline_refresh"

# 离线刷新进度(单进程内存共享,前端可轮询)
offline_refresh_progress = {
    "running": False,
    "phase": "",        # 当前阶段文案
    "done": 0,          # 已完成数
    "total": 0,         # 总量
    "ok": 0,            # 成功数
    "fail": 0,          # 失败数
    "started_at": 0.0,
    "finished_at": 0.0,
}
_offline_lock = threading.Lock()


def _collect_offline_codes(full_market: bool = True) -> list[str]:
    """收集离线刷新要覆盖的代码:全市场 ETF(可选) + 自选(ETF + 股票)。

    - 全市场 ETF:来自 data/etf_list.csv(由 core.data_source.list_etfs 读取,覆盖全部 ETF 基金);
    - 自选:watchlist 里所有分组,含 ETF 与 A 股,确保自选股票也刷新。
    """
    codes: set[str] = set()
    if full_market:
        try:
            etfs = ds.list_etfs()
            codes.update(str(c).zfill(6) for c in etfs["code"].tolist())
        except Exception as e:
            print(f"[offline-refresh] 读取全市场 ETF 列表失败(将仅刷新自选): {e}")
    for g in wl.list_groups():
        codes.update(str(c.get("code", c)).zfill(6) for c in g["codes"])
    return sorted(codes)


def _run_offline_refresh_loop(full_market: bool, collect_items: bool) -> dict:
    """真正执行离线 K 线刷新(force_refresh),线程安全、带进度、单只超时保护。

    调用前会原子地检查并占用 running 标记,避免与调度/手动并发重复刷新。
    返回汇总 dict(含 ok / refreshed / failed / total / items)。
    """
    import time as _t
    import threading

    def _fetch_one(code: str, years: int, timeout: int = 30):
        """单只拉取,带墙钟超时,防止某只数据源卡死拖垮全量刷新。"""
        res: dict = {"df": None, "err": None}

        def _target():
            try:
                res["df"] = ds.fetch_kline(code, years=years, force_refresh=True)
            except Exception as e:
                res["err"] = e
        th = threading.Thread(target=_target, name=f"fetch-{code}")
        th.daemon = True
        th.start()
        th.join(timeout=timeout)
        if th.is_alive():
            return None, f"单只超时(>{timeout}s)"
        if res["err"]:
            return None, str(res["err"])
        return res["df"], None

    with _offline_lock:
        if offline_refresh_progress["running"]:
            return {"ok": False, "reason": "已有刷新在运行", "refreshed": 0, "failed": 0, "total": 0, "items": []}
        offline_refresh_progress.update(
            running=True, phase="刷新K线", done=0, total=0, ok=0, fail=0,
            started_at=_t.time(), finished_at=0.0,
        )
    today = dt.date.today()
    try:
        cfg = cfg_mod.load_user()
        years = int(cfg["display"].get("kline_years", 3))
        codes = _collect_offline_codes(full_market=full_market)
        total = len(codes)
        with _offline_lock:
            offline_refresh_progress["total"] = total
        if not codes:
            print(f"[offline-refresh] {_t.strftime('%Y-%m-%d %H:%M')} 无代码,跳过")
            OFFLINE_REFRESH_STATE.write_text(today.isoformat(), encoding="utf-8")
            with _offline_lock:
                offline_refresh_progress["running"] = False
                offline_refresh_progress["finished_at"] = _t.time()
            return {"ok": True, "refreshed": 0, "failed": 0, "total": 0, "items": []}
        print(f"[offline-refresh] {_t.strftime('%Y-%m-%d %H:%M')} 开始全量刷新,共 {total} 只,单只超时 30s")
        ok = fail = 0
        items: list[dict] = []
        for i, c in enumerate(codes, 1):
            try:
                df, err = _fetch_one(c, years, timeout=30)
                good = (df is not None) and (not df.empty)
                if not good and err:
                    print(f"[offline-refresh] {c} 失败: {err}")
            except Exception as e:
                good = False
                print(f"[offline-refresh] {c} 失败: {e}")
            if good:
                ok += 1
            else:
                fail += 1
                if collect_items:
                    items.append({"code": c, "ok": False, "rows": 0, "error": "空数据/下载失败"})
            with _offline_lock:
                offline_refresh_progress["done"] = i
                offline_refresh_progress["ok"] = ok
                offline_refresh_progress["fail"] = fail
            # 每 100 只打印进度,方便判断刷新是否还在跑
            if i % 100 == 0 or i == total:
                print(f"[offline-refresh] 进度 {i}/{total} | 成功 {ok} | 失败 {fail}")
            _t.sleep(0.1)  # 节流,避免触发数据源限流
        print(f"[offline-refresh] {_t.strftime('%Y-%m-%d %H:%M')} 刷新 {ok} 只(失败 {fail}) / 共 {total} 只")
        OFFLINE_REFRESH_STATE.write_text(today.isoformat(), encoding="utf-8")
        with _offline_lock:
            offline_refresh_progress["running"] = False
            offline_refresh_progress["finished_at"] = _t.time()
        return {"ok": True, "refreshed": ok, "failed": fail, "total": total, "items": items}
    except Exception as e:
        import traceback
        print(f"[offline-refresh] 执行异常: {e}")
        traceback.print_exc()
        with _offline_lock:
            offline_refresh_progress["running"] = False
            offline_refresh_progress["finished_at"] = _t.time()
        return {"ok": False, "error": str(e), "refreshed": 0, "failed": 0, "total": 0, "items": []}


def _latest_closed_trade_day() -> dt.date | None:
    """返回「最新已收盘交易日」:当前时刻之后数据已经可用的最后一个交易日。

    - 若已收盘(>=15:30 且今天为交易日):今天即为最新已收盘交易日;
    - 否则:回溯到今天之前最近的交易日。
    """
    now = dt.datetime.now()
    today = now.date()
    holidays = _get_alert_holidays()
    if (now.hour > 15 or (now.hour == 15 and now.minute >= 30)) and alert_schedule.is_trade_day(today, holidays):
        return today
    d = today - dt.timedelta(days=1)
    for _ in range(8):
        if alert_schedule.is_trade_day(d, holidays):
            return d
        d -= dt.timedelta(days=1)
    return None


def _offline_already_fresh_for_latest_closed_day() -> bool:
    """最新已收盘交易日的离线数据是否已被刷新过(用于 07:30 补拉「跳过」判断)。"""
    latest = _latest_closed_trade_day()
    if latest is None or not OFFLINE_REFRESH_STATE.exists():
        return False
    try:
        last_str = OFFLINE_REFRESH_STATE.read_text(encoding="utf-8").strip()
        last_date = dt.date.fromisoformat(last_str)
    except Exception:
        return False
    return last_date >= latest


def _offline_refresh_run() -> None:
    """交易日全量离线 K 线调度回调,刷新全市场 ETF + 自选 K 线缓存。

    配置项 offline_refresh_schedule 默认 ["07:30", "16:15"]:
    - 主拉取:配置时间中最晚的一个(默认 16:15,收盘后),总是全量 force_refresh,
      保证当日收盘数据完整;先于 17:00 警戒推送,使推送用「当日收盘价」。
    - 补拉:其余时间(默认 07:30,盘前),仅当「最新已收盘交易日」的离线数据尚未刷新
      时才全量拉取;若已最新则直接跳过,避免重复下载同一份收盘数据以省资源。

    刷新成功后记录当天日期到 .last_offline_refresh。
    """
    now = dt.datetime.now()
    sched = sorted(_get_offline_refresh_schedule())
    now_str = now.strftime("%H:%M")
    main_time = sched[-1] if sched else "16:15"
    if now_str == main_time:
        print(f"[offline-refresh] {now:%Y-%m-%d %H:%M} 主拉取触发({main_time}),开始全量刷新")
        _run_offline_refresh_loop(full_market=True, collect_items=False)
        return
    # 补拉:已最新则跳过
    if _offline_already_fresh_for_latest_closed_day():
        print(f"[offline-refresh] {now:%Y-%m-%d %H:%M} 补拉触发({now_str}):最新已收盘交易日数据已刷新,跳过以省资源")
        return
    print(f"[offline-refresh] {now:%Y-%m-%d %H:%M} 补拉触发({now_str}),开始全量刷新")
    _run_offline_refresh_loop(full_market=True, collect_items=False)


def _maybe_catch_up_offline_refresh() -> None:
    """启动后补刷:确保「最新已收盘交易日」的离线数据已下载,防止定时点进程未运行而错过。

    覆盖两类场景:
    - 进程在 16:15 之后才启动 -> 当天主拉取错过,后台全量拉一次;
    - 盘前启动但前一交易日 16:15 未成功(进程未运行/失败) -> 后台补拉。
    若最新已收盘交易日已被刷新,则跳过(无需重复)。

    注意:启动补刷改为后台 daemon 线程执行,避免 1500+ 只全量刷新阻塞主线程,
    导致 Docker 容器启动时 waitress 无法监听、Web 界面长时间不通。
    """
    latest = _latest_closed_trade_day()
    if latest is None:
        return
    if _offline_already_fresh_for_latest_closed_day():
        return
    print(f"[offline-refresh] 启动补刷:最新已收盘交易日 {latest} 尚未刷新,后台执行")
    t = threading.Thread(
        target=_run_offline_refresh_loop,
        args=(True, False),
        daemon=True,
        name="offline-catch-up",
    )
    t.start()


if __name__ == "__main__":
    # 启动定时自动推送调度器(守护线程)
    # 提前 3 分钟预热盘中实时价,每 60 秒查一次,查到即停
    _scheduler = alert_schedule.AlertScheduler(
        _scheduled_alert_run,
        interval=30,
        pre_warm_callback=_pre_warm_realtime_prices,
        pre_warm_minutes=3,
    )
    _scheduler.start(_get_alert_schedule, _get_alert_holidays)

    # 启动「交易日自动下载所有离线 K 线」调度器(守护线程)
    # 用户可在参数配置里改 offline_refresh_schedule,默认 07:30(补拉) + 16:15(主拉取)
    _refresh_scheduler = alert_schedule.AlertScheduler(_offline_refresh_run, interval=30)
    _refresh_scheduler.start(_get_offline_refresh_schedule, _get_alert_holidays)

    # 启动 A 股全市场股票名称后台预热(若缓存缺失)。
    # 用后台线程拉,web 服务不被阻塞;单飞锁避免搜索/添加并发触发重复拉取。
    ds.start_stock_list_warmup()

    # 启动后补刷:若最新已收盘交易日尚未刷新(16:15 之后才启动 / 盘前补拉),立即执行一次
    _maybe_catch_up_offline_refresh()

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
