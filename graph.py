"""LangGraph 编排器: v2.3 自愈闭环 (失败自动再诊断).

流水线 (按 severity 分流):

低优先级:
  Triage → Aggregator → Classifier → Notifier

critical/high:
  Triage → Aggregator → Classifier → Investigator → Remediator → ApprovalGate
                                          ▲                              │
                                          │                              │
                                          │ v2.3: failed + retry<2       │
                                          │       回到 Investigator      │
                       ┌──────────────────┴───────────────────┬──────────┴──────────────────┐
                       │ executor                             │ human_review / skip / reject │
                       ▼                                      ▼                              │
                    Executor → Validator                                                     │
                                  │                                                          │
                       ┌──────────┴──────────┐                                               │
                       │ success/escalate    │ failed (v2.3 重试)                            │
                       ▼                     │                                               │
                    Notifier ◄───────────────┴───────────────────────────────────────────────┘
"""
import os

from langgraph.graph import StateGraph, END
from agents.state import AlertState
from agents.triage import triage_node
from agents.aggregator import aggregator_node
from agents.classifier import classifier_node
from agents.investigator import investigator_node
from agents.remediator import remediator_node
from agents.approval_gate import approval_gate_node, approval_gate_route
from agents.executor import executor_node
from agents.validator import validator_node
from agents.notifier import notifier_node


# v2.3 闭环最大重试次数 (从 Validator 跳回 Investigator 的次数).
# 默认 2: 即一个故障最多走 3 次诊断 (初次 + 2 次重试).
# 通过环境变量 SELF_HEAL_MAX_RETRIES 调整.
def _max_retries() -> int:
    try:
        return int(os.getenv("SELF_HEAL_MAX_RETRIES", "2"))
    except Exception:
        return 2


def _route_by_severity(state: AlertState) -> str:
    sev = state.get("severity", "medium")
    if sev in ("critical", "high"):
        return "investigator"
    return "notifier"


def _route_after_validator(state: AlertState) -> str:
    """v2.3 Validator 后路由:
    - failed + 未达重试上限 → 回 Investigator 重新诊断
    - 其他 → Notifier (success/pending/escalate_human/skipped 都走通知)
    """
    result = state.get("validation_result") or {}
    status = result.get("status", "")
    retry_count = state.get("retry_count", 0)

    if status == "failed" and retry_count < _max_retries():
        return "investigator"
    return "notifier"


def build_graph():
    g = StateGraph(AlertState)

    # 已有节点
    g.add_node("triage", triage_node)
    g.add_node("aggregator", aggregator_node)
    g.add_node("classifier", classifier_node)
    g.add_node("investigator", investigator_node)

    # v2.0 新增节点
    g.add_node("remediator", remediator_node)
    g.add_node("approval_gate", approval_gate_node)
    g.add_node("executor", executor_node)
    g.add_node("validator", validator_node)
    g.add_node("notifier", notifier_node)

    # 入口
    g.set_entry_point("triage")

    # 主线
    g.add_edge("triage", "aggregator")
    g.add_edge("aggregator", "classifier")

    # 分流: critical/high → investigator, 其他 → notifier
    g.add_conditional_edges(
        "classifier",
        _route_by_severity,
        {"investigator": "investigator", "notifier": "notifier"},
    )

    # Investigator → Remediator → ApprovalGate
    g.add_edge("investigator", "remediator")
    g.add_edge("remediator", "approval_gate")

    # ApprovalGate 条件路由
    g.add_conditional_edges(
        "approval_gate",
        approval_gate_route,
        {
            "executor":     "executor",
            "human_review": "notifier",
            "skip":         "notifier",
            "reject":       "notifier",
        },
    )

    # Executor → Validator
    g.add_edge("executor", "validator")

    # v2.3: Validator 条件路由
    #   - failed + 未达上限 → 回 Investigator (闭环重诊)
    #   - 其他 → Notifier
    g.add_conditional_edges(
        "validator",
        _route_after_validator,
        {"investigator": "investigator", "notifier": "notifier"},
    )

    # Notifier → END
    g.add_edge("notifier", END)

    return g.compile()
