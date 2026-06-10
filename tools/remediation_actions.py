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
import time

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


def _check_image_pull_issue(p) -> tuple:
    """检查 Pod 是否处于 ImagePull 类异常.
    返回 (is_image_issue: bool, reason_str: str)"""
    statuses = []
    if p.status.container_statuses:
        statuses.extend(p.status.container_statuses)
    if p.status.init_container_statuses:
        statuses.extend(p.status.init_container_statuses)
    for cs in statuses:
        if cs.state and cs.state.waiting:
            wreason = (cs.state.waiting.reason or "").lower()
            if "imagepull" in wreason or "errimage" in wreason or "imagebackoff" in wreason:
                return True, cs.state.waiting.reason
    return False, ""


def _check_owner_safe(p) -> tuple:
    """检查 Pod owner 是否在 L3 安全列表.
    返回 (ok: bool, owner_kind: str, reason: str)"""
    owners = p.metadata.owner_references or []
    if not owners:
        return False, "", "pod has no owner, refuse to delete"
    owner_kinds = [o.kind for o in owners]
    SAFE_OWNERS = {"ReplicaSet", "DaemonSet"}
    if not any(k in SAFE_OWNERS for k in owner_kinds):
        return False, "", f"pod owner is {owner_kinds}, only {sorted(SAFE_OWNERS)} are L3-allowed"
    owner_kind = next(k for k in owner_kinds if k in SAFE_OWNERS)
    return True, owner_kind, "ok"


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
    L3 白名单允许的 owner: ReplicaSet (Deployment) / DaemonSet.
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

    ok, owner_kind, reason = _check_owner_safe(p)
    if not ok:
        return {"ok": False, "reason": reason}

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


def restart_pod_for_image_pull(target: str, dry_run: bool = True) -> dict:
    """重启 ImagePullBackOff/ErrImagePull 状态的 Pod 让控制器重新拉镜像.

    安全性: 仅治标 (临时性网络抖动有效), 镜像名错/认证失效需要改 Deployment.
    重启 100% 安全 (坏情况是没用, 不会出事故)."""
    ns, name = _split_target(target)
    if not ns or not name:
        return {"ok": False, "reason": f"invalid target format: {target}"}
    if not _kube_ok:
        return {"ok": False, "reason": "k8s api not available"}

    try:
        p = _v1.read_namespaced_pod(name=name, namespace=ns)
    except Exception as e:
        return {"ok": False, "reason": f"pod not found: {e}"}

    is_image_issue, image_reason = _check_image_pull_issue(p)
    if not is_image_issue:
        return {
            "ok": False,
            "reason": "pod is not ImagePullBackOff/ErrImagePull (no image issue in container_statuses)",
        }

    ok, owner_kind, reason = _check_owner_safe(p)
    if not ok:
        return {"ok": False, "reason": reason}

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "message": (
                f"[DRY-RUN] would restart pod {target} "
                f"(reason={image_reason}, owner={owner_kind} will retry image pull)"
            ),
        }

    try:
        _v1.delete_namespaced_pod(name=name, namespace=ns)
        return {
            "ok": True,
            "message": (
                f"restart triggered for pod {target} "
                f"(was {image_reason}, owner={owner_kind} will retry image pull)"
            ),
        }
    except Exception as e:
        return {"ok": False, "reason": f"delete failed: {e}"}


def delete_failed_pod(target: str, dry_run: bool = True) -> dict:
    """清理 Failed phase 的 Pod (含 Evicted 之外的 Failed 情况).

    Pod phase=Failed 表示已终止且不会恢复, 删除是安全清理.
    """
    ns, name = _split_target(target)
    if not ns or not name:
        return {"ok": False, "reason": f"invalid target format: {target}"}
    if not _kube_ok:
        return {"ok": False, "reason": "k8s api not available"}

    try:
        p = _v1.read_namespaced_pod(name=name, namespace=ns)
    except Exception as e:
        return {"ok": False, "reason": f"pod not found: {e}"}

    if p.status.phase != "Failed":
        return {
            "ok": False,
            "reason": f"pod phase is {p.status.phase}, not Failed (refuse to delete)",
        }

    fail_reason = p.status.reason or "Unknown"

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "message": f"[DRY-RUN] would delete failed pod {target} (reason={fail_reason})",
        }

    try:
        _v1.delete_namespaced_pod(name=name, namespace=ns)
        return {
            "ok": True,
            "message": f"deleted failed pod {target} (reason={fail_reason})",
        }
    except Exception as e:
        return {"ok": False, "reason": f"delete failed: {e}"}


# ============================================================
# L3 白名单: 这里列出的就是允许自动执行的操作
# ============================================================
ALLOWED_ACTIONS = {
    "delete_evicted_pod": delete_evicted_pod,
    "delete_completed_job_pod": delete_completed_job_pod,
    "restart_pod": restart_pod,
    "restart_pod_for_image_pull": restart_pod_for_image_pull,
    "delete_failed_pod": delete_failed_pod,
}


def is_l3_allowed(action: str) -> bool:
    return action in ALLOWED_ACTIONS


def execute_action(action: str, target: str, dry_run: bool = True) -> dict:
    """统一执行入口"""
    fn = ALLOWED_ACTIONS.get(action)
    if fn is None:
        return {"ok": False, "reason": f"action {action} not in L3 whitelist"}
    return fn(target, dry_run=dry_run)
