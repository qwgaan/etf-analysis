"""
全市场 K 线预热脚本(命令行)
用途:把全市场 ETF 的历史 K 线缓存到 data/klines/,
之后 /api/screen?use_cache=1 就能秒级完成全市场 Mark 模板筛选。

用法:
    python warmup.py                # 默认全市场,回看 3 年
    python warmup.py --limit 100    # 只预热前 100 只
    python warmup.py --years 5      # 回看 5 年
    python warmup.py --resume       # 跳过已有缓存,续传(默认行为)

可随时 Ctrl+C 中断,已缓存的会保留。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import prewarm  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只预热前 N 只(0=全市场)")
    ap.add_argument("--years", type=int, default=3, help="回看年数")
    ap.add_argument("--sleep", type=float, default=0.05, help="每次请求间隔(秒),防限速")
    args = ap.parse_args()

    result = prewarm.start_preheat(limit=args.limit, years=args.years, sleep=args.sleep)
    if not result.get("ok") and result.get("reason") == "已在运行中":
        print(f"[warmup] 已有任务在运行,跳过。本次启动参数: limit={args.limit} years={args.years}")
        return 1

    total = result["status"]["total"]
    print(f"[warmup] 已启动后台线程,共 {total} 只待拉取 (回看 {args.years} 年)")
    print(f"[warmup] 进程内监听: 每 3 秒打印一次进度, Ctrl+C 退出 (后台线程继续)")

    try:
        while True:
            time.sleep(3)
            s = prewarm.get_status()
            if not s["running"]:
                el = s["finished_at"] - s["started_at"]
                print(f"\n[warmup] 完成: 新增 {s['added']} 只, 成功 {s['ok']} · 失败 {s['fail']} · 用时 {el:.0f}s")
                return 0
            pct = (s["done"] / s["total"] * 100) if s["total"] else 0
            print(f"[warmup] {s['done']}/{s['total']} ({pct:.0f}%) · 当前 {s['current']} · 成功 {s['ok']} · 失败 {s['fail']}", flush=True)
    except KeyboardInterrupt:
        print("\n[warmup] 已 Ctrl+C, 后台线程继续运行(等下一轮). 也可执行 'preheat.stop()' 暂停")
        return 0


if __name__ == "__main__":
    sys.exit(main())