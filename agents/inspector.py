"""Inspector Agent: 主动巡检 (代码兜底, 无 LLM).

v2.7 简化:
- 删除原"阶段 2 Top 5 LLM 深入预览" + "阶段 4 LLM 整体摘要"
  (跟调度器完整诊断冗余, 浪费 LLM 调用)
- Inspector 现在纯代码逻辑: 收集异常 → 分级 → 应用策略 → 输出 Top N

LLM 推理全部留给调度器后续的 Investigator/Remediator/Validator.
"""
import sys
from tools.k8s_tools import (
    get_cluster_overview,
    collect_all_real_issues,
)
from tools.langfuse_setup import TraceTimer


def _log(msg):
    print(msg, flush=True)
    sys.stdout.flush()


def _classify_severity(restarts, phase, reason=""):
    """计算异常严重度.

    旧版只看 restarts + phase, 但 ImagePullBackOff / ErrImagePull / Pending
    这些"卡死但不重启" (restarts=0) 的故障会被打成 low, 被调度器优先级过滤掉,
    根本进不了诊断流水线.

    新版按"是否会自愈"分级:
    - 永远不会自愈的卡死状态 (镜像/调度/配置错) → high (即使 restarts=0)
    - 反复重启型 (CrashLoopBackOff) 按 restart 数分级
    - 其他 phase 异常按原逻辑
    """
    r = (reason or "").lower()

    # phase=Failed/Unknown 始终是 critical
    if phase in ("Failed", "Unknown"):
        return "critical"

    # 永远不会自愈的状态 → high (即使 restart=0)
    # 这些是 R3 重启无救型故障的早期信号, 不该被埋没在 low 里
    NON_HEALING_KEYWORDS = (
        "imagepull", "errimage", "imagebackoff", "invalidimage",
        "createcontainerconfigerror", "createcontainererror",
        "runcontainererror", "configerror",
    )
    if any(kw in r for kw in NON_HEALING_KEYWORDS):
        return "high"

    # Pending (调度失败 / PVC 未绑定 / 资源不足) → high
    # 这种状态不会自愈, 但也不会进 restart 循环, 必须主动诊断
    if phase == "Pending":
        return "high"

    # 反复重启型: 按 restart 数分级
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
        owner_kind = p.get("owner_kind", "Unknown")
        sev = _classify_severity(restarts, phase, reason)
        typ = _classify_type(reason)
        summary = f"{ns}/{name} {typ} restarts={restarts} owner={owner_kind}"
        issues.append({
            "namespace": ns,
            "pod": name,
            "type": typ,
            "severity": sev,
            "summary": summary,
            "owner_kind": owner_kind,
            "restarts": restarts,
            "phase": phase,
            "reason": reason,
        })
    return issues



def run_inspector(top_n=None, deep_max_steps=8):
    """两阶段巡检. top_n=None 表示返回所有真实异常.

    v2.7 简化:
    - 删除原"阶段 2 Top 5 深入预览" (跟调度器完整诊断冗余, 浪费一次 LLM)
    - 删除原"阶段 3 合并 deep_finding" (没有 deep_finding 字段需要合并了)
    - 删除原"阶段 4 LLM 整体摘要" (调度器最后会总结所有 reports, 也冗余)
    - 把策略过滤前移到 Inspector 内部, 在打印 Top 10 之前先过滤,
      保证 Top 10 不会显示被策略忽略的 Pod
    """
    _log("=" * 60)
    _log("[Inspector] 启动主动巡检 (代码兜底, 无 LLM)")
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

    # 阶段 2: 应用 YAML 忽略策略 (v2.6, 前移到 Inspector 内部)
    # 这样 Top 10 / overview / 下游所有逻辑都看不到被忽略的 Pod
    try:
        from tools.policy import load_policies, filter_issues
        policies = load_policies()
        if policies.get("ignores"):
            before_n = len(issues)
            issues, ignored_log = filter_issues(issues, policies)
            after_n = len(issues)
            if ignored_log:
                _log("")
                _log(f"[Inspector] 策略忽略 {before_n - after_n} 个 Pod "
                     f"(共 {len(policies.get('ignores'))} 条规则):")
                from collections import Counter
                reason_count = Counter(x["reason"] for x in ignored_log)
                for reason, n in reason_count.most_common():
                    _log(f"  - {n} 个: {reason}")
    except Exception as e:
        _log(f"[Inspector] ⚠ 策略加载/过滤失败 (不影响主流程): {e}")

    if not issues:
        _log("[Inspector] 策略过滤后无异常, 退出")
        return []
    _log("")

    # 输出 Top 10 概览 (代码生成, 无 LLM, 仅给运维一眼看到优先级)
    _log("=" * 60)
    _log(f"[Inspector] 巡检完成, 共 {len(issues)} 个真实异常 (无遗漏, 已应用策略)")
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

    return issues
