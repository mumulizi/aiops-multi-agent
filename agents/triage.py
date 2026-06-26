"""Triage Agent: 解析 webhook payload, 提取关键字段"""
from agents.state import AlertState


def triage_node(state: AlertState) -> AlertState:
    raw = state.get("raw_alerts", [])
    cleaned = []
    for a in raw:
        labels = a.get("labels", {}) or {}
        annotations = a.get("annotations", {}) or {}
        cleaned.append({
            "alertname": labels.get("alertname", "unknown"),
            "severity_label": labels.get("severity", "unknown"),
            "instance": labels.get("instance", ""),
            "namespace": labels.get("namespace", ""),
            "summary": annotations.get("summary", ""),
            "description": annotations.get("description", ""),
            "starts_at": a.get("startsAt", ""),
            # 保留原始 labels (含 owner_kind), 供 Remediator/ApprovalGate 后处理使用
            "labels": dict(labels),
        })
    state["raw_alerts"] = cleaned
    state["alert_count"] = len(cleaned)
    print(f"[Triage] 清洗完成,共 {len(cleaned)} 条告警")
    return state
