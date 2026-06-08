"""Inspector Agent: 三阶段巡检 (代码兜底, 不漏异常)"""
import json
import re
import sys
from langchain_openai import ChatOpenAI
from tools.k8s_tools import (
    list_unhealthy_pods,
    get_cluster_overview,
    describe_pod_real,
    list_high_restart_pods,
    collect_all_real_issues,
)
from tools.mock_tools import prometheus_query
from tools.langfuse_setup import LANGFUSE_HANDLER, TraceTimer, trace_span

_callbacks = [LANGFUSE_HANDLER] if LANGFUSE_HANDLER else []

_llm = ChatOpenAI(
    model="qwen2.5-7b",
    base_url="http://localhost:8001/v1",
    api_key="dummy",
    temperature=0.1,
    max_tokens=512,
    callbacks=_callbacks,
)

DEEP_TOOLS = {
    "describe_pod_real": describe_pod_real,
    "prometheus_query": prometheus_query,
}

DEEP_TOOL_DESCS = {
    "describe_pod_real": "参数 name(str), namespace(str). 看 Pod 详情和事件.",
    "prometheus_query": "参数 query(str). 查 PromQL 指标.",
}


def _log(msg):
    print(msg, flush=True)
    sys.stdout.flush()


def _extract_json(text):
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def _classify_severity(restarts, phase):
    if phase in ("Failed", "Unknown"):
        return "critical"
    if restarts >= 1000:
        return "critical"
    if restarts >= 100:
        return "high"
    if restarts >= 10:
        return "medium"
    return "low"


def _classify_type(reason):
    r = (reason or "").lower()
    if "crashloop" in r:
        return "CrashLoopBackOff"
    if "imagepull" in r or "errimage" in r or "imagebackoff" in r:
        return "ImagePullError"
    if "oom" in r:
        return "OOMKilled"
    if "pending" in r:
        return "Pending"
    if "evicted" in r:
        return "Evicted"
    return "Unhealthy"


def _build_issues_from_real_data(top_n):
    """阶段 3 核心: K8s API 真实数据 -> issues, 不依赖 LLM"""
    real_pods = collect_all_real_issues()
    if top_n is not None:
        real_pods = real_pods[:top_n]
    issues = []
    for p in real_pods:
        ns = p["namespace"]
        name = p["pod"]
        phase = p["phase"]
        restarts = p["restarts"]
        reason = p["reason"]
        sev = _classify_severity(restarts, phase)
        typ = _classify_type(reason)
        summary = f"{ns}/{name} {typ} restarts={restarts}"
        issues.append({
            "namespace": ns,
            "pod": name,
            "type": typ,
            "severity": sev,
            "summary": summary,
            "restarts": restarts,
            "phase": phase,
            "reason": reason,
        })
    return issues


def _react_deep_dive(top_pods, max_steps=4):
    """阶段 2: LLM 自主决定深入查 Top 几个"""
    short_list = []
    for p in top_pods[:5]:
        short_list.append({
            "ns": p["namespace"],
            "pod": p["pod"],
            "type": p["type"],
            "restarts": p["restarts"],
        })
    sys_prompt = ("你是 K8s 巡检员. 已发现以下异常 Pod (按严重度排序):\n"
                  + json.dumps(short_list, ensure_ascii=False, indent=2)
                  + "\n\n可用深入工具:\n"
                  + "\n".join([f"- {n}: {d}" for n, d in DEEP_TOOL_DESCS.items()])
                  + "\n\n每轮严格输出 JSON 二选一:\n"
                  + "1. {\"action\":\"use_tool\",\"tool\":\"x\",\"args\":{},\"thought\":\"...\"}\n"
                  + "2. {\"action\":\"final\",\"detailed_findings\":[{\"pod\":\"x\",\"finding\":\"...\"}],\"summary\":\"...\"}\n\n"
                  + "强制规则:\n"
                  + "- 只输出 JSON, 没有任何说明文字\n"
                  + "- 调用 1-3 次工具就 final\n"
                  + "- 优先深入 critical 严重度的 Pod\n"
                  + "- detailed_findings 中 pod 要和上面列表的 pod 名一致")

    history = [
        ("system", sys_prompt),
        ("user", "开始深入调查, 第一步."),
    ]
    detailed = []
    raw_calls = []

    for step in range(max_steps):
        _log(f"  [deep] step {step}: 思考中...")
        try:
            resp = _llm.invoke(history)
        except Exception as e:
            _log(f"  [deep] step {step}: LLM 调用失败 {e}")
            break
        decision = _extract_json(resp.content)
        if not decision:
            _log(f"  [deep] step {step}: JSON 解析失败")
            break
        action = decision.get("action")
        if action == "use_tool":
            tool_name = decision.get("tool")
            args = decision.get("args", {})
            thought = decision.get("thought", "")
            _log(f"  [deep] step {step}: {tool_name}({args}) - {thought[:60]}")
            fn = DEEP_TOOLS.get(tool_name)
            if not fn:
                tool_result = f"工具 {tool_name} 不存在"
            else:
                with TraceTimer(
                    agent="inspector",
                    name=f"tool:{tool_name}",
                    input_data={"args": args, "thought": thought},
                ) as t:
                    try:
                        tool_result = fn(**args)
                    except Exception as e:
                        tool_result = f"调用失败: {e}"
                    t.set_output({"result_preview": str(tool_result)[:300]})
            tr = str(tool_result)[:600]
            _log(f"  [deep]   结果(前 200): {tr[:200]}")
            raw_calls.append({"tool": tool_name, "args": args, "result": tr})
            history.append(("assistant", resp.content))
            history.append(("user", f"工具返回: {tr}\n\n请输出下一步."))
        elif action == "final":
            detailed = decision.get("detailed_findings", [])
            sm = decision.get("summary", "")
            _log(f"  [deep] LLM 主动 final, 详细发现 {len(detailed)} 条")
            return {"detailed_findings": detailed, "summary": sm, "raw_calls": raw_calls}
        else:
            break

    _log(f"  [deep] 步数上限, 已收集 {len(raw_calls)} 条原始证据 (不丢失)")
    return {"detailed_findings": [], "summary": "深入调查未完成但已记录原始调用", "raw_calls": raw_calls}


def _llm_overview(issues, deep_result):
    """阶段 4: LLM 写一句中文 overview (失败也不影响结果)"""
    if not issues:
        return "集群当前无异常."
    top3 = []
    for i in issues[:3]:
        top3.append({"pod": i["pod"], "type": i["type"], "restarts": i["restarts"], "severity": i["severity"]})
    summary_input = {
        "total_issues": len(issues),
        "top_3": top3,
        "deep_summary": deep_result.get("summary", ""),
    }
    prompt = ("以下是 K8s 集群巡检结果, 请用 1-2 句中文总结整体情况:\n"
              + json.dumps(summary_input, ensure_ascii=False, indent=2)
              + "\n\n只输出总结文本, 不要 JSON, 不要解释.")
    try:
        resp = _llm.invoke([("user", prompt)])
        return resp.content.strip()[:300]
    except Exception as e:
        return f"集群发现 {len(issues)} 个异常 (LLM 摘要失败)"


def run_inspector(top_n=None, deep_max_steps=4):
    """三阶段巡检. top_n=None 表示返回所有真实异常"""
    _log("=" * 60)
    _log("[Inspector] 启动主动巡检 (3 阶段, 代码兜底)")
    _log("=" * 60)

    # 阶段 1: 强制收集真实数据
    _log("[Inspector] 阶段 1: 代码强制收集真实数据")
    with TraceTimer("inspector", "phase1:cluster_overview") as t:
        overview = get_cluster_overview()
        t.set_output({"overview_preview": overview[:300]})
    _log(overview)
    _log("")

    with TraceTimer("inspector", "phase1:collect_unhealthy_pods") as t:
        issues = _build_issues_from_real_data(top_n=top_n)
        t.set_output({"total_issues": len(issues)})

    _log(f"[Inspector] 阶段 1 完成: K8s API 收集到 {len(issues)} 个真实异常 Pod")
    if not issues:
        _log("[Inspector] 集群无异常, 退出")
        return []
    _log("")

    # 阶段 2: LLM 深入调查 Top 5
    _log("[Inspector] 阶段 2: LLM 深入调查 Top 5")
    with TraceTimer("inspector", "phase2:deep_dive",
                    input_data={"top_pods": [p["pod"] for p in issues[:5]]}) as t:
        deep_result = _react_deep_dive(issues[:5], max_steps=deep_max_steps)
        t.set_output({
            "detailed_findings_count": len(deep_result.get("detailed_findings", [])),
            "raw_calls_count": len(deep_result.get("raw_calls", [])),
        })
    _log("")

    # 阶段 3: 合并 deep finding 到 issues
    _log("[Inspector] 阶段 3: 合并深入发现")
    deep_findings = deep_result.get("detailed_findings", [])
    pod_to_finding = {}
    for df in deep_findings:
        pod_key = df.get("pod", "")
        if pod_key:
            pod_to_finding[pod_key] = df.get("finding", "")
    enriched_count = 0
    for i in issues:
        pkey = i["pod"]
        if pkey in pod_to_finding:
            i["deep_finding"] = pod_to_finding[pkey]
            enriched_count += 1
    _log(f"[Inspector] 阶段 3 完成: {enriched_count} 个异常补充了深入发现")
    trace_span("phase3:merge_findings", "inspector",
               input_data={"deep_findings_count": len(deep_findings)},
               output_data={"enriched_count": enriched_count})
    _log("")

    # 阶段 4: LLM 写 overview
    _log("[Inspector] 阶段 4: 生成整体摘要")
    with TraceTimer("inspector", "phase4:overview_summary") as t:
        overview_text = _llm_overview(issues, deep_result)
        t.set_output({"overview": overview_text[:300]})
    _log(f"  Overview: {overview_text}")
    _log("")

    # 输出
    _log("=" * 60)
    _log(f"[Inspector] 巡检完成, 共 {len(issues)} 个真实异常 (无遗漏)")
    _log("=" * 60)
    sev_count = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for i in issues:
        sev_count[i["severity"]] = sev_count.get(i["severity"], 0) + 1
    _log(f"  严重度分布: {sev_count}")
    _log("")
    show_n = min(10, len(issues))
    _log(f"  Top {show_n}:")
    for idx in range(show_n):
        isu = issues[idx]
        sev = isu["severity"]
        sm = isu["summary"]
        _log(f"    {idx+1}. [{sev}] {sm}")
        if isu.get("deep_finding"):
            deep = (isu.get("deep_finding") or "")[:120]
            _log(f"       深入: {deep}")

    return issues
