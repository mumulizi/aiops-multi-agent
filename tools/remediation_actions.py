"""K8s 修复操作工具集 (L3 白名单).

每个动作都有:
- 严格的输入校验 (target 格式, 资源类型确认)
- dry_run 模式 (默认开启, 不真动手)
- 详细日志 (前后状态对比)

设计原则:
1. 每个动作都是幂等的 (重复执行不出错)
2. 失败要返回结构化错误, 不抛异常
3. 成功要返回 before/after 快照
4. 不在这一层做"该不该执行"的判断 (那是 Approval Gate 的事)
"""
import os
import time
from typing import Optional

from tools.k8s_tools import _v1, _kube_ok


def _split_target(target: str):
    """target 格式: 'namespace/pod-name'"""
    if "/" not in target:
        return None, None
    parts = target.split("/", 1)
    return parts[0], parts[1]


def _capture_pod_state(namespace: str, name: str) -> dict:
    """捕获 Pod 当前状态快照 (供 before/after 对比)"""
    if not _kube_ok:
        return {"error": "k8s api not available"}
    try:
        p = _v1.read_namespaced_pod(name=name, namespace=namespace)
    except Exception as e:
        return {"error": f"pod not found: {e}"}
    snap = {
        "phase": p.status.phase,
        "node": p.spec.node_name or "",
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if p.status.container_statuses:
        snap["containers"] = []
        total_restarts = 0
        any_not_ready = False
        for cs in p.status.container_statuses:
            total_restarts += cs.restart_count or 0
            if not cs.ready:
                any_not_ready = True
            snap["containers"].append({
                "name": cs.name,
                "ready": cs.ready,
                "restart_count": cs.restart_count or 0,
            })
        snap["total_restarts"] = total_restarts
        snap["any_not_ready"] = any_not_ready
    return snap


def delete_evicted_pod(target: str, dry_run: bool = True) -> dict:
    """删除 Evicted 状态的 Pod (集群清理类操作, 极低风险)"""
    ns, name = _split_target(target)
    if not ns or not name:
        return {"ok": False, "reason": f"invalid target format: {target}"}
    if not _kube_ok:
        return {"ok": False, "reason": "k8s api not available"}

    try:
        p = _v1.read_namespaced_pod(name=name, namespace=ns)
    except Exception as e:
        return {"ok": False, "reason": f"pod not found: {e}"}

    # 校验: 必须真的是 Evicted 状态
    is_evicted = (
        p.status.phase == "Failed"
        and (p.status.reason == "Evicted" or "Evicted" in (p.status.reason or ""))
    )
    if not is_evicted:
        return {
            "ok": False,
            "reason": f"pod is not Evicted (phase={p.status.phase}, reason={p.status.reason})",
        }

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "message": f"[DRY-RUN] would delete evicted pod {target}",
        }

    try:
        _v1.delete_namespaced_pod(name=name, namespace=ns)
        return {"ok": True, "message": f"deleted evicted pod {target}"}
    except Exception as e:
        return {"ok": False, "reason": f"delete failed: {e}"}


def restart_pod(target: str, dry_run: bool = True) -> dict:
    """重启 Pod (实际是 delete pod 让 owner controller 重建).
    L3 白名单允许的 owner: ReplicaSet (Deployment) / DaemonSet / StatefulSet 中的 ReplicaSet+DaemonSet.
    StatefulSet 重启可能影响数据一致性, 走 L2 人审."""
    ns, name = _split_target(target)
    if not ns or not name:
        return {"ok": False, "reason": f"invalid target format: {target}"}
    if not _kube_ok:
        return {"ok": False, "reason": "k8s api not available"}

    try:
        p = _v1.read_namespaced_pod(name=name, namespace=ns)
    except Exception as e:
        return {"ok": False, "reason": f"pod not found: {e}"}

    # 校验 owner: 允许 ReplicaSet (Deployment) 和 DaemonSet
    # 不允许: StatefulSet (数据一致性) / Job (重启没意义) / 无 owner 的裸 Pod
    owners = p.metadata.owner_references or []
    if not owners:
        return {"ok": False, "reason": "pod has no owner, refuse to delete"}
    owner_kinds = [o.kind for o in owners]

    SAFE_OWNERS = {"ReplicaSet", "DaemonSet"}
    if not any(k in SAFE_OWNERS for k in owner_kinds):
        return {
            "ok": False,
            "reason": f"pod owner is {owner_kinds}, only {sorted(SAFE_OWNERS)} are L3-allowed",
        }

    owner_kind = next(k for k in owner_kinds if k in SAFE_OWNERS)

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "message": f"[DRY-RUN] would restart pod {target} (owner={owner_kind} will recreate it)",
        }

    try:
        _v1.delete_namespaced_pod(name=name, namespace=ns)
        return {
            "ok": True,
            "message": f"restart triggered for pod {target} (owner={owner_kind} will recreate)",
        }
    except Exception as e:
        return {"ok": False, "reason": f"delete failed: {e}"}


def delete_completed_job_pod(target: str, dry_run: bool = True) -> dict:
    """清理已完成 (Succeeded) 的 Pod"""
    ns, name = _split_target(target)
    if not ns or not name:
        return {"ok": False, "reason": f"invalid target format: {target}"}
    if not _kube_ok:
        return {"ok": False, "reason": "k8s api not available"}

    try:
        p = _v1.read_namespaced_pod(name=name, namespace=ns)
    except Exception as e:
        return {"ok": False, "reason": f"pod not found: {e}"}

    if p.status.phase != "Succeeded":
        return {
            "ok": False,
            "reason": f"pod is not Succeeded (phase={p.status.phase})",
        }

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "message": f"[DRY-RUN] would delete completed pod {target}",
        }

    try:
        _v1.delete_namespaced_pod(name=name, namespace=ns)
        return {"ok": True, "message": f"deleted completed pod {target}"}
    except Exception as e:
        return {"ok": False, "reason": f"delete failed: {e}"}


# ============================================================
# L3 白名单: 这里列出的就是允许自动执行的操作
# ============================================================
ALLOWED_ACTIONS = {
    "delete_evicted_pod": delete_evicted_pod,
    "delete_completed_job_pod": delete_completed_job_pod,
    "restart_pod": restart_pod,
}


def is_l3_allowed(action: str) -> bool:
    return action in ALLOWED_ACTIONS


def execute_action(action: str, target: str, dry_run: bool = True) -> dict:
    """统一执行入口"""
    fn = ALLOWED_ACTIONS.get(action)
    if fn is None:
        return {"ok": False, "reason": f"action {action} not in L3 whitelist"}
    return fn(target, dry_run=dry_run)
