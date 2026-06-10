"""Investigator Agent: ReAct + 代码兜底"""
import json
import re
import sys
from langchain_openai import ChatOpenAI
from agents.state import AlertState
from tools.mock_tools import TOOLS, TOOL_DESCRIPTIONS
from tools.langfuse_setup import LANGFUSE_HANDLER, TraceTimer

_callbacks = [LANGFUSE_HANDLER] if LANGFUSE_HANDLER else []

_llm = ChatOpenAI(
    model="qwen2.5-7b",
    base_url="http://localhost:8001/v1",
    api_key="dummy",
    temperature=0,
    max_tokens=512,
    callbacks=_callbacks,
)

_SYSTEM_TPL = """你是资深 SRE, 通过工具定位告警根因.

排障原则 (按异常类型决定第一步, 灵活选择):
- **CrashLoopBackOff / Error exit**: 第一步通常调 get_pod_logs 看容器日志,
  日志为空时**必须**调 kubectl_describe 看 last_terminated.message 和 events
- **ImagePullBackOff / ErrImagePull**: 第一步**直接调 kubectl_describe**
  (这种问题日志为空, container_statuses[].waiting.message 才有 "rpc error: ...
  Failed to pull image" 等关键信息)
- **Pending / 调度失败**: 第一步**直接调 kubectl_describe**
  (events / conditions.message 显示 "0/8 nodes available" 等调度原因, 日志根本没用)
- **OOMKilled**: 第一步调 get_pod_logs 看崩溃前日志, 第二步 kubectl_describe
  看 last_terminated.message 和 limits

kubectl_describe 关键阅读顺序 (有些 Pod 没日志, 全靠这里):
1. **container_statuses[].waiting.message** — ImagePullBackOff 的真实错误在这
   (如 "rpc error: code = NotFound desc = failed to pull")
2. **container_statuses[].last_terminated.message** — 崩溃前最后的输出
3. **conditions[].message** — Pending 调度失败原因
4. **pod.status.message** + **pod.status.reason** — Pod 整体状态
5. **events[].message** — K8s 控制器事件

工具回退链 (重要):
- get_pod_logs 返回为空 / 报错 → **必须**调 kubectl_describe
- kubectl_describe 也没线索 → prometheus_query 看历史趋势
- 不要因为日志为空就放弃, 一定换工具继续查

日志/message 关键词 (任意一个找到就立即 final):
- connection refused / no such file / permission denied
- panic / fatal / OOMKilled / SIGKILL / SIGTERM
- flag provided but not defined / unknown command
- ImagePullBackOff / Failed to pull image / unauthorized / not found
- exec format error / no space left
- rpc error / context deadline exceeded

判断原则:
- 找到关键证据后立即 final, 不要继续无意义的指标查询
- alert description 中可能已包含 Inspector 收集的细节(容器状态/退出码), 优先利用
- 节点内存判断: 可用 >10GB 算充足, <2GB 才算紧张
- 不要引用查询为空的指标作为根因证据

可用工具:
{tools}

每轮严格输出 JSON 二选一:
1. {{"action":"use_tool","tool":"x","args":{{}},"thought":"..."}}
2. {{"action":"final","hypothesis":"...","confidence":"高/中/低","key_evidence":["..."]}}

规则: 只输出 JSON; 拿到关键证据后立即 final; 不要循环."""


def _log(msg):
    print(msg, flush=True)


def _extract_json(text):
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def _format_tools():
    lines = []
    for n, d in TOOL_DESCRIPTIONS.items():
        lines.append(f"- {n}: {d}")
    return "\n".join(lines)


def _build_fallback_hypothesis(evidence, summary):
    """代码兜底"""
    if not evidence:
        sm = summary[:80]
        return f"诊断未完成: 无有效证据 (基于摘要 {sm} 需人工介入)"
    findings = []
    for ev in evidence[:3]:
        tool = ev.get("tool", "?")
        result = str(ev.get("result", ""))[:200]
        findings.append(f"[{tool}] {result}")
    joined = " | ".join(findings)
    n = len(evidence)
    return f"诊断未自主完成 (LLM 未在限定步数内 final), 已收集 {n} 条证据: {joined}"


def investigator_node(state: AlertState) -> AlertState:
    summary = state.get("event_summary", "")
    alerts = state.get("raw_alerts", [])

    _log("[Investigator] 开始诊断 (ReAct + 代码兜底)")

    sys_prompt = _SYSTEM_TPL.format(tools=_format_tools())
    user_msg = f"事件摘要: {summary}\n\n告警明细: {alerts}\n\n请输出第一步."
    history = [
        ("system", sys_prompt),
        ("user", user_msg),
    ]

    evidence = []
    final_result = None

    for step in range(4):
        try:
            resp = _llm.invoke(history)
        except Exception as e:
            err = str(e)
            _log(f"[Investigator] step {step}: LLM 调用失败 {err}")
            break
        text = resp.content
        decision = _extract_json(text)
        if not decision:
            _log(f"[Investigator] step {step}: JSON 解析失败")
            break
        action = decision.get("action")

        if action == "use_tool":
            tool_name = decision.get("tool")
            args = decision.get("args", {})
            thought = decision.get("thought", "")
            short_thought = thought[:60]
            _log(f"[Investigator]  step {step}: 调用 {tool_name}({args}) - {short_thought}")
            fn = TOOLS.get(tool_name)
            if not fn:
                tool_result = f"工具 {tool_name} 不存在"
            else:
                # 用 TraceTimer 把工具调用打到 Langfuse
                with TraceTimer(
                    agent="investigator",
                    name=f"tool:{tool_name}",
                    input_data={"args": args, "thought": thought},
                ) as t:
                    try:
                        tool_result = fn(**args)
                    except Exception as e:
                        err = str(e)
                        tool_result = f"调用失败: {err}"
                    t.set_output({"result_preview": str(tool_result)[:300]})
            preview = str(tool_result)[:120]
            _log(f"[Investigator]  step {step}: 结果 = {preview}...")
            # 日志可能很长, 截断到 1500 字给下一轮
            evidence.append({"tool": tool_name, "args": args, "result": str(tool_result)[:500]})
            tool_result_for_llm = str(tool_result)[:1500]
            history.append(("assistant", text))
            history.append(("user", f"工具 {tool_name} 返回: {tool_result_for_llm}\n\n请输出下一步."))

        elif action == "final":
            final_result = decision
            break
        else:
            _log(f"[Investigator] step {step}: 未知 action {action}")
            break

    if final_result:
        hypothesis = final_result.get("hypothesis", "(无)")
        confidence = final_result.get("confidence", "?")
        key_evidence = final_result.get("key_evidence", [])
        joined = "; ".join(key_evidence) if key_evidence else "无"
        rca_text = f"{hypothesis} (置信度: {confidence}; 关键证据: {joined})"
    else:
        rca_text = _build_fallback_hypothesis(evidence, summary)

    state["rca_hypothesis"] = rca_text
    state["evidence"] = evidence
    n = len(evidence)
    short_rca = rca_text[:200]
    _log(f"[Investigator] 共调用 {n} 次工具")
    _log(f"[Investigator] 结论: {short_rca}")
    return state
