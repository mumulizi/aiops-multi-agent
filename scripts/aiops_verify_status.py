#!/usr/bin/env python3
"""查看异步验证任务表 (v2.12).

usage:
  python scripts/aiops_verify_status.py            # 全部 (pending 优先)
  python scripts/aiops_verify_status.py --pending  # 只看 pending
  python scripts/aiops_verify_status.py --limit 100
"""
import argparse
import json
import os
import sys
import time

# 允许从仓库根运行
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import verifier_store


def _fmt_age(ts: int) -> str:
    """秒级时间戳 → '1m20s' / '8m' / '12h' 这种简短格式."""
    if not ts:
        return "?"
    sec = int(time.time()) - int(ts)
    if sec < 0:
        return f"+{-sec}s"  # 未来时间
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m{sec % 60}s"
    return f"{sec // 3600}h"


def _fmt_eta(ts: int) -> str:
    """未来时间戳 → '+40s'; 已过去 → '过期'."""
    if not ts:
        return "-"
    diff = int(ts) - int(time.time())
    if diff <= 0:
        return "due now"
    if diff < 60:
        return f"+{diff}s"
    return f"+{diff // 60}m{diff % 60}s"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pending", action="store_true",
                        help="只看 pending 任务")
    parser.add_argument("--limit", type=int, default=20,
                        help="返回条数上限 (默认 20)")
    parser.add_argument("--json", action="store_true",
                        help="输出 JSON 而不是表格")
    args = parser.parse_args()

    status = "pending" if args.pending else None
    tasks = verifier_store.list_recent(limit=args.limit, status=status)

    if args.json:
        print(json.dumps(tasks, ensure_ascii=False, indent=2, default=str))
        return

    if not tasks:
        print("(无任务)")
        return

    # 统计
    stats = {}
    for t in tasks:
        s = t.get("status", "?")
        stats[s] = stats.get(s, 0) + 1
    print(f"任务总数: {len(tasks)} | {' | '.join(f'{k}={v}' for k, v in stats.items())}")
    print("-" * 110)
    print(f"{'TASK_ID':<10} {'AGE':<8} {'NS/POD':<40} {'ACTION':<22} "
          f"{'ROUND':<6} {'STATUS':<14} {'NEXT':<12}")
    print("-" * 110)
    for t in tasks:
        tid = t["task_id"][:8]
        age = _fmt_age(t.get("created_at"))
        target = f"{t.get('namespace', '')}/{t.get('pod', '')}"
        target = target[:38] + ".." if len(target) > 40 else target
        action = (t.get("action") or "")[:22]
        rnd = f"{t.get('check_round', 0)}/3"
        status_str = t.get("status", "?")
        if status_str == "pending":
            nxt = _fmt_eta(t.get("check_at"))
        else:
            nxt = _fmt_age(t.get("updated_at")) + " ago"
        print(f"{tid:<10} {age:<8} {target:<40} {action:<22} {rnd:<6} "
              f"{status_str:<14} {nxt:<12}")
        # 终态时附最后一行 reason
        last = t.get("last_result")
        if last and last.get("reason") and status_str != "pending":
            reason = last["reason"][:90]
            print(f"           └─ {reason}")


if __name__ == "__main__":
    main()
