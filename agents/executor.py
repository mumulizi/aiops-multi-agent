"""Executor Agent: 执行 L3 白名单动作 + 前后快照 + 审计.

只接受 approval_decision=='executor' 的请求.
"""
import os
import sys
import time

from agents.state import AlertState
from tools.remediation_actions import (
    execute_action,
    is_l3_allowed,
    _capture_pod_state,
    _split_target,
)
from tools.safety_guards import record_audit


def _log(msg):
    print(msg, flush=True)
    sys.stdout.flush()


def _is_dry_run() -> bool:
    return os.getenv("AUTO_HEAL_DRY_RUN", "true").lower() == "true"


def _heal_enabled() -> bool:
    return os.getenv("AUTO_HEAL_ENABLED", "false").lower() == "true"


def executor_node(state: AlertState) -> AlertState:
    plan = state.get("remediation_plan") or {}
    action = plan.get("action", "")
    target = plan.get("target", "")

    # 双重校验 (防止 Approval Gate 被绕过)
    if not is_l3_allowed(action):
        state["execution_status"] = "rejected"
        state["execution_log"] = f"action {action} not in L3 whitelist (double-check)"
        _log(f"[Executor] ✗ rejected: {state['execution_log']}")
        record_audit({
            "stage": "executor", "trace_id": state.get("trace_id"),
            "action": action, "target": target,
            "result": "rejected_double_check",
        })
        return state

    if not _heal_enabled():
        state["execution_status"] = "skipped"
        state["execution_log"] = "AUTO_HEAL_ENABLED=false"
        _log("[Executor] - skipped (kill switch off)")
        return state

    # T-1: target 实存性预检 (防御 LLM 幻觉 / Approval 漏校验)
    # 不同 action 的 target 形态不同, 只对 Pod 操作做存在性预检.
    # cordon_node / scale_deployment / rollback_deployment 由各自的 action 函数自检.
    POD_LEVEL_ACTIONS = {
        "delete_evicted_pod", "delete_completed_job_pod", "delete_failed_pod",
        "restart_pod", "restart_pod_for_image_pull", "restart_statefulset_pod",
    }
    ns, name = _split_target(target)
    if action in POD_LEVEL_ACTIONS and ns and name:
        try:
            from kubernetes import client, config as k8s_config
            try:
                k8s_config.load_incluster_config()
            except Exception:
                k8s_config.load_kube_config()
            client.CoreV1Api().read_namespaced_pod(name, ns)
        except Exception as e:
            status = getattr(e, "status", None)
            if status == 404:
                state["execution_status"] = "aborted"
                state["execution_log"] = f"target {target} 不存在 (404), 拒绝执行 (LLM 幻觉?)"
                _log(f"[Executor] ✗ aborted: {state['execution_log']}")
                record_audit({
                    "stage": "executor", "trace_id": state.get("trace_id"),
                    "action": action, "target": target,
                    "result": "aborted_target_not_found",
                })
                return state
            # 其他异常 (权限/网络) 不应阻止执行, 让 execute_action 自己报错
            _log(f"[Executor] ⚠ target 预检异常 (非 404): {e}, 继续执行")

    # T0: 执行前快照 (仅对 Pod 操作快照, 节点/Deployment 操作没有 Pod 状态可比)
    if action in POD_LEVEL_ACTIONS and ns and name:
        snap_before = _capture_pod_state(ns, name)
        state["snapshot_before"] = snap_before
        _log(f"[Executor] T0 snapshot: phase={snap_before.get('phase')} "
             f"restarts={snap_before.get('total_restarts')}")
    else:
        snap_before = {}
        state["snapshot_before"] = {}

    # T1: 执行
    dry_run = _is_dry_run()
    _log(f"[Executor] executing action={action} target={target} dry_run={dry_run}")
    # scale_deployment 需要从 plan.extra 拿 delta / replicas
    extra = (plan.get("extra") or {}) if isinstance(plan.get("extra"), dict) else {}
    if action == "scale_deployment":
        result = execute_action(action, target, dry_run=dry_run, **{
            k: v for k, v in extra.items() if k in ("replicas", "delta")
        })
    else:
        result = execute_action(action, target, dry_run=dry_run)

    if result.get("dry_run"):
        state["execution_status"] = "dry_run"
        state["execution_log"] = result.get("message", "")
        _log(f"[Executor] ✓ {result.get('message')}")
    elif result.get("ok"):
        state["execution_status"] = "executed"
        state["execution_log"] = result.get("message", "")
        _log(f"[Executor] ✓ {result.get('message')}")
    else:
        state["execution_status"] = "failed"
        state["execution_log"] = result.get("reason", "unknown error")
        _log(f"[Executor] ✗ failed: {result.get('reason')}")

    # T2: 立即后快照 (dry-run 跳过等待)
    if not dry_run and result.get("ok") and ns and name:
        time.sleep(2)  # 给 K8s 一点时间
        snap_after = _capture_pod_state(ns, name)
        state["snapshot_after"] = snap_after
        _log(f"[Executor] T2 snapshot: phase={snap_after.get('phase')} "
             f"restarts={snap_after.get('total_restarts')}")
    else:
        state["snapshot_after"] = {}

    # 审计
    record_audit({
        "stage": "executor", "trace_id": state.get("trace_id"),
        "action": action, "target": target,
        "result": state["execution_status"],
        "log": state.get("execution_log", "")[:200],
        "dry_run": dry_run,
    })
    return state
