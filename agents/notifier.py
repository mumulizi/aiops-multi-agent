"""Notifier Agent: 渲染告警卡片并打印 (后续可换飞书/钉钉/如流).

v2.0 升级: 输出 Inspector → Investigator → Remediator → Approval → Executor → Validator
完整链路报告.
v2.0+: 支持 IM 推送 (如流/钉钉/企微/飞书 + 本地审计文件兜底).
"""
from agents.state import AlertState
from tools.im_notify import send_message, format_alert_message, should_push


_ICON = {
    "critical": "[!!]",
    "high":     "[!] ",
    "medium":   "[*] ",
    "low":      "[-] ",
}

_DECISION_ICON = {
    "executor":     "✓ AUTO",
    "human_review": "? REVIEW",
    "skip":         "- SKIP",
    "reject":       "✗ REJECT",
}

_VALIDATION_ICON = {
    "success":  "✓",
    "partial":  "~",
    "pending":  "?",
    "failed":   "✗",
    "timeout":  "⏰",
    "skipped":  "-",
}


def notifier_node(state: AlertState) -> AlertState:
    sev = state.get("severity", "?") or "?"
    label = state.get("label", "?") or "?"
    summary = state.get("event_summary", "(no summary)")
    count = state.get("alert_count", 0)
    rca = state.get("rca_hypothesis")

    icon = _ICON.get(sev, "[*] ")
    sev_upper = sev.upper() if isinstance(sev, str) else "?"

    lines = [
        "=" * 70,
        f"{icon} ALERT NOTIFICATION  severity={sev_upper}",
        "=" * 70,
        f"label       : {label}",
        f"alert count : {count}",
        f"summary     : {summary}",
    ]
    if rca:
        # 截断长根因方便阅读
        rca_short = rca if len(rca) <= 300 else rca[:297] + "..."
        lines.append(f"hypothesis  : {rca_short}")

    # === Phase 1+: 修复决策 ===
    plan = state.get("remediation_plan")
    if plan and isinstance(plan, dict):
        action = plan.get("action", "none")
        target = plan.get("target", "")
        safety = plan.get("safety_level", "?")
        rationale = plan.get("rationale", "")
        rollback = plan.get("rollback", "")
        lines.append("-" * 70)
        lines.append(f"REMEDIATION PLAN")
        lines.append(f"  action      : {action}")
        if target:
            lines.append(f"  target      : {target}")
        # action=none 时 safety 是 N/A, 没必要显示
        if action != "none" and safety and safety != "N/A":
            lines.append(f"  safety      : {safety}")
        if rationale:
            lines.append(f"  rationale   : {rationale[:200]}")
        if rollback and rollback not in ("n/a", "N/A", ""):
            lines.append(f"  rollback    : {rollback[:150]}")

    # === Phase 2: 安全门决策 ===
    decision = state.get("approval_decision")
    if decision:
        decision_icon = _DECISION_ICON.get(decision, "?")
        approval_reason = state.get("approval_reason", "")
        lines.append("-" * 70)
        lines.append(f"APPROVAL GATE  : {decision_icon}")
        if approval_reason:
            lines.append(f"  reason      : {approval_reason}")

    # === Phase 3: 执行结果 ===
    exec_status = state.get("execution_status")
    if exec_status:
        exec_log = state.get("execution_log", "")
        lines.append("-" * 70)
        lines.append(f"EXECUTION      : {exec_status}")
        if exec_log:
            lines.append(f"  log         : {exec_log[:200]}")
        snap_before = state.get("snapshot_before") or {}
        snap_after = state.get("snapshot_after") or {}
        if snap_before and not snap_before.get("error"):
            br = snap_before.get("total_restarts", "?")
            bp = snap_before.get("phase", "?")
            lines.append(f"  before      : phase={bp} restarts={br}")
        if snap_after and not snap_after.get("error"):
            ar = snap_after.get("total_restarts", "?")
            ap = snap_after.get("phase", "?")
            lines.append(f"  after       : phase={ap} restarts={ar}")

    # === Phase 4: 验证结果 ===
    validation = state.get("validation_result")
    if validation and isinstance(validation, dict):
        status = validation.get("status", "?")
        v_icon = _VALIDATION_ICON.get(status, "?")
        v_reason = validation.get("reason", "")
        verified_at = validation.get("verified_at", "")
        lines.append("-" * 70)
        lines.append(f"VALIDATION     : {v_icon} {status}")
        if verified_at:
            lines.append(f"  verified_at : {verified_at}")
        if v_reason:
            lines.append(f"  reason      : {v_reason[:200]}")

    lines.append("=" * 70)

    text = "\n".join(lines)
    state["notification_text"] = text
    state["notification_sent"] = True
    print()
    print(text)

    # IM 推送 (默认仅对重要事件: critical/high/已执行/被拒)
    if should_push(state):
        im_text = format_alert_message(state)
        result = send_message(im_text)
        if result.get("im_sent"):
            print(f"[Notifier] IM 推送成功 ({result.get('im_status')})", flush=True)
        elif result.get("error"):
            print(f"[Notifier] IM 推送失败: {result['error']}", flush=True)
        if result.get("local_file"):
            print(f"[Notifier] 本地审计: {result['local_file']}", flush=True)

    return state
