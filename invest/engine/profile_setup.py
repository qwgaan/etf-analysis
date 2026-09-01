#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""首次建档：投资者画像配置工具（自适应评分的输入源）。

画像决定三件事：
  1) 四个维度（技术面/基本面/估值/资金面）的权重分配
  2) 综合分 → 参与建议的映射
  3) 单一标的仓位上限

用法：
  python profile_setup.py                        # 交互式建档（推荐首次使用）
  python profile_setup.py --show                 # 查看当前画像与推导出的权重
  python profile_setup.py --preview              # 预览三种风险偏好的权重差异
  python profile_setup.py --set risk=保守 horizon=长线 max_position=0.08 dividend_focus=true
  python profile_setup.py --out profile_me.json  # 另存为独立画像文件

生成的画像文件用于：
  python run_report.py --code 600036 --name 招商银行 --profile tools/profile_me.json
"""
import os, sys, json, argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from invest import paths

HERE = os.path.dirname(os.path.abspath(__file__))
# 画像改落 invest/config/invest_profile.json（与 ETF 的 config/user.json 完全隔离）
DEFAULT = str(paths.CONFIG_DIR / "invest_profile.json")

RISK_OPTIONS = {
    "保守": "重安全边际与股息，容忍低回报换低波动；估值与基本面权重最高，几乎不追动量。",
    "平衡": "四维度等权起步，兼顾成长与安全边际（默认）。",
    "激进": "重动量与成长弹性，接受较大回撤换取上行；技术面权重最高。",
    "自定义": "手动指定技术面/基本面/估值/资金面的权重比值，系统自动归一化。",
}
HORIZON_OPTIONS = {
    "短线": "持有数日至数周，技术面与资金面主导，估值容忍度高。",
    "中线": "持有数月至一年，四维度均衡（默认）。",
    "长线": "持有一年以上，基本面与估值主导，主动降低技术面权重。",
}

TEMPLATE = {
    "name": "默认投资者画像（首次建档）",
    "risk": "平衡",
    "horizon": "中线",
    "max_position": 0.15,
    "dividend_focus": False,
    "notes": "风险偏好:保守/平衡/激进; 投资周期:短线/中线/长线; 单一标的仓位上限(0~1); 是否重股息。",
}


def load(path=DEFAULT):
    if os.path.exists(path):
        try:
            d = dict(TEMPLATE)
            d.update(json.load(open(path, encoding="utf-8")))
            return d
        except Exception as e:
            print(f"[warn] 画像文件解析失败({e})，使用模板默认值", file=sys.stderr)
    return dict(TEMPLATE)


def save(prof, path=DEFAULT):
    json.dump(prof, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"[ok] 画像已保存：{path}")
    return path


def explain(prof):
    """展示画像 → 权重 → 仓位映射的完整推导（复用 pipeline 的真实逻辑）。"""
    sys.path.insert(0, HERE)
    from invest.engine.pipeline import adaptive, position_note
    base = {"技术面": 4.0, "基本面": 4.0, "估值": 4.0, "资金面": 4.0}
    _, w = adaptive(base, prof)
    print("=" * 62)
    print(f"画像：{prof.get('name')}")
    print(f"  风险偏好 risk         = {prof.get('risk')}    · {RISK_OPTIONS.get(prof.get('risk'), '')}")
    print(f"  投资周期 horizon      = {prof.get('horizon')}    · {HORIZON_OPTIONS.get(prof.get('horizon'), '')}")
    print(f"  仓位上限 max_position = {prof.get('max_position')}（即单一标的最多 {float(prof.get('max_position', 0.15))*100:.0f}% 仓位）")
    print(f"  重视股息 dividend_focus = {prof.get('dividend_focus')}")
    print("-" * 62)
    print("推导出的四维权重（自动归一化到 1.0）：")
    for k, v in w.items():
        bar = "█" * int(round(v * 40))
        print(f"  {k:<6} {v:>5.1%}  {bar}")
    print("-" * 62)
    print("综合分 → 参与建议 / 仓位上限 映射：")
    for s in (4.5, 4.0, 3.6, 3.3, 3.1, 2.8, 2.0):
        st, size = position_note(s, prof)
        print(f"  综合分 {s:>4}  →  {st:<12}  建议仓位上限 {size*100:>5.1f}%")
    print("=" * 62)


def preview_all():
    sys.path.insert(0, HERE)
    from invest.engine.pipeline import adaptive
    base = {"技术面": 4.0, "基本面": 4.0, "估值": 4.0, "资金面": 4.0}
    print(f"{'画像组合':<14}{'技术面':>9}{'基本面':>9}{'估值':>9}{'资金面':>9}")
    print("-" * 52)
    for risk in ("保守", "平衡", "激进"):
        for hz in ("短线", "中线", "长线"):
            _, w = adaptive(base, {"risk": risk, "horizon": hz})
            print(f"{risk}/{hz:<10}" + "".join(f"{w[k]:>8.1%}" for k in ("技术面", "基本面", "估值", "资金面")))
    print("-" * 52)
    print("说明：权重由 risk 定基准，再由 horizon 微调后归一化。保守/长线最看估值与基本面；激进/短线最看技术面。")


def ask(prompt, options, default):
    keys = list(options.keys())
    print(f"\n{prompt}")
    for i, k in enumerate(keys, 1):
        mark = "（默认）" if k == default else ""
        print(f"  {i}. {k}{mark} —— {options[k]}")
    raw = input(f"请输入序号或名称 [{default}]：").strip()
    if not raw:
        return default
    if raw.isdigit() and 1 <= int(raw) <= len(keys):
        return keys[int(raw) - 1]
    return raw if raw in options else default


def interactive(path):
    prof = load(path)
    print("=" * 62)
    print("投资者画像首次建档（直接回车＝采用默认值）")
    print("=" * 62)
    nm = input(f"画像名称 [{prof.get('name')}]：").strip()
    if nm:
        prof["name"] = nm
    prof["risk"] = ask("① 风险偏好：", RISK_OPTIONS, prof.get("risk", "平衡"))
    prof["horizon"] = ask("② 投资周期：", HORIZON_OPTIONS, prof.get("horizon", "中线"))
    print("\n③ 单一标的仓位上限：控制单票最大占总仓位比例，是风控硬约束。")
    print("   参考：保守 5%~10%｜平衡 10%~15%｜激进 15%~25%")
    raw = input(f"请输入 0~1 之间的小数或百分数 [{prof.get('max_position')}]：").strip().replace("%", "")
    if raw:
        try:
            v = float(raw)
            prof["max_position"] = round(v / 100 if v > 1 else v, 4)
        except ValueError:
            print("   输入无法解析，保留原值。")
    raw = input(f"\n④ 是否偏好高股息标的？(y/n) [{'y' if prof.get('dividend_focus') else 'n'}]：").strip().lower()
    if raw in ("y", "yes", "是"):
        prof["dividend_focus"] = True
    elif raw in ("n", "no", "否"):
        prof["dividend_focus"] = False
    save(prof, path)
    print()
    explain(prof)
    print(f"\n用法：python run_report.py --code 600036 --name 招商银行 --profile {os.path.relpath(path, os.path.dirname(HERE))}")


def apply_sets(prof, pairs):
    for p in pairs:
        if "=" not in p:
            print(f"[warn] 忽略无法解析的参数：{p}")
            continue
        k, v = p.split("=", 1)
        k, v = k.strip(), v.strip()
        if k == "max_position":
            try:
                fv = float(v.replace("%", ""))
                prof[k] = round(fv / 100 if fv > 1 else fv, 4)
            except ValueError:
                print(f"[warn] max_position 无法解析：{v}")
        elif k == "dividend_focus":
            prof[k] = v.lower() in ("true", "1", "y", "yes", "是")
        elif k in ("risk", "horizon", "name", "notes"):
            prof[k] = v
        else:
            print(f"[warn] 未知字段：{k}")
    return prof


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=DEFAULT, help="画像文件路径（默认覆盖 profile_default.json）")
    ap.add_argument("--show", action="store_true", help="查看当前画像与推导权重")
    ap.add_argument("--preview", action="store_true", help="预览九种画像组合的权重差异")
    ap.add_argument("--set", nargs="*", default=None, metavar="K=V", help="非交互设置，如 risk=保守 horizon=长线")
    a = ap.parse_args()

    if a.preview:
        preview_all()
        return
    if a.show:
        explain(load(a.out))
        return
    if a.set is not None:
        prof = apply_sets(load(a.out), a.set)
        save(prof, a.out)
        print()
        explain(prof)
        return
    try:
        interactive(a.out)
    except (EOFError, KeyboardInterrupt):
        print("\n[已取消] 非交互环境请改用：python profile_setup.py --set risk=保守 horizon=长线 max_position=0.08")


if __name__ == "__main__":
    main()
