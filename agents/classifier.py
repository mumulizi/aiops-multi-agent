"""Classifier Agent: 给事件打标签 + 严重度.

v2.12+ fix: source=metrics 的 issue 走 fast-path, 不调 LLM.
- MetricsInspector 已经基于 PromQL 阈值给出了权威 severity (代码规则, 不是 LLM 推断)
- 让 LLM 看一句话摘要重判, 经常会把 high 改成 medium → 流水线把它路由到 Notifier 跳过 Investigator
- 这种降级是错误的: 阈值规则是真实超阈, 不该被 LLM 否决
"""
import json
import re
from agents.state import AlertState
from tools.llm_factory import build_llm

_llm = build_llm("classifier", temperature=0)

_PROMPT = """你是 SRE 分诊助手. 请对以下事件分类.

事件: {summary}

输出严格的 JSON, 不要任何额外解释, 字段如下:
- label: infra(基础设施) / app(应用层) / business(业务)
- severity: critical / high / medium / low

示例输出:
{{"label": "infra", "severity": "critical"}}"""


def _extract_json(text: str) -> dict:
    m = re.search(r"\{[^{}]*\}", text, re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {}


def _is_metrics_issue(state: AlertState) -> tuple:
    """判断这个 state 是不是来自 MetricsInspector. 返回 (is_metric, original_severity).

    Triage 节点会把 labels 透传到 raw_alerts[0].labels, 我们看 source 字段.
    """
    alerts = state.get("raw_alerts") or []
    if not alerts:
        return False, ""
    labels = alerts[0].get("labels") or {}
    src = labels.get("source", "")
    orig = labels.get("original_severity", "")
    return src == "metrics", orig


def classifier_node(state: AlertState) -> AlertState:
    summary = state.get("event_summary", "")

    # v2.12+ fix: metric 来源走 fast-path
    is_metric, orig_sev = _is_metrics_issue(state)
    if is_metric and orig_sev in ("critical", "high", "medium", "low"):
        state["label"] = "infra"   # 指标层默认 infra
        state["severity"] = orig_sev
        print(f"[Classifier] (metrics fast-path) 分类: infra | 严重度: {orig_sev} "
              f"(复用 MetricsInspector 阈值规则结果, 跳过 LLM)")
        return state

    # Pod 来源走原 LLM 路径
    resp = _llm.invoke(_PROMPT.format(summary=summary))
    parsed = _extract_json(resp.content)
    label = parsed.get("label", "infra")
    severity = parsed.get("severity", "medium")
    state["label"] = label
    state["severity"] = severity
    print(f"[Classifier] 分类: {label} | 严重度: {severity}")
    return state
