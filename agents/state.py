"""多 Agent 共享状态定义"""
from typing import TypedDict, List, Optional, Literal, Annotated
from operator import add


class AlertState(TypedDict, total=False):
    # === Triage 写 ===
    raw_alerts: List[dict]

    # === Aggregator 写 ===
    event_summary: Optional[str]
    alert_count: int

    # === Classifier 写 ===
    label: Optional[Literal["infra", "app", "business"]]
    severity: Optional[Literal["critical", "high", "medium", "low"]]

    # === Investigator 写(下阶段加) ===
    rca_hypothesis: Optional[str]
    evidence: Annotated[List[dict], add]

    # === Notifier 写 ===
    notification_sent: bool
    notification_text: Optional[str]

    # === 元数据 ===
    trace_id: str
