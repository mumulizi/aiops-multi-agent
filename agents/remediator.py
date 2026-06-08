"""Remediator Agent: 基于 Investigator 的 hypothesis 给出修复计划 (dry-run 决策).

核心职责: LLM 决定"该不该修 / 修什么 / 安全等级", 但绝不执行.
执行交给 Executor.
"""
import json
import re
import sys

from langchain_openai import ChatOpenAI
from agents.state import AlertState
from tools.langfuse_setup import LANGFUSE_HANDLER
from tools.remediation_actions import ALLOWED_ACTIONS

_callbacks = [LANGFUSE_HANDLER] if LANGFUSE_HANDLER else []

_llm = ChatOpenAI(
    model="qwen2.5-7b",
    base_url="http://localhost:8001/v1",
    api_key="dummy",
    temperature=0,
    max_tokens=512,
    callbacks=_callbacks,
)


# 给 LLM 看的工具说明
_L3_WHITELIST_DESC = """
L3 自动白名单 (低风险, 可自动执行):
- delete_evicted_pod: 删除 Evicted 状态的 Pod (清理类, 极安全)
- delete_completed_job_pod: 删除 Succeeded 完成态 Pod
- restart_pod: 删除 Pod 让控制器重建 (允许 ReplicaSet 和 DaemonSet 管理的 Pod;
  StatefulSet 因数据一致性走 L2)

L2 人审灰名单 (中风险, 需人工确认):
- scale_deployment: 调整 Deployment 副本数
- evict_pod: 强制驱逐异常 Pod
- cordon_node: 标记节点不可调度
- restart_statefulset_pod: 重启 StatefulSet Pod (有数据一致性风险)

L4 黑名单 (高危, 永远不自动执行):
- delete_pvc: 删除 PVC (会丢数据)
- drain_node: 驱逐节点上所有 Pod
- update_configmap: 修改 ConfigMap
- update_image: 改 Deployment image

action=none: 当前问题没有合适的安全操作, 需人工介入
"""

_SYSTEM_TPL = """你是 SRE 修复决策专家. 根据根因分析输出修复计划.

{whitelist_desc}

输出原则:
- 优先选 L3 (能自动修复就自动)
- L3 解决不了, 选 L2 (人审)
- 高风险操作走 L4 = 直接 reject
- 没有合适操作就 action=none

严格输出 JSON, 字段:
{{
  "action": "delete_evicted_pod|delete_completed_job_pod|restart_pod|scale_deployment|evict_pod|cordon_node|delete_pvc|drain_node|update_configmap|update_image|none",
  "target": "namespace/pod-name 格式 (action=none 时为空)",
  "safety_level": "L2|L3|L4",
  "rationale": "为什么选这个动作",
  "rollback": "如果出问题怎么回滚",
  "expected_outcome": "预期效果"
}}

只输出 JSON, 不要任何额外说明."""


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


def _build_plan_prompt(state):
    rca = state.get("rca_hypothesis", "(无)")
    summary = state.get("event_summary", "")
    raw_alerts = state.get("raw_alerts", [])
    # 取第一条告警的 namespace + pod 作为修复目标提示
    target_hint = ""
    if raw_alerts:
        labels = raw_alerts[0].get("labels", {})
        ns = labels.get("namespace", "")
        pod = labels.get("instance", "")
        if ns and pod:
            target_hint = f"{ns}/{pod}"
    return (
        f"事件摘要: {summary}\n\n"
        f"诊断结论: {rca}\n\n"
        f"建议 target (如适用): {target_hint}\n\n"
        f"请输出修复计划 JSON."
    )


def _validate_plan(plan: dict) -> tuple:
    """校验 LLM 输出的 plan 是否合法. 返回 (ok, reason)."""
    if not plan or not isinstance(plan, dict):
        return False, "plan is not a dict"
    action = plan.get("action")
    if not action:
        return False, "missing action"
    safety = plan.get("safety_level", "")

    # 一致性校验: action 必须在对应 safety_level 的白名单/灰名单/黑名单
    l3_actions = set(ALLOWED_ACTIONS.keys())
    l2_actions = {"scale_deployment", "evict_pod", "cordon_node"}
    l4_actions = {"delete_pvc", "drain_node", "update_configmap", "update_image"}

    if action == "none":
        # action=none 强制 safety=N/A, 避免 LLM 给出 L4 等不一致的标签
        plan["safety_level"] = "N/A"
        return True, "no action needed"
    if safety == "L3" and action not in l3_actions:
        return False, f"L3 但 action {action} 不在白名单"
    if safety == "L2" and action not in l2_actions:
        return False, f"L2 但 action {action} 不在灰名单"
    if safety == "L4":
        return True, "L4 will be rejected"  # L4 合法但会被拒绝
    if action in l3_actions and safety != "L3":
        return False, f"action {action} 是 L3 但 safety={safety}"
    return True, "ok"


def remediator_node(state: AlertState) -> AlertState:
    _log("[Remediator] 开始生成修复计划")

    # 没诊断结论就跳过
    rca = state.get("rca_hypothesis")
    if not rca or "诊断未完成" in rca or "无有效证据" in rca:
        plan = {
            "action": "none",
            "target": "",
            "safety_level": "N/A",
            "rationale": "诊断未完成或证据不足, 不生成修复计划",
            "rollback": "n/a",
            "expected_outcome": "n/a",
        }
        state["remediation_plan"] = plan
        _log("[Remediator] 诊断不完整, 跳过修复决策")
        return state

    sys_prompt = _SYSTEM_TPL.format(whitelist_desc=_L3_WHITELIST_DESC)
    user_msg = _build_plan_prompt(state)

    try:
        resp = _llm.invoke([
            ("system", sys_prompt),
            ("user", user_msg),
        ])
        plan = _extract_json(resp.content)
    except Exception as e:
        _log(f"[Remediator] LLM 调用失败: {e}")
        plan = None

    if plan is None:
        plan = {
            "action": "none",
            "target": "",
            "safety_level": "N/A",
            "rationale": "LLM 输出 JSON 解析失败, 降级为人审",
            "rollback": "n/a",
            "expected_outcome": "n/a",
        }
    else:
        # 校验
        ok, reason = _validate_plan(plan)
        if not ok:
            _log(f"[Remediator] 计划校验失败: {reason}, 降级为人审")
            plan = {
                "action": "none",
                "target": "",
                "safety_level": "N/A",
                "rationale": f"LLM 计划不合规 ({reason}), 降级人审",
                "rollback": "n/a",
                "expected_outcome": "n/a",
            }

    state["remediation_plan"] = plan
    _log(f"[Remediator] 修复计划: action={plan.get('action')} "
         f"safety={plan.get('safety_level')} target={plan.get('target')}")
    return state
