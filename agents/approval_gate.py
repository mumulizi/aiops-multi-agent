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
