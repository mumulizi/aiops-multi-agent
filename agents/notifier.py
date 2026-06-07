"""Notifier Agent: 渲染告警卡片并打印(后续可换飞书/钉钉)"""
from agents.state import AlertState


_ICON = {
    "critical": "[!!]",
    "high":     "[!] ",
    "medium":   "[*] ",
    "low":      "[-] ",
}


def notifier_node(state: AlertState) -> AlertState:
    sev = state.get("severity", "?")
    label = state.get("label", "?")
    summary = state.get("event_summary", "(no summary)")
    count = state.get("alert_count", 0)
    rca = state.get("rca_hypothesis")

    icon = _ICON.get(sev, "[*] ")

    lines = [
        "=" * 60,
        f"{icon} ALERT NOTIFICATION  severity={sev.upper()}",
        "=" * 60,
        f"label       : {label}",
        f"alert count : {count}",
        f"summary     : {summary}",
    ]
    if rca:
        lines.append(f"hypothesis  : {rca}")
    lines.append("=" * 60)

    text = "\n".join(lines)
    state["notification_text"] = text
    state["notification_sent"] = True
    print()
    print(text)
    return state
