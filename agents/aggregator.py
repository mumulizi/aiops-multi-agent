"""Aggregator Agent: 把多条告警聚合成一句话事件描述"""
import json
from langchain_openai import ChatOpenAI
from agents.state import AlertState

_llm = ChatOpenAI(
    model="qwen2.5-7b",
    base_url="http://localhost:8001/v1",
    api_key="dummy",
    temperature=0,
)


def aggregator_node(state: AlertState) -> AlertState:
    alerts = state.get("raw_alerts", [])
    alerts_json = json.dumps(alerts, ensure_ascii=False, indent=2)
    prompt = f"""你是 SRE 助手. 下面是一批告警, 请用一句中文总结这批告警代表什么事件(突出关键服务/节点/根本现象).

告警列表:
{alerts_json}

要求: 不要解释, 直接给出 1 句话事件摘要."""
    resp = _llm.invoke(prompt)
    summary = resp.content.strip()
    state["event_summary"] = summary
    print(f"[Aggregator] 事件摘要: {summary}")
    return state
