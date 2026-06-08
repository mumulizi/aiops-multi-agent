"""Validator Agent: 修复后的健康检查.

注意: 默认是"轻量验证" - 只等 30s 看一次 (避免主流程被阻塞 10 分钟).
完整版 30s/2min/10min 三次检查在异步模式 (TODO: 后续 Celery / asyncio).
"""
import os
import sys
import time

from agents.state import AlertState
from tools.remediation_actions import _split_target, _capture_pod_state
from tools.k8s_tools import _v1, _kube_ok
from tools.safety_guards import record_audit


def _log(msg):
    print(msg, flush=True)
    sys.stdout.flush()


def _wait_seconds() -> int:
    """主流程内的验证等待时间 (默认 30s)"""
    try:
        return int(os.getenv("VALIDATOR_WAIT_SEC", "30"))
    except Exception:
        return 30


def _check_pod_recreated_by_owner(namespace: str, old_pod_name: str) -> dict:
    """检查 namespace 下是否有新 Pod 被控制器重建出来 (替代旧 Pod 名).

    判断逻辑:
    - 列出 namespace 所有 Pod
    - 按"前缀相似度"找候选 (DaemonSet/ReplicaSet 命名规律: <prefix>-<hash>)
    - 选时间最新的 Pod 作为新 Pod, 看是否 ready
    """
    if not _kube_ok:
        return {"found": False, "new_pod": "", "ready": False}
    try:
        pods = _v1.list_namespaced_pod(namespace=namespace, timeout_seconds=10).items
    except Exception:
        return {"found": False, "new_pod": "", "ready": False}

    # 提取 prefix: 去掉最后一段 hash (用 - 分隔)
    parts = old_pod_name.rsplit("-", 2)
    if len(parts) < 2:
        prefix = old_pod_name
    else:
        prefix = parts[0]  # e.g. "harbor-registry-68b7bcb984" -> "harbor-registry"

    # 找 prefix 匹配的 Pod, 排除原 Pod 名
    candidates = []
    for p in pods:
        if not p.metadata.name.startswith(prefix):
            continue
        if p.metadata.name == old_pod_name:
            continue
        candidates.append(p)

    if not candidates:
        return {"found": False, "new_pod": "", "ready": False}

    # 选最新创建的
    latest = max(candidates, key=lambda x: x.metadata.creation_timestamp or 0)

    # 看 ready
    ready = False
    if latest.status.container_statuses:
        ready = all(cs.ready for cs in latest.status.container_statuses)

    return {
        "found": True,
        "new_pod": latest.metadata.name,
        "phase": latest.status.phase,
        "ready": ready,
    }


def validator_node(state: AlertState) -> AlertState:
    """修复后等一会, 看 Pod 是否恢复.

    判定:
    - executed + Pod Ready + 重启次数没继续涨 → success
    - executed + 重启 +5 → failed (修复无效, 触发再诊断)
    - dry_run / skipped / rejected → skipped (没执行就不验证)
    """
    exec_status = state.get("execution_status", "")
    plan = state.get("remediation_plan") or {}
    target = plan.get("target", "")

    # 没执行就跳过验证
    if exec_status not in ("executed",):
        state["validation_result"] = {
            "status": "skipped",
            "reason": f"no real execution (status={exec_status})",
        }
        if exec_status == "dry_run":
            _log("[Validator] - skipped (dry-run, nothing to verify)")
        else:
            _log(f"[Validator] - skipped (status={exec_status})")
        return state

    ns, name = _split_target(target)
    if not ns or not name:
        state["validation_result"] = {
            "status": "skipped",
            "reason": "invalid target",
        }
        return state

    snap_before = state.get("snapshot_before") or {}
    before_restarts = snap_before.get("total_restarts", 0)

    wait_sec = _wait_seconds()
    _log(f"[Validator] waiting {wait_sec}s for pod to stabilize...")
    time.sleep(wait_sec)

    snap_now = _capture_pod_state(ns, name)
    state["snapshot_after"] = snap_now

    if snap_now.get("error"):
        # Pod 不存在了: 区分情况
        action = plan.get("action", "")
        if action in ("delete_evicted_pod", "delete_completed_job_pod"):
            result = {
                "status": "success",
                "verified_at": f"{wait_sec}s",
                "reason": "pod deleted as expected",
            }
        elif action == "restart_pod":
            # restart_pod = delete + 控制器重建. 旧 Pod 名消失是预期行为.
            # 进一步: 检查控制器是否真的重建了新 Pod (按 owner 找)
            owners = (snap_before.get("containers") and []) or []
            # 简化: 直接看同 namespace 同 prefix 的 Pod 数量是否还在 (DaemonSet/RS 重建会用新名字)
            recreated = _check_pod_recreated_by_owner(ns, name)
            if recreated["found"]:
                result = {
                    "status": "success",
                    "verified_at": f"{wait_sec}s",
                    "reason": (
                        f"pod recreated by controller "
                        f"(new pod: {recreated['new_pod']}, ready={recreated['ready']})"
                    ),
                }
            else:
                # 旧 Pod 删了但新的还没起 → pending (控制器可能还在创建中)
                result = {
                    "status": "pending",
                    "verified_at": f"{wait_sec}s",
                    "reason": "old pod deleted, new pod not yet visible (controller in progress)",
                }
        else:
            result = {
                "status": "failed",
                "verified_at": f"{wait_sec}s",
                "reason": f"pod gone after action ({action}) - unexpected",
            }
        state["validation_result"] = result
        _log(f"[Validator] {result['status']}: {result['reason']}")
        record_audit({
            "stage": "validator", "trace_id": state.get("trace_id"),
            "target": target, "result": result["status"],
            "reason": result["reason"],
        })
        return state

    now_phase = snap_now.get("phase", "")
    now_restarts = snap_now.get("total_restarts", 0)
    any_not_ready = snap_now.get("any_not_ready", True)

    if now_phase == "Running" and not any_not_ready:
        if now_restarts <= before_restarts + 1:
            result = {
                "status": "success",
                "verified_at": f"{wait_sec}s",
                "phase": now_phase,
                "restarts_delta": now_restarts - before_restarts,
            }
        else:
            result = {
                "status": "partial",
                "verified_at": f"{wait_sec}s",
                "reason": f"Ready but restarts +{now_restarts - before_restarts}",
            }
    elif now_restarts > before_restarts + 5:
        result = {
            "status": "failed",
            "verified_at": f"{wait_sec}s",
            "reason": f"restarts continue to grow (+{now_restarts - before_restarts})",
        }
    else:
        # 30s 还没 Ready, 但也没炸 → 给予观察
        result = {
            "status": "pending",
            "verified_at": f"{wait_sec}s",
            "reason": f"phase={now_phase} not_ready={any_not_ready}, needs longer observation",
        }

    state["validation_result"] = result
    _log(f"[Validator] {result['status']}: {result.get('reason', '')}")

    record_audit({
        "stage": "validator", "trace_id": state.get("trace_id"),
        "target": target, "result": result["status"],
        "reason": result.get("reason", ""),
    })
    return state
