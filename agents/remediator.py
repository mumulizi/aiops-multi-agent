"""Remediator Agent: 基于 Investigator 的 hypothesis 给出修复计划 (dry-run 决策).

核心职责: LLM 决定"该不该修 / 修什么 / 安全等级", 但绝不执行.
执行交给 Executor.
"""
import json
import re
import sys

from agents.state import AlertState
from tools.remediation_actions import ALLOWED_ACTIONS
from tools.llm_factory import build_llm

# v2.8: max_tokens 升到 1024 避免 plan + rationale + rollback 加起来超长被截
_llm = build_llm("remediator", temperature=0, max_tokens=1024)


# 给 LLM 看的工具说明
_L3_WHITELIST_DESC = """
L3 自动白名单 (低风险, 可自动执行):
- delete_evicted_pod: 删除 Evicted 状态的 Pod (清理类, 极安全)
- delete_completed_job_pod: 删除 Succeeded 完成态 Pod
- delete_failed_pod: 删除 Failed phase 的 Pod (含 Evicted 之外的失败情况)
- restart_pod: 删除 Pod 让控制器重建 (允许 ReplicaSet 和 DaemonSet 管理的 Pod;
  StatefulSet 因数据一致性走 L2). 适用 CrashLoopBackOff / OOMKilled / 临时性故障.
- restart_pod_for_image_pull: 重启 ImagePullBackOff/ErrImagePull 状态的 Pod
  让控制器重新拉镜像 (允许 RS/DaemonSet). 注: 仅治标, 镜像名错/认证失效需改 Deployment.
- cordon_node: 标节点不可调度 (kubectl cordon). target 是 node 名 (无 namespace).
  适用: 节点频繁 NotReady / 磁盘满 / 准备维护. 完全可逆 (uncordon_node 恢复).
  影响面仅"未来调度", 不动存量 Pod.
- uncordon_node: cordon_node 的反操作.

L2 人审灰名单 (中风险, 需人工 CLI 审批后才执行):
- restart_statefulset_pod: 重启 StatefulSet 管理的 Pod (与 restart_pod 互斥, 仅限 StatefulSet)
  适用场景: StatefulSet Pod 处于 CrashLoopBackOff / OOMKilled 等
  风险: 单副本+PV 短暂不可用, 主从架构可能触发主从切换
- scale_deployment: 调 Deployment 副本数. target="namespace/deployment-name".
  必须额外指定 replicas=N (绝对值) 或 delta=±N (相对增减).
  安全边界: |delta|<=5, 最终 replicas<=50.
  适用: 副本不足扩容 / 抗压临时扩 / 缩到 0 暂停服务.
- rollback_deployment: 回滚 Deployment 到上一个 ReplicaSet (kubectl rollout undo).
  target="namespace/deployment-name". 适用: 新版本上线崩了.

L4 黑名单 (高危, 永远不自动执行):
- delete_pvc: 删除 PVC (会丢数据)
- drain_node: 驱逐节点上所有 Pod
- update_configmap: 修改 ConfigMap
- update_image: 改 Deployment image

action=none: 当前问题没有合适的安全操作, 需人工介入

⚠️ Owner 类型决策铁律 (优先于其他规则):
- BarePod (无 owner_references) → action=none (删了不会被任何控制器重建, 修了反而出事故)
- Job → action=none (Job 状态机不应被外部干预)
- StatefulSet → 只能用 restart_statefulset_pod (L2), 绝不用 restart_pod
- ReplicaSet (Deployment) → 用 restart_pod (L3)
- DaemonSet → 用 restart_pod (L3)

⚠️ "重启无救" 黑名单 (R3 强制规则, 命中即 action=none):
- 启动参数错 (flag provided but not defined / executable not found)
- 配置文件不存在 (no such file / stat ... config.yaml)
- 镜像名错 (InvalidImageName / ErrImagePull / ImagePullBackOff)
- ConfigMap/Secret 引用错 (CreateContainerConfigError)
- 容器配置错 (RunContainerError / CreateContainerError)
这些故障重启 1000 次都救不了, 必须人工修 Deployment/ConfigMap, 不能开 restart 假修复.

异常类型 + Owner 类型 → 推荐 action 速查 (重要):
- CrashLoopBackOff (RS/DS owner)         → restart_pod                   [L3]
- CrashLoopBackOff (StatefulSet owner)   → restart_statefulset_pod       [L2]
- CrashLoopBackOff (BarePod / Job)       → action=none
- ImagePullBackOff / ErrImagePull (任意) → action=none (重启无救, 走 R3)
- OOMKilled (RS/DS)                       → restart_pod                  [L3]
- OOMKilled (StatefulSet)                 → restart_statefulset_pod     [L2]
- Evicted (节点压力驱逐)                  → delete_evicted_pod          [L3]
- Failed (非 Evicted)                     → delete_failed_pod           [L3]
- Pending (调度失败/资源不足/PVC pending)  → action=none
- 启动参数错 / 配置错 / 依赖服务 down    → action=none (走 R3)
- 节点 NotReady / 频繁失联                → cordon_node                  [L3]
- Deployment 副本不足 / 服务抗压           → scale_deployment delta=+N   [L2]
- 新版本上线崩了 / 回归 bug                → rollback_deployment          [L2]

⚠️ target 字段铁律:
- target 必须是完整的 "namespace/pod-full-name" 格式 (Pod 操作)
- 例外: cordon_node / uncordon_node 的 target 是 node 名 (无 namespace, 例: "192.168.48.78")
- 例外: scale_deployment / rollback_deployment 的 target 是 "namespace/deployment-name"
       (deployment 名, 不是 pod 名)
- pod-full-name 必须包含完整的 hash 后缀 (例: abc-bcd-efg-75c547c587-xfhmd)
- 严禁简化为 service 名 (例: 错误 "abc-system/abc-bcd-efg")
- 直接使用 user 输入中提供的精确 target, 不要任何修改
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
  "action": "delete_evicted_pod|delete_completed_job_pod|restart_pod|restart_pod_for_image_pull|delete_failed_pod|cordon_node|uncordon_node|restart_statefulset_pod|scale_deployment|rollback_deployment|delete_pvc|drain_node|update_configmap|update_image|none",
  "target": "namespace/pod-name 或 namespace/deployment-name 或 node 名 (action=none 时为空)",
  "safety_level": "L2|L3|L4",
  "rationale": "为什么选这个动作",
  "rollback": "如果出问题怎么回滚",
  "expected_outcome": "预期效果",
  "extra": {{}}  // scale_deployment 必须含 {{\"delta\": ±N}} 或 {{\"replicas\": N}}
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
    # 取第一条告警的 namespace + pod + owner_kind 作为修复目标提示
    target_hint = ""
    owner_hint = ""
    if raw_alerts:
        labels = raw_alerts[0].get("labels", {})
        ns = labels.get("namespace", "")
        pod = labels.get("instance", "")
        owner = labels.get("owner_kind", "")
        if ns and pod:
            target_hint = f"{ns}/{pod}"
        if owner:
            owner_hint = owner
    prompt_parts = [f"事件摘要: {summary}", f"诊断结论: {rca}"]
    if target_hint:
        prompt_parts.append(
            f"!! 必须使用的精确 target (含完整 Pod 名, 不要简化): {target_hint}"
        )
    if owner_hint:
        prompt_parts.append(
            f"!! Pod 的 Owner 类型: {owner_hint} "
            f"(BarePod=裸 Pod 没有控制器→必须 action=none; "
            f"StatefulSet→restart_statefulset_pod L2; "
            f"ReplicaSet/DaemonSet→restart_pod L3 或 restart_pod_for_image_pull L3)"
        )
    prompt_parts.append("请输出修复计划 JSON.")
    return "\n\n".join(prompt_parts)


def _post_process_plan(plan: dict, raw_alerts: list, rca: str = "") -> dict:
    """对 LLM 输出做清洗 + 强制规则覆盖.

    清洗:
    1. 清掉 'action=' 前缀 (LLM 偶尔输出 "action=none" 字符串当 action 值)
    2. target 必须等于 raw_alerts 给的真实 namespace/pod, 否则强制改回

    强制规则 (代码优先于 LLM):
    R1. BarePod / Job (无控制器) → 强制 action=none
        理由: 删了控制器不会重建, 反而出事故
    R2. StatefulSet + CrashLoopBackOff/OOMKilled → 强制 restart_statefulset_pod L2
        理由: LLM 经常被 RCA 带偏给 none, 但 StatefulSet 重启是合理的人审场景
    R3. RCA / alertname 命中"重启无救"黑名单 → 强制 action=none, 标 escalate_human
        理由: 启动参数错/配置文件不存在/镜像名错 这类故障重启 1000 次都救不了,
              不应该让 restart_pod 假修复, 也不该浪费一次 L2 人审, 直接给人.
    """
    if not isinstance(plan, dict):
        return plan
    # 1. 清 action 前缀
    action = plan.get("action", "")
    if isinstance(action, str) and action.startswith("action="):
        plan["action"] = action.split("=", 1)[1].strip() or "none"

    # 2. 提取真实 namespace/pod/owner/alertname (从 raw_alerts.labels)
    if not raw_alerts:
        return plan
    first_alert = raw_alerts[0] if isinstance(raw_alerts[0], dict) else {}
    labels = first_alert.get("labels") or {}
    real_ns = labels.get("namespace", "")
    real_pod = labels.get("instance", "")
    owner = labels.get("owner_kind", "")
    alert_type = labels.get("alertname", "")  # CrashLoopBackOff/Unhealthy/OOMKilled/...
    expected = f"{real_ns}/{real_pod}" if real_ns and real_pod else ""

    # 3. target 校正 (LLM 偷懒 / 拼写错都拦下)
    if expected and plan.get("action") not in ("none", "", None):
        current = plan.get("target", "")
        if current != expected:
            plan["target"] = expected
            plan["_target_corrected"] = True

    # 4. 强制规则 R1: BarePod / Job → action=none
    if owner in ("BarePod", "Job"):
        if plan.get("action") != "none":
            plan["action"] = "none"
            plan["safety_level"] = "N/A"
            plan["target"] = ""
            plan["_overridden"] = f"R1: {owner} (无控制器) 强制 action=none"
        return plan

    # 5. 强制规则 R2: StatefulSet + CrashLoopBackOff/OOMKilled → restart_statefulset_pod L2
    sts_recoverable_types = {"CrashLoopBackOff", "OOMKilled"}
    if owner == "StatefulSet" and alert_type in sts_recoverable_types:
        cur_action = plan.get("action", "")
        if cur_action != "restart_statefulset_pod" and expected:
            old_action = cur_action
            plan["action"] = "restart_statefulset_pod"
            plan["safety_level"] = "L2"
            plan["target"] = expected
            plan["_overridden"] = (
                f"R2: StatefulSet+{alert_type} 强制 restart_statefulset_pod L2 "
                f"(原 LLM 输出: {old_action})"
            )
            if not plan.get("rationale"):
                plan["rationale"] = "StatefulSet Pod 重启 (代码强制规则)"
            if not plan.get("rollback") or plan.get("rollback") in ("n/a", "N/A"):
                plan["rollback"] = "如重启后不恢复, 检查 PV / 主从状态"

    # 6. 强制规则 R3: "重启无救" 型故障 → 强制 action=none, 标 escalate_human
    # 启动参数错 / 配置文件不存在 / 镜像名错 / ConfigMap 引用错 这些重启 N 次都救不了.
    # 必须在 Remediator 阶段拦下, 不让走 restart 路径, 也不让进 Approval Gate.
    rca_text = (rca or "").lower()
    if _is_non_restartable_failure(rca_text, alert_type):
        cur_action = plan.get("action", "")
        if cur_action in ("restart_pod", "restart_pod_for_image_pull",
                          "restart_statefulset_pod"):
            old_action = cur_action
            plan["action"] = "none"
            plan["safety_level"] = "N/A"
            plan["target"] = ""
            plan["_overridden"] = (
                f"R3: 重启无救型故障 (RCA/alert 命中黑名单), 强制 action=none "
                f"(原 LLM 输出: {old_action})"
            )
            plan["rationale"] = (
                "根因在配置/镜像/启动参数, 重启不能解决问题. "
                "需人工修复 Deployment/ConfigMap/启动命令."
            )
            plan["rollback"] = "无需回滚 (未执行任何操作)"
            plan["escalate_human"] = True  # 让 Notifier 用 🚨 推 IM

    return plan


# R3 命中的根因关键词集合 (RCA 文本里出现这些 → 重启无救)
# 跟 tools/validator.py 的 _NON_RESTARTABLE_REASONS 是镜像关系,
# 但这里匹配自由文本里的关键词, 那边匹配 K8s waiting.reason 枚举.
_R3_RCA_HINTS = (
    "no such file or directory",
    "no such file",
    "stat ",                          # "stat /path/...: no such file"
    "flag provided but not defined",
    "exec format error",
    "exec: ",                          # "exec: --config=...": stat ...
    "executable file not found",
    "permission denied",               # 配置/挂载权限错
    "invalid argument",
    "configmap ",                      # ConfigMap 引用失败
    "secret ",                         # Secret 引用失败
    "errimagepull",
    "imagepullbackoff",
    "errimage",
    "invalidimagename",
    "createcontainerconfigerror",
    "runcontainererror",
    # v2.10: 挂载/容器创建类故障 (host 层问题, Pod 重启救不了)
    "invalid mount",                   # OCI runtime: invalid mount ... (你之前 fluid-system 的 case)
    "bind mounts cannot have",         # bind mounts cannot have any filesystem-specific options
    "failed to create containerd task",
    "oci runtime create failed",
    # v2.10: GPU 驱动 / 库加载类故障 (host nvidia driver 问题, Pod 重启 1000 次没用)
    # 实战 case: dcgm-exporter / device-plugin-patch 已重启 2563 次仍在崩
    # 根因在 host: 驱动没装好 / containerd nvidia-runtime 配置坏 / /dev/nvidia* 没暴露
    "nvml",                            # "Failed to initialize NVML" / "could not load NVML"
    "could not load",                  # 通用动态库加载失败
    "dcgm initialization",             # DCGM initialization error
    "cuda error",                      # CUDA runtime 错
    "no such device",                  # /dev/nvidia* 设备不存在
    "failed to initialize",            # init nvml / init cuda / ... (host driver 不健康)
)

# R3 命中的 alertname 集合 (alertname 直接命中 → 重启无救)
_R3_ALERT_TYPES = {
    "RunContainerError",
    "CreateContainerConfigError",
    "CreateContainerError",
    "InvalidImageName",
    "ImageInspectError",
    "ErrImagePull",
    "ImagePullBackOff",
    "ErrImageNeverPull",
}


def _is_non_restartable_failure(rca_text: str, alert_type: str) -> bool:
    """R3: 判断是否为"重启无救"型故障.

    命中条件 (二选一):
    1. RCA 文本里出现 _R3_RCA_HINTS 任一关键词
    2. alertname 在 _R3_ALERT_TYPES 集合中
    """
    if alert_type in _R3_ALERT_TYPES:
        return True
    if not rca_text:
        return False
    return any(hint in rca_text for hint in _R3_RCA_HINTS)


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
    # L2 灰名单: 已实现执行端 (与 ALLOWED_L2_ACTIONS 同步)
    l2_actions = {
        "restart_statefulset_pod",
        "scale_deployment",
        "rollback_deployment",
        # 未来计划:
        "evict_pod",
    }
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
    # v2.3 Memory 命中时, plan 已经在 Investigator 里复用过了, 直接跳过
    if state.get("from_memory") and state.get("remediation_plan"):
        plan = state["remediation_plan"]
        _log(f"[Remediator] ⚡ Memory 命中, 复用 plan: action={plan.get('action')} "
             f"safety={plan.get('safety_level')} target={plan.get('target')}")
        return state

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
        # 后处理: 清 action= 前缀 + 校正 target
        plan = _post_process_plan(
            plan, state.get("raw_alerts") or [], rca=rca or "")
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
    overridden = plan.get("_overridden", "")
    _log(f"[Remediator] 修复计划: action={plan.get('action')} "
         f"safety={plan.get('safety_level')} target={plan.get('target')}")
    if overridden:
        _log(f"[Remediator]   ⚠ 代码强制覆盖: {overridden}")
    return state
