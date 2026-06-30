"""异步验证 worker (v2.12 §4): daemon 线程定期扫表跑验证.

为什么放 agents/ 而不是 tools/:
- 这是有"业务逻辑"的执行单元 (调 K8s API 检查 Pod, 决定下一步)
- 不是无状态工具

工作流:
- 每 5s 扫一次 verifier_store, 拿到期的 pending 任务
- 对每个任务复用 validator.py 现有的检查逻辑:
  1. _diagnose_restart_futility - 重启无救型故障升级人审
  2. _capture_pod_state - 取当前 Pod 快照
  3. _check_pod_recreated_by_owner - restart_pod 后看控制器有没有重建新 Pod
- 终态推第二条 IM 通知 (区别于第一条 "已派单" 通知)

防 crash:
- 主循环外层 try/except, 永不退出
- 单任务异常不影响其他任务

线程安全:
- start() 幂等, 多次调用只起一次 worker
"""
import json
import os
import sys
import threading
import time

from tools import verifier_store
from tools.safety_guards import record_audit

# 复用 validator 现有逻辑 (尽量不重复代码)
from agents.validator import (
    _diagnose_restart_futility,
    _check_pod_recreated_by_owner,
)
from tools.remediation_actions import _capture_pod_state

# IM 通知 (终态时发第二条)
try:
    from tools.im_notify import send_message
except Exception:
    send_message = None


_started = False
_start_lock = threading.Lock()

# worker 主循环间隔 (秒)
LOOP_INTERVAL = int(os.getenv("VERIFIER_LOOP_SEC", "5"))


def _log(msg):
    print(f"[verifier_worker] {msg}", flush=True)
    sys.stdout.flush()


# === 单任务验证逻辑 (跟 validator.validator_node 的 sync 路径同源) ===

def _verify_once(task: dict) -> dict:
    """跑单次验证, 返回 result dict: {status, reason, ...}.

    status 取值:
    - success / escalate_human / pending / failed / timeout
    - pending: 本轮没结论, 等下一轮
    - timeout: 已到第 3 轮仍 pending → 终态
    """
    ns = task["namespace"]
    pod = task["pod"]
    action = task["action"]
    state = task.get("state") or {}
    plan = task.get("plan") or {}
    check_round = task["check_round"]

    if not ns or not pod:
        return {"status": "failed", "reason": "missing ns/pod"}

    snap_before = state.get("snapshot_before") or {}
    before_restarts = snap_before.get("total_restarts", 0)

    snap_now = _capture_pod_state(ns, pod)

    # 1. 优先看 "重启无救" 型故障
    is_futile, futile_reasons = _diagnose_restart_futility(snap_now)
    if is_futile:
        return {
            "status": "escalate_human",
            "verified_at": f"round={check_round}",
            "reason": (
                f"重启无救型故障 (waiting.reason={','.join(futile_reasons)}); "
                f"根因不在 runtime, 请人工检查配置/镜像/启动参数"
            ),
            "futile_reasons": futile_reasons,
        }

    # 2. Pod 不见了的处理 (跟 validator sync 路径同逻辑)
    if snap_now.get("error"):
        if action in ("delete_evicted_pod", "delete_completed_job_pod",
                      "delete_failed_pod"):
            return {
                "status": "success",
                "verified_at": f"round={check_round}",
                "reason": "pod deleted as expected",
            }
        if action in ("restart_pod", "restart_pod_for_image_pull",
                      "restart_statefulset_pod"):
            recreated = _check_pod_recreated_by_owner(ns, pod)
            if recreated.get("found"):
                return {
                    "status": "success",
                    "verified_at": f"round={check_round}",
                    "reason": (
                        f"pod recreated by controller "
                        f"(new pod: {recreated['new_pod']}, "
                        f"ready={recreated['ready']})"
                    ),
                }
            # 等下一轮看控制器是否完成重建
            return {
                "status": "pending",
                "verified_at": f"round={check_round}",
                "reason": "old pod deleted, controller still creating",
            }
        return {
            "status": "failed",
            "verified_at": f"round={check_round}",
            "reason": f"pod gone after action ({action}) - unexpected",
        }

    # 3. Pod 还在的处理
    now_phase = snap_now.get("phase", "")
    now_restarts = snap_now.get("total_restarts", 0)
    any_not_ready = snap_now.get("any_not_ready", True)

    if now_phase == "Running" and not any_not_ready:
        if now_restarts <= before_restarts + 1:
            return {
                "status": "success",
                "verified_at": f"round={check_round}",
                "phase": now_phase,
                "restarts_delta": now_restarts - before_restarts,
            }
        # Ready 但重启在涨 → pending, 后续轮次看是否稳定
        return {
            "status": "pending",
            "verified_at": f"round={check_round}",
            "reason": f"ready but restarts +{now_restarts - before_restarts}, 等待稳定",
        }

    # 4. 重启在大量增长 → 修复失败
    if now_restarts > before_restarts + 5:
        return {
            "status": "failed",
            "verified_at": f"round={check_round}",
            "reason": f"restarts continue to grow (+{now_restarts - before_restarts})",
        }

    # 5. 还没 ready, 但也没炸 → 等下一轮
    return {
        "status": "pending",
        "verified_at": f"round={check_round}",
        "reason": f"phase={now_phase} not_ready={any_not_ready}, 等下一轮",
    }


def _notify_terminal(task: dict, result: dict) -> None:
    """终态时推一条 IM (区别于第一条 '已派单' 通知)."""
    if send_message is None:
        return
    status = result.get("status", "?")
    icon = {
        "success": "✅",
        "failed": "❌",
        "escalate_human": "🚨",
        "timeout": "⏰",
    }.get(status, "ℹ️")
    msg = (
        f"{icon} [异步验证] {status}\n"
        f"Pod: {task['namespace']}/{task['pod']}\n"
        f"Action: {task['action']}\n"
        f"Round: {task['check_round']+1}/3\n"
        f"Reason: {result.get('reason', '')[:200]}\n"
        f"TraceID: {task.get('trace_id', '')}"
    )
    try:
        send_message(msg)
    except Exception as e:
        _log(f"IM 推送失败 (不影响主流程): {e}")


def _next_check_at(task: dict) -> tuple:
    """计算下一轮的检查时间. 返回 (next_at, next_round).

    超过 3 轮 (round 0/1/2 都没成功) → 返回 (None, 3) 表示该终态化为 timeout.
    """
    cur_round = task["check_round"]
    created_at = task["created_at"]
    next_round = cur_round + 1
    if next_round >= len(verifier_store.ROUND_OFFSETS):
        return None, next_round
    next_at = created_at + verifier_store.ROUND_OFFSETS[next_round]
    # 如果计算出来的时间已经过去, 至少加 1s 避免立即重跑
    now = int(time.time())
    next_at = max(next_at, now + 1)
    return next_at, next_round


def _handle_one(task: dict) -> None:
    """处理一个到期任务."""
    task_id = task["task_id"]
    try:
        result = _verify_once(task)
    except Exception as e:
        _log(f"task {task_id} 验证抛异常: {e}")
        result = {"status": "failed", "reason": f"verifier exception: {e}"}

    status = result.get("status", "")

    if status in ("success", "escalate_human", "failed"):
        # 终态
        verifier_store.update_status(
            task_id, status=status, last_result=result,
            check_round=task["check_round"] + 1,
        )
        record_audit({
            "stage": "verifier_async",
            "trace_id": task.get("trace_id"),
            "target": f"{task['namespace']}/{task['pod']}",
            "action": task["action"],
            "result": status,
            "round": task["check_round"] + 1,
            "reason": result.get("reason", "")[:200],
        })
        _notify_terminal(task, result)
        _log(f"task {task_id[:8]} round {task['check_round']+1}/3 → {status}: "
             f"{result.get('reason', '')[:100]}")
        return

    # status=pending, 看是否还能再排一轮
    next_at, next_round = _next_check_at(task)
    if next_at is None:
        # 3 轮都跑完仍 pending → timeout
        timeout_result = {
            "status": "timeout",
            "verified_at": "round=3",
            "reason": "3 轮验证 (30s/2min/10min) 后仍未达到 success/failed, 已超时",
            "last_pending_reason": result.get("reason", ""),
        }
        verifier_store.update_status(
            task_id, status="timeout", last_result=timeout_result,
            check_round=next_round,
        )
        record_audit({
            "stage": "verifier_async",
            "trace_id": task.get("trace_id"),
            "target": f"{task['namespace']}/{task['pod']}",
            "action": task["action"],
            "result": "timeout",
            "round": next_round,
        })
        # 把 task 注入 timeout result 通知
        _notify_terminal(task, timeout_result)
        _log(f"task {task_id[:8]} round 3/3 → timeout")
        return

    # 排下一轮
    verifier_store.update_status(
        task_id, last_result=result,
        next_check_at=next_at, check_round=next_round,
    )
    delay = next_at - int(time.time())
    _log(f"task {task_id[:8]} round {task['check_round']+1} pending, "
         f"下一轮 round {next_round+1}/3 在 {delay}s 后")


# === 主循环 ===

def _loop():
    _log(f"daemon 启动, 每 {LOOP_INTERVAL}s 扫一次任务表")
    while True:
        try:
            tasks = verifier_store.claim_due(limit=10)
            if tasks:
                _log(f"拉到 {len(tasks)} 个到期任务")
            for t in tasks:
                try:
                    _handle_one(t)
                except Exception as e:
                    _log(f"task {t.get('task_id')} 处理失败: {e}")
        except Exception as e:
            _log(f"主循环异常 (1s 后重试): {e}")
            time.sleep(1)
        time.sleep(LOOP_INTERVAL)


def start() -> bool:
    """启动 daemon 线程. 幂等, 多次调用只起一次. 返回是否真正启动了."""
    global _started
    with _start_lock:
        if _started:
            return False
        t = threading.Thread(target=_loop, name="verifier_worker", daemon=True)
        t.start()
        _started = True
    _log("线程已启动 (daemon=True, 主进程退出会自动结束)")
    return True
