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

    # === Investigator 写 ===
    rca_hypothesis: Optional[str]
    evidence: Annotated[List[dict], add]

    # === Remediator 写 (Phase 1) ===
    # 修复计划 JSON: {action, target, safety_level, rationale, rollback, expected_outcome}
    remediation_plan: Optional[dict]
    # Approval Gate 决策: "executor" | "human_review" | "skip" | "reject"
    approval_decision: Optional[str]
    approval_reason: Optional[str]

    # === Executor 写 (Phase 3) ===
    execution_status: Optional[Literal["executed", "skipped", "failed", "rejected", "dry_run"]]
    execution_log: Optional[str]
    snapshot_before: Optional[dict]
    snapshot_after: Optional[dict]

    # === Validator 写 (Phase 4) ===
    # {status: success|failed|timeout|skipped, verified_at, reason}
    validation_result: Optional[dict]

    # === Notifier 写 ===
    notification_sent: bool
    notification_text: Optional[str]

    # === 元数据 ===
    trace_id: str
