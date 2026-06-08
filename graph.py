"""LangGraph 编排器: v2.0 完整自愈闭环.

流水线 (按 severity 分流):

低优先级:
  Triage → Aggregator → Classifier → Notifier

critical/high:
  Triage → Aggregator → Classifier → Investigator → Remediator → ApprovalGate
                                                                      │
                       ┌──────────────────────────┬──────────────────┴──────────────┐
                       │ executor                 │ human_review / skip / reject     │
                       ▼                          ▼                                  │
                    Executor → Validator → ──────────────────────────────────────► Notifier
"""
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


def _route_by_severity(state: AlertState) -> str:
    sev = state.get("severity", "medium")
    if sev in ("critical", "high"):
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

    # Executor → Validator → Notifier
    g.add_edge("executor", "validator")
    g.add_edge("validator", "notifier")

    # Notifier → END
    g.add_edge("notifier", END)

    return g.compile()
