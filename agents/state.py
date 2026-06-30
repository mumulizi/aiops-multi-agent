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
    # human_review 时写入的审批 ID (CLI 审批用)
    approval_id: Optional[str]

    # === Executor 写 (Phase 3) ===
    # "aborted" = T-1 实存性预检失败 (target 404, 拒绝执行 LLM 幻觉的 pod)
    execution_status: Optional[Literal["executed", "skipped", "failed", "rejected", "dry_run", "aborted"]]
    execution_log: Optional[str]
    snapshot_before: Optional[dict]
    snapshot_after: Optional[dict]

    # === Validator 写 (Phase 4) ===
    # {status: success|failed|timeout|skipped|escalate_human|pending|pending_async, verified_at, reason}
    # escalate_human: "重启无救"型故障, IM 用 🚨 推
    # pending: 30s 内未恢复, 但也未明确 failed
    # pending_async (v2.12): 已派单异步验证, 主流程不阻塞, daemon 30s/2min/10min 三轮复查
    # failed: 重启次数继续涨 +5 / 状态恶化, 触发 v2.3 闭环回到 Investigator
    validation_result: Optional[dict]

    # === Notifier 写 ===
    notification_sent: bool
    notification_text: Optional[str]

    # === 元数据 ===
    trace_id: str

    # === v2.3 失败再诊断闭环 ===
    # Validator 标 failed 时, router 会跳回 Investigator 重新诊断.
    # retry_count: 已重试次数 (默认 0), 上限由 graph 路由控制 (默认 2).
    retry_count: int
    # 上一次失败的 plan + 失败原因, 给 Investigator/Remediator 当上下文,
    # 防止它们重新生成同样的失败 plan.
    last_failed_plan: Optional[dict]
    last_failure_reason: Optional[str]

    # === v2.3 故障 Memory ===
    # 命中 SQLite Memory → 跳过 Investigator/Remediator (秒级响应).
    from_memory: bool
    # 故障指纹 (namespace + alertname + RCA 前 100 字符 hash), 用于 Memory 查询.
    fingerprint: Optional[str]

    # === v2.4 分级诊断 ===
    # 调度器告诉 Investigator 走哪种深度: "full" (默认 8 步 ReAct) | "light" (3 步快诊)
    # medium 级异常走 light, 节省 LLM 调用又保留可见性.
    investigation_mode: Optional[Literal["full", "light"]]
