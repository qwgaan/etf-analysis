"""
自选 ETF 警戒推送模块
- 根据 BIAS 三档逃顶 / 年度最大回撤阈值,计算每只 ETF 是否触发警戒。
- 生成 Markdown 推送内容,调用 WxPusher 发送。

WxPusher 使用单用户推送 token(即 SPT_TOKEN):
    POST https://wxpusher.zjiecode.com/api/send/message/{spt_token}
contentType=3 表示 Markdown。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pandas as pd

from . import data_source as ds
from . import indicators as ind

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# WxPusher 官方 POST 端点
# - appToken(AT_xxx): 标准推送 POST /api/send/message
# - SPT(SPT_xxx): 极简推送 POST /api/send/message/simple-push
WXPUSHER_STANDARD_URL = "https://wxpusher.zjiecode.com/api/send/message"
WXPUSHER_SIMPLE_URL = "https://wxpusher.zjiecode.com/api/send/message/simple-push"


def _pct(v: float | None, digits: int = 2) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    sign = "+" if v > 0 else ""
    return f"{sign}{v:.{digits}f}%"


def _merge_thresholds(
    global_thresholds: dict[str, Any],
    code_thresholds: dict[str, list[float]] | None,
) -> dict[str, Any]:
    """合并全局阈值与单只 ETF 独立阈值,独立优先级高。"""
    out = dict(global_thresholds)
    for k in ("bias20_levels", "bias60_levels", "ytd_levels"):
        if code_thresholds and k in code_thresholds and code_thresholds[k]:
            out[k] = list(code_thresholds[k])
    return out


def evaluate_alert(
    code: str,
    name: str,
    df: pd.DataFrame,
    thresholds: dict[str, Any],
    subscribed: dict[str, bool] | None = None,
    code_thresholds: dict[str, list[float]] | None = None,
) -> dict[str, Any]:
    """
    对单只 ETF 计算 BIAS / 年度最大回撤触发情况。

    thresholds 示例:
        {
            "bias20_levels": [10.0, 15.0],
            "bias60_levels": [20.0],
            "ytd_levels": [10.0, 15.0, 20.0],
            "ytd_level_tags": ["红利档", "中性档", "创业板档"],
        }

    subscribed 示例(逐只订阅开关):
        {"bias20": true, "bias60": false, "dd": true}
    - 为 None 时(旧版整组模式)返回所有触发的信号;
    - 传入时只把「已订阅且触发」的标准纳入 triggered,并额外给出 hot 标记。

    code_thresholds: 该 ETF 独立覆盖的阈值(可选)。
    """
    effective = _merge_thresholds(thresholds, code_thresholds)

    close = df["close"]
    bias20 = ind.safe_last(ind.bias(close, 20))
    bias60 = ind.safe_last(ind.bias(close, 60))
    last_close = ind.safe_last(close)
    # 当前价格年内回撤:用于「回撤档」警戒触发
    ytd_dd = ind.drawdown_ytd_current(close)
    # 真正的年内最大回撤(高点→低点):用于关键摘要展示
    ytd_max_dd, ytd_max_dd_date, ytd_max_dd_price = ind.ytd_max_drawdown_with_low(close)

    # 52 周/年内高、低(用于推送正文展示)
    hi52 = ind.safe_last(ind.period_high(close, window=260))
    lo52 = ind.safe_last(ind.period_low(close, window=260))
    ytd_high_series = close.groupby(close.index.year).cummax()
    ytd_low_series = close.groupby(close.index.year).cummin()
    ytd_high = ytd_high_series.iloc[-1] if len(ytd_high_series) else None
    ytd_low = ytd_low_series.iloc[-1] if len(ytd_low_series) else None

    def dist_pct(now, base):
        if now is None or base is None or base == 0:
            return None
        return (now - base) / base * 100.0

    # 各标准独立计算「当前是否触发」
    bias20_signals: list[str] = []
    for level in sorted(effective.get("bias20_levels", []), reverse=True):
        if bias20 is not None and not pd.isna(bias20) and bias20 >= level:
            bias20_signals.append(f"BIAS20≥{level}%")

    bias60_signals: list[str] = []
    for level in sorted(effective.get("bias60_levels", []), reverse=True):
        if bias60 is not None and not pd.isna(bias60) and bias60 >= level:
            bias60_signals.append(f"BIAS60≥{level}%")

    dd_signals: list[str] = []
    ytd_levels = sorted(effective.get("ytd_levels", []), reverse=True)
    tags = effective.get("ytd_level_tags", []) or thresholds.get("ytd_level_tags", []) or []
    for i, level in enumerate(ytd_levels):
        if ytd_dd is not None and not pd.isna(ytd_dd) and ytd_dd <= -level:
            tag = tags[i] if i < len(tags) else f"{level}%"
            dd_signals.append(f"{tag}回撤≥{level}%")

    hot = {
        "bias20": bool(bias20_signals),
        "bias60": bool(bias60_signals),
        "dd": bool(dd_signals),
    }

    # 按订阅过滤真正要推送的信号
    triggered: list[str] = []
    if subscribed:
        if subscribed.get("bias20") and bias20_signals:
            triggered.extend(bias20_signals)
        if subscribed.get("bias60") and bias60_signals:
            triggered.extend(bias60_signals)
        if subscribed.get("dd") and dd_signals:
            triggered.extend(dd_signals)
    else:
        # 未指定订阅(预览/整组场景):展示所有触发的标准
        triggered = bias20_signals + bias60_signals + dd_signals

    return {
        "code": code,
        "name": name,
        "close": last_close,
        "bias20": bias20,
        "bias60": bias60,
        "ytd_drawdown": ytd_dd,
        "ytd_max_drawdown": ytd_max_dd,
        "ytd_max_drawdown_date": ytd_max_dd_date,
        "ytd_max_drawdown_price": ytd_max_dd_price,
        "high_52w": hi52,
        "low_52w": lo52,
        "ytd_high": ytd_high,
        "ytd_low": ytd_low,
        "dist_52w_high_pct": dist_pct(last_close, hi52),
        "dist_52w_low_pct": dist_pct(last_close, lo52),
        "dist_ytd_high_pct": dist_pct(last_close, ytd_high),
        "dist_ytd_low_pct": dist_pct(last_close, ytd_low),
        "effective_thresholds": {k: v for k, v in effective.items() if k != "ytd_level_tags"},
        "hot": hot,
        "subscribed": dict(subscribed) if subscribed else None,
        "triggered": triggered,
        "triggered_any": bool(triggered),
    }


def _bar(val: float | None, min_v: float = -10.0, max_v: float = 20.0, width: int = 20) -> str:
    """文本进度条,用于 Markdown 推送。"""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return "□" * width
    pct = (val - min_v) / (max_v - min_v)
    pct = max(0.0, min(1.0, pct))
    filled = int(round(pct * width))
    return "█" * filled + "░" * (width - filled)


def _bar_abs(val: float | None, max_v: float | None = None, width: int = 20) -> str:
    """基于绝对值/最大值的文本进度条(用于回撤)。"""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return "□" * width
    abs_v = abs(val)
    base = max(max_v or 20.0, abs_v)
    pct = min(1.0, abs_v / base)
    filled = int(round(pct * width))
    return "█" * filled + "░" * (width - filled)


def _price(v: float | None) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    return f"{v:.3f}"


def _build_etf_card(it: dict[str, Any], global_tags: list[str]) -> list[str]:
    """为单只 ETF 生成「当前信号」风格的 Markdown 卡片。"""
    code, name = it["code"], it["name"]
    close = it["close"]
    b20 = it["bias20"]
    b60 = it["bias60"]
    ydd = it["ytd_drawdown"]           # 当前价格年内回撤
    eff = it.get("effective_thresholds", {})

    b20_levels = sorted(eff.get("bias20_levels", []))
    b60_levels = sorted(eff.get("bias60_levels", []))
    ytd_levels = sorted(eff.get("ytd_levels", []))

    lines: list[str] = []
    lines.append(f"### {name} · {code}")
    lines.append("")

    # 关键摘要
    lines.append("**📌 关键摘要**")
    lines.append(f"- 现价：**{close:.3f}**")
    lines.append(f"- 52 周高 / 低：{_price(it.get('high_52w'))} / {_price(it.get('low_52w'))}")
    lines.append(f"- 今年高 / 低：{_price(it.get('ytd_high'))} / {_price(it.get('ytd_low'))}")
    lines.append(f"- 距 52 周高 / 低：{_pct(it.get('dist_52w_high_pct'))} / {_pct(it.get('dist_52w_low_pct'))}")
    lines.append(f"- 距今年高 / 低：{_pct(it.get('dist_ytd_high_pct'))} / {_pct(it.get('dist_ytd_low_pct'))}")
    dd_max = it.get("ytd_max_drawdown")
    dd_max_date = it.get("ytd_max_drawdown_date", "")
    dd_max_price = it.get("ytd_max_drawdown_price")
    dd_max_extra = f"(低点 {_price(dd_max_price)} @ {dd_max_date or '—'})" if dd_max_price else ""
    lines.append(f"- 年内最大回撤：**{_pct(dd_max)}** {dd_max_extra}")
    lines.append("")

    # BIAS 三档逃顶
    lines.append("**🚨 BIAS 三档逃顶**")
    for key, val, levels in [("BIAS20", b20, b20_levels), ("BIAS60", b60, b60_levels)]:
        color = "🔴" if any(val is not None and not pd.isna(val) and val >= lv for lv in levels) else "🟢"
        bar = _bar(val)
        marks = "  ".join(f"| {lv}%" for lv in levels)
        trig = " · ".join([s for s in it.get("triggered", []) if key in s]) or "未触发警戒"
        lines.append(f"{color} **{key} {_pct(val)}**")
        lines.append(f"`{bar}`")
        lines.append(f"{marks}")
        lines.append(f"*{trig}*")
        lines.append("")

    # 当前价格年内回撤(警戒档内容)
    lines.append("**📉 当前价格年内回撤**")
    tags = global_tags or [f"{lv}%" for lv in ytd_levels]
    marks = "  ".join(f"| {tags[i] if i < len(tags) else str(lv) + '%'} {lv}%" for i, lv in enumerate(ytd_levels))
    trig = " · ".join([s for s in it.get("triggered", []) if "回撤" in s]) or "未抵任何警戒档"
    lines.append(f"**{_pct(ydd)}**")
    lines.append(f"`{_bar_abs(ydd, max_v=max(ytd_levels) if ytd_levels else 20)}`")
    lines.append(f"{marks}")
    lines.append(f"*{trig}*")
    lines.append("")

    return lines


def build_markdown(
    items: list[dict[str, Any]],
    thresholds: dict[str, Any],
    pushed_at: str | None = None,
    title: str | None = None,
    subtitle: str | None = None,
) -> str:
    """把触发列表组装成「当前信号」风格的 Markdown 推送正文。

    title: 自定义标题(默认 ## 🚨 自选 ETF 警戒推送)。
    subtitle: 标题下方的补充说明(可选)。
    """
    if pushed_at is None:
        pushed_at = time.strftime("%Y-%m-%d %H:%M:%S")

    b20 = thresholds.get("bias20_levels", [])
    b60 = thresholds.get("bias60_levels", [])
    ytd = thresholds.get("ytd_levels", [])
    tags = thresholds.get("ytd_level_tags", []) or []

    lines: list[str] = [
        title or "## 🚨 自选 ETF 警戒推送",
        "",
        f"**推送时间**：{pushed_at}",
        "",
    ]
    if subtitle:
        lines.append(subtitle)
        lines.append("")
    lines.extend([
        f"**全局警戒档**：BIAS20 `{b20}` · BIAS60 `{b60}` · 当前价格年内回撤 `{ytd}`",
        "",
        f"共 **{len(items)}** 只 ETF 触发警戒：",
        "",
    ])

    if not items:
        lines.append("当前自选池中没有任何 ETF 触发警戒档。")
        lines.append("")
        lines.append("_数据来自本地缓存/AKShare,仅供参考。_")
        return "\n".join(lines)

    for it in items:
        lines.extend(_build_etf_card(it, tags))

    lines.append("_数据来自本地缓存/AKShare,仅供参考。_")
    return "\n".join(lines)


def test_push_code(
    code: str,
    name: str,
    df: pd.DataFrame,
    thresholds: dict[str, Any],
    subscribed: dict[str, bool] | None,
    code_thresholds: dict[str, list[float]] | None,
    token: str,
) -> dict[str, Any]:
    """对单只 ETF 执行测试推送。

    - 若该代码已触发已订阅的警戒,发送真实触发内容;
    - 若未触发,仍发送一张「测试卡片」,并在触发信号处标注为手动测试,
      方便用户验证 WxPusher 推送链路而不必等到行情触发。
    """
    res = evaluate_alert(code, name, df, thresholds, subscribed=subscribed, code_thresholds=code_thresholds)
    is_real = bool(res.get("triggered"))
    if not is_real:
        res["triggered"] = ["🧪 手动测试推送(当前未触发警戒)"]
    markdown = build_markdown(
        [res],
        thresholds,
        title=f"## 🧪 自选 ETF 单代码测试推送 · {code}",
        subtitle=f"**{'真实触发' if is_real else '模拟测试'}**：{name}({code}) · 仅用于验证推送链路",
    )
    summary = f"🧪 自选 ETF 测试推送 · {code}"
    wx_resp = push_wxpusher(token, markdown, summary=summary)
    return {
        "ok": True,
        "sent": True,
        "real_trigger": is_real,
        "item": res,
        "markdown": markdown,
        "wxpusher": wx_resp,
    }


def push_wxpusher(
    token: str,
    markdown: str,
    summary: str = "自选 ETF 警戒推送",
) -> dict[str, Any]:
    """
    调用 WxPusher 发送 Markdown 消息。
    根据 token 前缀自动选择官方端点:
      - AT_xxx  -> 标准推送 /api/send/message
      - SPT_xxx -> 极简推送 /api/send/message/simple-push
    返回 WxPusher 的原始响应 dict,或 {"error": str}。
    """
    if not token:
        return {"ok": False, "error": "缺少 SPT_TOKEN"}

    token_upper = token.upper()
    if token_upper.startswith("AT_"):
        url = WXPUSHER_STANDARD_URL
        payload = {
            "appToken": token,
            "content": markdown,
            "summary": summary,
            "contentType": 3,  # Markdown
        }
    elif token_upper.startswith("SPT_"):
        url = WXPUSHER_SIMPLE_URL
        payload = {
            "spt": token,
            "content": markdown,
            "summary": summary,
            "contentType": 3,
        }
    else:
        # 未识别前缀时,优先按极简推送处理(用户称为 SPT_TOKEN)
        url = WXPUSHER_SIMPLE_URL
        payload = {
            "spt": token,
            "content": markdown,
            "summary": summary,
            "contentType": 3,
        }

    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
            try:
                return json.loads(raw)
            except Exception:
                return {"ok": True, "raw": raw}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        return {"ok": False, "error": f"HTTP {e.code}", "detail": body}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def scan_group(
    group_codes: list[str],
    thresholds: dict[str, Any],
    years: int = 3,
    subscriptions: dict[str, dict[str, bool]] | None = None,
    code_thresholds: dict[str, dict[str, list[float]]] | None = None,
) -> list[dict[str, Any]]:
    """扫描一组自选 ETF,返回每只的触发详情(含未触发)。

    subscriptions: {code: {bias20, bias60, dd}} 订阅开关;None 表示不按订阅过滤。
    code_thresholds: {code: {bias20_levels, bias60_levels, ytd_levels}} 单只阈值覆盖。
    """
    codes = [str(c).zfill(6) for c in group_codes]
    name_map = ds.resolve_names(codes)

    results: list[dict[str, Any]] = []
    for code in codes:
        df = ds.fetch_kline(code, years=years)
        sub = (subscriptions or {}).get(code)
        th = (code_thresholds or {}).get(code)
        name = name_map.get(code, code)
        if df.empty:
            results.append({
                "code": code,
                "name": name,
                "close": None,
                "bias20": None,
                "bias60": None,
                "ytd_drawdown": None,
                "ytd_max_drawdown": None,
                "ytd_max_drawdown_date": "",
                "ytd_max_drawdown_price": None,
                "hot": {"bias20": False, "bias60": False, "dd": False},
                "subscribed": dict(sub) if sub else None,
                "triggered": [],
                "triggered_any": False,
                "error": "数据为空",
            })
            continue
        results.append(evaluate_alert(code, name, df, thresholds, subscribed=sub, code_thresholds=th))
    return results


# ---------- 推送去重状态(data/alert_state.json) ----------
import datetime as _dt  # noqa: E402  (置于文件尾部工具区前)

STATE_PATH = PROJECT_ROOT / "data" / "alert_state.json"


def load_push_state() -> dict:
    if STATE_PATH.exists():
        try:
            with STATE_PATH.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_push_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with STATE_PATH.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _today_str() -> str:
    return _dt.date.today().strftime("%Y-%m-%d")


def already_pushed_today(code: str, signal_key: str) -> bool:
    """signal_key ∈ {bias20, bias60, dd}。今天已推送过则返回 True。"""
    return load_push_state().get(code, {}).get(signal_key) == _today_str()


def mark_pushed_today(code: str, signal_keys: set[str]) -> None:
    """记录某 code 当天已推送(逐信号键 + 整码标记,用于「当天每码一次」去重)。"""
    state = load_push_state()
    code_state = state.setdefault(code, {})
    for k in signal_keys:
        code_state[k] = _today_str()
    code_state["__code"] = _today_str()
    save_push_state(state)


def code_pushed_today(code: str) -> bool:
    """该 code 今天是否已触发过(任意信号)。用于「当天同一代码只推送一次」。"""
    return load_push_state().get(code, {}).get("__code") == _today_str()


def signal_key_of(triggered_str: str) -> str:
    """把触发信号文案映射到订阅键:BIAS20≥.. -> bias20 等。"""
    s = triggered_str or ""
    if s.startswith("BIAS20"):
        return "bias20"
    if s.startswith("BIAS60"):
        return "bias60"
    return "dd"


def run_subscription_scan(
    thresholds: dict[str, Any],
    subscriptions: list[dict[str, Any]],
    years: int = 3,
    force: bool = False,
    token: str | None = None,
) -> dict[str, Any]:
    """
    对一批订阅(每只含 {code, alerts, thresholds?, name?})逐只评估并推送。

    - force=True:测试推送,忽略「今天已推送」去重,直接推送当前触发的。
    - token 为 None:仅评估返回 to_push 列表,不真正推送(供预览/日志)。
    返回 {items, pushed, markdown, wxpusher}。
    """
    codes = [str(sub["code"]).zfill(6) for sub in subscriptions]
    name_map = ds.resolve_names(codes)

    to_push: list[dict[str, Any]] = []
    for sub in subscriptions:
        code = str(sub["code"]).zfill(6)
        alerts = sub.get("alerts") or {}
        if not any(alerts.values()):
            continue
        df = ds.fetch_kline(code, years=years)
        name = sub.get("name") or name_map.get(code, code)
        if df.empty:
            continue
        code_th = sub.get("thresholds")
        res = evaluate_alert(code, name, df, thresholds, subscribed=alerts, code_thresholds=code_th)
        if not res.get("triggered_any"):
            continue
        if force:
            # 测试推送:忽略「当天已推送」去重,直接发送(用于验证推送链路)
            to_push.append(res)
            continue
        # 去重(按 code):当天该代码只要触发过一次,后续时间点不再重复推送
        if code_pushed_today(code):
            continue
        to_push.append(res)

    # 仅非强制(定时/真实)扫描才记录已推送,避免手动测试污染当天去重
    if not force:
        for res in to_push:
            mark_pushed_today(res["code"], {signal_key_of(s) for s in res["triggered"]})

    markdown = build_markdown(to_push, thresholds)
    wx_resp = None
    if token and to_push:
        wx_resp = push_wxpusher(token, markdown)

    return {
        "items": to_push,
        "pushed": bool(to_push) and token is not None,
        "markdown": markdown,
        "wxpusher": wx_resp,
    }
