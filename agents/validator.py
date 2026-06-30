"""Validator Agent: 修复后的健康检查.

v2.12 升级: 默认走异步路径 (VALIDATOR_ASYNC=true)
- 主流程内只做 T+0 立即检查 + "重启无救" 命中判定
- 长时间复查 (30s/2min/10min) 交给 agents.verifier_worker daemon 线程
- 调度不再被单 Pod 30s 阻塞

VALIDATOR_ASYNC=false 回退到旧 sync 路径 (30s 单次检查), 保留兼容性.
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
    """sync 路径下的验证等待时间 (默认 30s). 异步路径不用这个."""
    try:
        return int(os.getenv("VALIDATOR_WAIT_SEC", "30"))
    except Exception:
        return 30


def _is_async() -> bool:
    """是否走异步路径 (默认 true). 设 false 走旧 sync 30s 等待."""
    return os.getenv("VALIDATOR_ASYNC", "true").lower() == "true"


# "重启无救" 型异常 reason 集合.
# 这类故障的根因是配置/镜像/挂载/启动参数, 重启 1000 次也是同样的错.
# Validator 看到这种 reason → 直接告警 "重启不解决问题, 根因不在 runtime, 请人工介入".
_NON_RESTARTABLE_REASONS = {
    "RunContainerError",        # 启动命令/参数错 (container 进程无法 exec)
    "CreateContainerConfigError",  # ConfigMap/Secret 引用错
    "CreateContainerError",     # 容器配置错 (volume mount/security context)
    "InvalidImageName",         # 镜像名拼错
    "ImageInspectError",        # 镜像本身坏
    "ErrImagePull",             # 拉镜像失败 (auth/network/repo)
    "ImagePullBackOff",         # 拉镜像反复退避
    "ErrImageNeverPull",        # imagePullPolicy=Never 但本地无镜像
}


def _diagnose_restart_futility(snap_now: dict) -> tuple:
    """判断"重启无救"型故障. 返回 (is_futile: bool, reasons: list).

    用 _capture_pod_state 已经收集好的 waiting_reasons.
    """
    waiting_reasons = (snap_now or {}).get("waiting_reasons", []) or []
    matched = [r for r in waiting_reasons if r in _NON_RESTARTABLE_REASONS]
    return (len(matched) > 0, matched)


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

    v2.12: 默认 VALIDATOR_ASYNC=true, 走异步路径 (T+0 + 任务表入队 + worker 后续验证).
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

    # === 异步路径 (v2.12 默认) ===
    if _is_async():
        return _validator_async_path(state, ns, name, target, plan)

    # === 同步路径 (回退) ===
    return _validator_sync_path(state, ns, name, target, plan)


def _validator_async_path(state, ns, name, target, plan) -> AlertState:
    """异步路径: T+0 检查 + 写任务表 + 立即返回 pending_async.

    T+0 检查只看两件事 (主流程不阻塞):
    1. 重启无救型故障 → 直接 escalate_human (这种再等也没用)
    2. 否则 → enqueue 异步任务, 写 validation_result.status=pending_async

    长时间复查交给 verifier_worker daemon 线程.
    """
    from tools import verifier_store

    # 立即拿一次快照看是否命中 "重启无救"
    snap_now = _capture_pod_state(ns, name)
    state["snapshot_after"] = snap_now

    is_futile, futile_reasons = _diagnose_restart_futility(snap_now)
    if is_futile:
        result = {
            "status": "escalate_human",
            "verified_at": "t+0",
            "reason": (
                f"重启无救型故障 (waiting.reason={','.join(futile_reasons)}); "
                f"根因不在 runtime, 重启不解决问题, 请人工检查配置/镜像/启动参数"
            ),
            "futile_reasons": futile_reasons,
        }
        state["validation_result"] = result
        _log(f"[Validator] ⚠ {result['status']} (T+0 即判定): "
             f"{result['reason'][:100]}")
        record_audit({
            "stage": "validator", "trace_id": state.get("trace_id"),
            "target": target, "result": result["status"],
            "reason": result["reason"], "mode": "async-t+0",
        })
        return state

    # 排进异步队列
    try:
        task_id = verifier_store.enqueue(state, plan)
    except Exception as e:
        # 入队失败兜底 → 回退到 sync 路径单次验证
        _log(f"[Validator] ⚠ 异步入队失败 ({e}), 回退 sync 一次性验证")
        return _validator_sync_path(state, ns, name, target, plan)

    state["validation_result"] = {
        "status": "pending_async",
        "verified_at": "t+0",
        "reason": "已派单异步验证 (30s / 2min / 10min 三轮覆盖)",
        "task_id": task_id,
    }
    _log(f"[Validator] ⏳ 派单异步验证 task_id={task_id[:12]} "
         f"target={target} action={plan.get('action')}")
    record_audit({
        "stage": "validator", "trace_id": state.get("trace_id"),
        "target": target, "result": "pending_async",
        "task_id": task_id, "mode": "async",
    })
    return state


def _validator_sync_path(state, ns, name, target, plan) -> AlertState:
    """旧 sync 路径: 主流程内 sleep 30s 等 Pod 稳定. 通过 VALIDATOR_ASYNC=false 启用."""
    snap_before = state.get("snapshot_before") or {}
    before_restarts = snap_before.get("total_restarts", 0)

    wait_sec = _wait_seconds()
    _log(f"[Validator] (sync) waiting {wait_sec}s for pod to stabilize...")
    time.sleep(wait_sec)

    snap_now = _capture_pod_state(ns, name)
    state["snapshot_after"] = snap_now

    # 优先检查 "重启无救" 型故障
    is_futile, futile_reasons = _diagnose_restart_futility(snap_now)
    if is_futile:
        result = {
            "status": "escalate_human",
            "verified_at": f"{_wait_seconds()}s",
            "reason": (
                f"重启无救型故障 (waiting.reason={','.join(futile_reasons)}); "
                f"根因不在 runtime, 重启不解决问题, 请人工检查配置/镜像/启动参数"
            ),
            "futile_reasons": futile_reasons,
        }
        state["validation_result"] = result
        _log(f"[Validator] ⚠ {result['status']}: {result['reason']}")
        record_audit({
            "stage": "validator", "trace_id": state.get("trace_id"),
            "target": target, "result": result["status"],
            "reason": result["reason"],
        })
        return state

    if snap_now.get("error"):
        action = plan.get("action", "")
        if action in ("delete_evicted_pod", "delete_completed_job_pod"):
            result = {
                "status": "success",
                "verified_at": f"{wait_sec}s",
                "reason": "pod deleted as expected",
            }
        elif action == "restart_pod":
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
        result = {
            "status": "pending",
            "verified_at": f"{wait_sec}s",
            "reason": f"phase={now_phase} not_ready={any_not_ready}, needs longer observation",
        }

    state["validation_result"] = result
    _log(f"[Validator] {result['status']}: {result.get('reason', '')}")

    # v2.3 故障 Memory: success 时记录修复成功
    if result["status"] == "success":
        fp = state.get("fingerprint")
        if fp and not state.get("from_memory"):
            try:
                from tools.fault_memory import record_success
                rca = state.get("rca_hypothesis", "")
                confidence = "中"
                if "置信度: 高" in rca or "置信度:高" in rca:
                    confidence = "高"
                elif "置信度: 低" in rca or "置信度:低" in rca:
                    confidence = "低"
                first_alert = (state.get("raw_alerts") or [{}])[0]
                labels = first_alert.get("labels") or {}
                ns2 = labels.get("namespace", "") or first_alert.get("namespace", "")
                alertname = labels.get("alertname", "") or \
                    first_alert.get("alertname", "")
                record_success(fp, ns2, alertname, rca, plan, confidence=confidence)
                _log(f"[Validator] 📌 写入 Memory fp={fp} confidence={confidence}")
            except Exception as e:
                _log(f"[Validator] ⚠ Memory 写入失败 (不影响主流程): {e}")

    # v2.3: failed 时记录失败上下文, 给闭环重诊用
    if result["status"] == "failed":
        state["last_failed_plan"] = dict(plan)
        state["last_failure_reason"] = result.get("reason", "")
        retry_count = state.get("retry_count", 0)
        _log(f"[Validator] ⟳ 失败上下文已记录, 当前 retry_count={retry_count}")

    record_audit({
        "stage": "validator", "trace_id": state.get("trace_id"),
        "target": target, "result": result["status"],
        "reason": result.get("reason", ""),
        "retry_count": state.get("retry_count", 0),
        "from_memory": state.get("from_memory", False),
    })
    return state
