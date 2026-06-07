from typing import TypedDict
from langgraph.graph import StateGraph, END
from langchain_openai import ChatOpenAI

llm = ChatOpenAI(
    model="qwen2.5-7b",
    base_url="http://localhost:8001/v1",
    api_key="dummy",
    temperature=0,
)

class State(TypedDict):
    alert: str
    summary: str

def summarize(state: State) -> State:
    alert = state["alert"]
    prompt = f"用一句中文总结这个告警(直接给摘要,不要解释): {alert}"
    resp = llm.invoke(prompt)
    state["summary"] = resp.content
    return state

graph = StateGraph(State)
graph.add_node("summarize", summarize)
graph.set_entry_point("summarize")
graph.add_edge("summarize", END)
app = graph.compile()

if __name__ == "__main__":
    result = app.invoke({
        "alert": "Pod nginx-7d8 在 default 命名空间 OOMKilled,内存上限 512Mi,实际使用 530Mi",
        "summary": "",
    })
    print("\n=== LLM 摘要 ===")
    print(result["summary"])
