"""LangGraph 编排器: Triage -> Aggregator -> Classifier -> [Investigator?] -> Notifier"""
from langgraph.graph import StateGraph, END
from agents.state import AlertState
from agents.triage import triage_node
from agents.aggregator import aggregator_node
from agents.classifier import classifier_node
from agents.investigator import investigator_node
from agents.notifier import notifier_node


def _route_by_severity(state: AlertState) -> str:
    sev = state.get("severity", "medium")
    if sev in ("critical", "high"):
        return "investigator"
    return "notifier"


def build_graph():
    g = StateGraph(AlertState)
    g.add_node("triage", triage_node)
    g.add_node("aggregator", aggregator_node)
    g.add_node("classifier", classifier_node)
    g.add_node("investigator", investigator_node)
    g.add_node("notifier", notifier_node)

    g.set_entry_point("triage")
    g.add_edge("triage", "aggregator")
    g.add_edge("aggregator", "classifier")
    g.add_conditional_edges(
        "classifier",
        _route_by_severity,
        {"investigator": "investigator", "notifier": "notifier"},
    )
    g.add_edge("investigator", "notifier")
    g.add_edge("notifier", END)

    return g.compile()
