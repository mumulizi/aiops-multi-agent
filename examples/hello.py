from typing import TypedDict
from langgraph.graph import StateGraph, END

class State(TypedDict):
    message: str
    count: int

def node_a(state: State) -> State:
    print(f"[A] 收到: {state['message']}")
    state["count"] = state.get("count", 0) + 1
    return state

def node_b(state: State) -> State:
    print(f"[B] 处理后: count={state['count']}")
    state["message"] = f"{state['message']} → 已处理"
    return state

def should_continue(state: State) -> str:
    return "node_b" if state["count"] < 1 else END

graph = StateGraph(State)
graph.add_node("node_a", node_a)
graph.add_node("node_b", node_b)
graph.set_entry_point("node_a")
graph.add_conditional_edges("node_a", should_continue, {"node_b": "node_b", END: END})
graph.add_edge("node_b", END)
app = graph.compile()

if __name__ == "__main__":
    result = app.invoke({"message": "test alert", "count": 0})
    print("最终状态:", result)
