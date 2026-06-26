"""Approval Gate: 根据 safety_level + 安全护栏决定下一步路径.

不是独立 Agent, 是 LangGraph 的条件路由函数 + 审计写入.

路由结果:
- "executor"     → L3 自动执行
- "human_review" → L2 推 IM 等人审 + SQLite 持久化审批记录
- "skip"         → action=none, 直接通知
- "reject"       → L4 拒绝
"""
import os
import sys
import time
from datetime import datetime

from agents.state import AlertState
from tools.safety_guards import allow as rate_allow, record_audit
from tools.approval_store import create_pending, DEFAULT_TTL_SEC
from tools.im_notify import send_message, format_approval_message


def _log(msg):
    print(msg, flush=True)
    sys.stdout.flush()


def _is_business_hours() -> bool:
    """业务时段保护: 9-18 点不自动执行"""
    h = datetime.now().hour
    return 9 <= h < 18


def _heal_enabled() -> bool:
    """大开关 (默认关闭, 必须 export AUTO_HEAL_ENABLED=true 才开)"""
    return os.getenv("AUTO_HEAL_ENABLED", "false").lower() == "true"


def _is_dry_run() -> bool:
    """dry-run 模式 (默认开启, 即使 enabled 也只打印不动手)"""
    return os.getenv("AUTO_HEAL_DRY_RUN", "true").lower() == "true"


def _validate_target_in_alerts(plan: dict, raw_alerts: list) -> tuple:
    """target sanity check: plan.target 的 ns/pod 必须出现在原始告警里.

    这是抵御 LLM 幻觉的终极防线: LLM 偶尔会输出 default/pod-name-1234567890abcdef
    这种和告警毫无关系的 target, 一旦放过去执行就是无差别误删.

    支持三种 target 格式 (按 action 类型分):
    - "namespace/pod-full-name": Pod 操作 (restart_pod 等), 必须出自原始告警
    - "namespace/deployment-name": scale_deployment / rollback_deployment,
      校验 namespace 命中告警里出现过的 ns
    - "node-name" (不含 /): cordon_node / uncordon_node, 校验 node 命中告警 .node

    返回 (ok: bool, reason: str)
    """
    action = plan.get("action", "")
    target = (plan.get("target") or "").strip()
    if not target:
        return False, "target 为空"

    # === 节点级 action: target 是 node 名 ===
    if action in ("cordon_node", "uncordon_node"):
        valid_nodes = set()
        for a in raw_alerts or []:
            if not isinstance(a, dict):
                continue
            labels = a.get("labels") or {}
            n = labels.get("node") or a.get("node") or ""
            if n:
                valid_nodes.add(n)
        # 兼容 "node/name" 前缀写法
        node_name = target[len("node/"):] if target.startswith("node/") else target
        if not valid_nodes:
            return False, "原始告警没有 node 字段, 无法校验 cordon target"
        if node_name not in valid_nodes:
            return False, f"target node {node_name!r} 不在告警节点列表: {sorted(valid_nodes)}"
        return True, ""

    # === Deployment 级 action: target = "ns/deployment-name" ===
    # 只校验 namespace 在告警里出现过 (deployment 名拿不到, 因为告警按 pod 维度)
    if action in ("scale_deployment", "rollback_deployment"):
        if "/" not in target:
            return False, f"target 必须是 'namespace/deployment-name': {target!r}"
        ns, dep = target.split("/", 1)
        if not ns or not dep:
            return False, f"target 格式错 (ns 或 dep 为空): {target!r}"
        valid_ns = set()
        for a in raw_alerts or []:
            if not isinstance(a, dict):
                continue
            labels = a.get("labels") or {}
            rns = labels.get("namespace") or a.get("namespace") or ""
            if rns:
                valid_ns.add(rns)
        if ns not in valid_ns:
            return False, f"target namespace {ns!r} 不在告警 ns 列表: {sorted(valid_ns)}"
        return True, ""

    # === 默认: Pod 级 action, target = "ns/pod-full-name" ===
    if "/" not in target:
        return False, f"target 为空或格式错: {target!r}"
    ns, pod = target.split("/", 1)

    valid = set()
    for a in raw_alerts or []:
        if not isinstance(a, dict):
            continue
        labels = a.get("labels") or {}
        rns = labels.get("namespace") or a.get("namespace") or ""
        rpod = labels.get("instance") or a.get("instance") or ""
        if rns and rpod:
            valid.add((rns, rpod))

    if not valid:
        return False, "原始告警没有有效的 ns/pod 字段, 无法校验 target"
    if (ns, pod) not in valid:
        sample = list(valid)[:3]
        return False, f"target {target} 不在告警 Pod 列表 (示例: {sample})"
    return True, ""


def approval_gate_route(state: AlertState) -> str:
    """LangGraph 条件路由函数. 仅返回路径名, 不修改 state.
    具体决策原因写在 approval_gate_node 里."""
    plan = state.get("remediation_plan") or {}
    decision = state.get("approval_decision", "")
    if not decision:
        # approval_gate_node 还没跑就来这儿是异常情况, 兜底跳过
        return "skip"
    return decision


def approval_gate_node(state: AlertState) -> AlertState:
    """显式的安全门节点: 计算 approval_decision + approval_reason 并写审计"""
    plan = state.get("remediation_plan") or {}
    action = plan.get("action", "none")
    target = plan.get("target", "")
    safety = plan.get("safety_level", "L2")

    decision = "skip"
    reason = ""

    # --- 决策树 ---
    if action == "none":
        decision = "skip"
        reason = "no action needed"

    elif safety == "L4":
        decision = "reject"
        reason = "L4 high-risk action, never auto-execute"

    else:
        # 任何要真正执行的 action (L2/L3) 都必须先过 target sanity check
        ok_target, why = _validate_target_in_alerts(plan, state.get("raw_alerts") or [])
        if not ok_target:
            decision = "reject"
            reason = f"target sanity check failed: {why}"
            _log(f"[Approval] ✗ target 校验拒绝: {why}")
        elif safety == "L2":
            decision = "human_review"
            reason = "L2 needs human review"
        elif safety == "L3":
            # L3 进一步走护栏检查
            if not _heal_enabled():
                decision = "human_review"
                reason = "AUTO_HEAL_ENABLED=false (kill switch off), downgrade to L2"
            elif _is_business_hours() and not _is_dry_run():
                decision = "human_review"
                reason = "business hours protection (9-18), downgrade to L2"
            else:
                ok, rate_reason = rate_allow(target, action, max_per_hour=3)
                if not ok:
                    decision = "reject"
                    reason = rate_reason
                else:
                    decision = "executor"
                    reason = "L3 whitelist + safety checks passed"
        else:
            decision = "skip"
            reason = f"unknown safety_level: {safety}"

    state["approval_decision"] = decision
    state["approval_reason"] = reason

    # 如果是 human_review, 写入 SQLite + 推 IM 审批通知
    if decision == "human_review":
        try:
            approval_id = create_pending(plan, state)
            state["approval_id"] = approval_id
            ttl_min = DEFAULT_TTL_SEC // 60
            msg = format_approval_message(approval_id, plan, state, ttl_min)
            push_result = send_message(msg)
            _log(f"[Approval]   approval_id={approval_id} 已推送 IM (sent={push_result.get('im_sent')})")
        except Exception as e:
            _log(f"[Approval]   ⚠ 创建审批记录失败: {e}")

    # 审计: 任何决策都写日志
    record_audit({
        "stage": "approval_gate",
        "trace_id": state.get("trace_id"),
        "action": action,
        "target": target,
        "safety_level": safety,
        "decision": decision,
        "reason": reason,
        "approval_id": state.get("approval_id"),
    })

    icon = {"executor": "✓", "human_review": "?", "skip": "-", "reject": "✗"}.get(decision, "?")
    _log(f"[Approval] {icon} decision={decision} reason={reason}")
    return state
