"""Classifier Agent: 给事件打标签 + 严重度"""
import json
import re
from langchain_openai import ChatOpenAI
from agents.state import AlertState

_llm = ChatOpenAI(
    model="qwen2.5-7b",
    base_url="http://localhost:8001/v1",
    api_key="dummy",
    temperature=0,
)

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


def classifier_node(state: AlertState) -> AlertState:
    summary = state.get("event_summary", "")
    resp = _llm.invoke(_PROMPT.format(summary=summary))
    parsed = _extract_json(resp.content)
    label = parsed.get("label", "infra")
    severity = parsed.get("severity", "medium")
    state["label"] = label
    state["severity"] = severity
    print(f"[Classifier] 分类: {label} | 严重度: {severity}")
    return state
