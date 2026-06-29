"""主调度器: Inspector 主动巡检 + 触发现有 5 Agent 流水线诊断"""
import argparse
import os
import sys
import time
import uuid
from collections import defaultdict

from agents.inspector import run_inspector
from agents.metrics_inspector import run_metrics_inspector
from graph import build_graph
from tools.langfuse_setup import (
    LANGFUSE_HANDLER,
    start_cycle_trace,
    end_cycle_trace,
    flush_langfuse,
)


def _log(msg):
    print(msg, flush=True)
    sys.stdout.flush()


def _issue_to_alert(issue):
    """把 Inspector issue 转成 Alertmanager 告警 (附带 deep_finding)"""
    sev_map = {"critical": "critical", "high": "warning", "medium": "warning", "low": "info"}
    typ = issue.get("type", "Unhealthy")
    sev = sev_map.get(issue.get("severity"), "warning")
    ns = issue.get("namespace", "")
    pod = issue.get("pod", "")
    node = issue.get("node", "")
    summary = issue.get("summary", "")
    phase = issue.get("phase", "")
    restarts = issue.get("restarts", 0)
    reason = (issue.get("reason") or "")[:200]
    deep = (issue.get("deep_finding") or "")[:300]
    owner_kind = issue.get("owner_kind", "Unknown")
    source = issue.get("source", "pod")

    # v2.12: metric 来源的 issue 拼装方式不同 — 重点是 metric_query + value
    if source == "metrics":
        mq = (issue.get("metric_query") or "")[:200]
        mv = issue.get("metric_value", "?")
        labels = issue.get("metric_labels") or {}
        # 取几个关键 label 摘要
        keep = {k: v for k, v in labels.items()
                if k in ("pod", "namespace", "instance", "node", "container")}
        parts = [f"指标异常 (来源 MetricsInspector)"]
        parts.append(f"PromQL: {mq}")
        parts.append(f"当前值: {mv}")
        if keep:
            parts.append(f"关键 labels: {keep}")
        description = " | ".join(parts)
    else:
        parts = [f"Pod {ns}/{pod} on node {node} owner={owner_kind}"]
        parts.append(f"phase={phase} restarts={restarts}")
        parts.append(f"reason: {reason}")
        if deep:
            parts.append(f"Inspector 收集的细节: {deep}")
        description = " | ".join(parts)

    return {
        "labels": {
            "alertname": typ,
            "severity": sev,
            "namespace": ns,
            "instance": pod,
            "node": node,
            "owner_kind": owner_kind,
            "source": source,
        },
        "annotations": {
            "summary": summary,
            "description": description,
        },
        "startsAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _service_prefix(pod_name: str, owner_kind: str) -> str:
    """从 pod 名提服务前缀, 用于按"服务"维度分组 (而不是按 ns+type 粗暴归一组).

    K8s 命名约定:
    - ReplicaSet (Deployment) Pod: <deployment>-<rs-hash>-<pod-hash>  → 去最后 2 段
    - DaemonSet/StatefulSet/Job/BarePod: <controller>-<suffix>         → 去最后 1 段

    例:
    - kube-external-auditor-192.168.48.78 (BarePod)              → kube-external-auditor
    - dcgm-exporter-75h9v (DaemonSet)                            → dcgm-exporter
    - device-plugin-patch-5vw58 (DaemonSet)                      → device-plugin-patch
    - baremetal-operator-controller-manager-7466749c9f-q98kw (RS) → baremetal-operator-controller-manager
    - abcd-abcd-task-manager-0 (StatefulSet)           → abcd-abcd-task-manager

    这样 dcgm-exporter / device-plugin-patch / kube-external-auditor 三个不同服务
    在同 ns + 同 type (CrashLoopBackOff) 时会分到三组, 各自被诊断. 准确性优先.
    """
    if not pod_name:
        return ""
    parts = pod_name.split("-")
    if owner_kind == "ReplicaSet":
        return "-".join(parts[:-2]) if len(parts) >= 3 else pod_name
    return "-".join(parts[:-1]) if len(parts) >= 2 else pod_name


def _group_similar_issues(issues):
    """同类去重: 把 (namespace + type + service_prefix) 一致的 issue 归一组,
    每组挑 restarts 最多的当代表诊断, 结论应用到全组成员。

    v2.5 修复: 之前只按 (ns, type) 归组, 导致同 ns 不同服务都是 CrashLoopBackOff
    时会被合并到一起 (例如 dcgm-exporter / device-plugin-patch / kube-external-auditor
    都在 kube-system + CrashLoopBackOff). 准确性优先于成本, 加上 service_prefix 维度.

    返回: [(代表 issue, [组内成员 pod 列表]), ...]
    """
    groups = defaultdict(list)
    for i in issues:
        prefix = _service_prefix(i.get("pod", ""), i.get("owner_kind", ""))
        key = (i.get("namespace", ""), i.get("type", ""), prefix)
        groups[key].append(i)

    # "卡死状态" 关键词 (跟 tools/k8s_tools._is_stuck 同步).
    # 这些故障 restart=0 但永不自愈, 必须排在前面.
    NON_HEALING_KEYWORDS = (
        "imagepull", "errimage", "imagebackoff", "invalidimage",
        "createcontainerconfigerror", "createcontainererror",
        "runcontainererror", "configerror",
    )

    def _is_stuck(issue):
        r = (issue.get("reason") or "").lower()
        t = (issue.get("type") or "").lower()
        return any(kw in r or kw in t for kw in NON_HEALING_KEYWORDS)

    result = []
    for key, members in groups.items():
        # 组内按 restarts 降序排, 第一个当代表
        members.sort(key=lambda x: x.get("restarts", 0), reverse=True)
        representative = members[0]
        member_pods = [m.get("pod", "") for m in members]
        result.append((representative, member_pods))

    # 排序优先级:
    # 1. severity (critical > high > medium > low)
    # 2. 卡死状态 (ImagePullError 等永不自愈, 优先级 > 同 severity 的反复重启)
    # 3. restarts 数 (大 → 小)
    # 这样 ImagePullError (restart=0 + high) 排在 fluid-system (restart=666 + high) 前面
    sev_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    result.sort(key=lambda x: (
        sev_rank.get(x[0].get("severity"), 9),
        0 if _is_stuck(x[0]) else 1,   # 卡死的排前 (False=1, True=0)
        -x[0].get("restarts", 0),
    ))
    return result


def run_one_inspection_cycle(top_n=20, deep_max_steps=8, dedup=True):
    cycle_id = str(uuid.uuid4())[:8]
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")
    _log("")
    _log("#" * 70)
    _log(f"# 巡检周期 {cycle_id} 开始 @ {now_str}")
    _log("#" * 70)

    # 开启 Langfuse trace (整个周期一个 trace, 内部所有 span 自动归入)
    cycle_trace = start_cycle_trace(
        name="inspection_cycle",
        session_id=cycle_id,
        metadata={
            "started_at": now_str,
            "top_n": top_n,
            "dedup": dedup,
            "deep_max_steps": deep_max_steps,
        },
    )

    try:
        return _run_cycle_body(cycle_id, top_n, deep_max_steps, dedup, cycle_trace)
    finally:
        flush_langfuse()


def _run_cycle_body(cycle_id, top_n, deep_max_steps, dedup, cycle_trace):
    issues = run_inspector(top_n=None, deep_max_steps=deep_max_steps)

    # v2.12: MetricsInspector 并行采集指标层异常, merge 到同一个 issues 列表
    metric_issues = []
    try:
        metric_issues = run_metrics_inspector()
    except Exception as e:
        _log(f"[调度器] MetricsInspector 失败, 跳过 (不影响主流程): {e}")
    if metric_issues:
        _log(f"[调度器] MetricsInspector 贡献 {len(metric_issues)} 个指标异常, "
             f"合并到调度队列")
        issues = (issues or []) + metric_issues

    if not issues:
        _log("[调度器] 集群无异常, 周期结束")
        end_cycle_trace(cycle_trace, output={"reports": [], "total_issues": 0})
        return []

    # v2.6 策略过滤已经前移到 Inspector 内部 (run_inspector), 这里 issues 是过滤后的

    # 1) v2.4: 取消 critical/high 硬过滤, 所有 severity 都进流水线 (按级分诊).
    #   - critical/high → 完整 ReAct 诊断 (deep_max_steps 默认 8)
    #   - medium        → 轻量诊断 (max_steps=3, 只看日志和 events 就结)
    #   - low           → 不调 LLM, 只记审计 (无遗漏 + 不浪费 token)
    # critical/high 仍然先排序在前, 但 medium/low 不再被丢弃.
    sev_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    issues_sorted = sorted(issues, key=lambda x: (
        sev_rank.get(x.get("severity"), 9),
        -x.get("restarts", 0),
    ))
    priority = [i for i in issues_sorted if i.get("severity") in ("critical", "high")]
    medium_issues = [i for i in issues_sorted if i.get("severity") == "medium"]
    low_issues = [i for i in issues_sorted if i.get("severity") == "low"]
    total = len(issues)
    pri_count = len(priority)
    med_count = len(medium_issues)
    low_count = len(low_issues)

    # 2) 去重 (默认开启): 同 namespace + type 归组, 每组只诊断代表
    if dedup:
        grouped_pri = _group_similar_issues(priority)
        grouped_med = _group_similar_issues(medium_issues)
        # 优先 critical/high 组, 再排 medium 组 (medium 走轻量诊断)
        all_groups = [(g, "full") for g in grouped_pri] + \
                     [(g, "light") for g in grouped_med]
        # top_n=0 表示不限制 (诊断全部, 只靠去重和 Memory 控成本)
        if top_n and top_n > 0:
            selected_groups_typed = all_groups[:top_n]
        else:
            selected_groups_typed = all_groups
        sel_count = len(selected_groups_typed)
        pri_groups_selected = sum(1 for _, mode in selected_groups_typed if mode == "full")
        med_groups_selected = sel_count - pri_groups_selected
        saved = (pri_count + med_count) - (
            sum(len(pods) for (_, pods), _ in selected_groups_typed))
        _log("")
        _log("=" * 70)
        _log(f"[调度器] 集群异常 {total} 个 "
             f"(critical/high {pri_count}, medium {med_count}, low {low_count})")
        _log(f"[调度器] 去重: critical/high → {len(grouped_pri)} 组, "
             f"medium → {len(grouped_med)} 组, 节省 {saved} 次 LLM")
        _log(f"[调度器] 本轮诊断 {sel_count} 组: "
             f"{pri_groups_selected} 组完整诊断 + {med_groups_selected} 组轻量诊断")
        if low_count > 0:
            _log(f"[调度器] {low_count} 个 low 级异常仅记录审计, 不调 LLM")
        _log("=" * 70)
        selected_groups = selected_groups_typed
    else:
        # 未去重模式: 也按 severity 排序, critical/high 在前, medium 跟上
        items = ([(i, "full") for i in priority] +
                 [(i, "light") for i in medium_issues])
        if top_n and top_n > 0:
            items = items[:top_n]
        selected_groups = [((i, [i.get("pod", "")]), mode) for i, mode in items]
        sel_count = len(selected_groups)
        _log("")
        _log("=" * 70)
        _log(f"[调度器] 共 {total} 个异常 "
             f"(critical/high {pri_count}, medium {med_count}, low {low_count})")
        _log(f"[调度器] 选 Top {sel_count} 触发流水线 (未去重)")
        _log("=" * 70)

    # 3) 逐组诊断
    pipeline = build_graph()
    # langfuse callback 透传到流水线 (LangGraph + LangChain 自动捕获)
    runtime_config = {"callbacks": [LANGFUSE_HANDLER]} if LANGFUSE_HANDLER else None
    reports = []
    for idx, ((issue, member_pods), mode) in enumerate(selected_groups):
        sm = issue.get("summary", "")
        progress = f"{idx+1}/{sel_count}"
        ns = issue.get("namespace", "")
        typ = issue.get("type", "")
        n_members = len(member_pods)
        mode_label = "完整" if mode == "full" else "轻量"
        _log("")
        _log("-" * 70)
        if n_members > 1:
            _log(f"[调度器] 诊断 {progress} ({mode_label}): "
                 f"[{ns}] {typ} 类型, 代表 Pod: {issue.get('pod')}")
            _log(f"          组内共 {n_members} 个同类 Pod, 结论将应用到全组")
        else:
            _log(f"[调度器] 诊断 {progress} ({mode_label}): {sm}")
        _log("-" * 70)

        alert = _issue_to_alert(issue)
        trace_id = f"{cycle_id}-{idx}"
        initial_state = {
            "raw_alerts": [alert],
            "trace_id": trace_id,
            "alert_count": 0,
            "evidence": [],
            "notification_sent": False,
            "investigation_mode": mode,  # v2.4: 让 Investigator 知道走轻量还是完整
        }
        try:
            if runtime_config:
                final_state = pipeline.invoke(initial_state, config=runtime_config)
            else:
                final_state = pipeline.invoke(initial_state)
            reports.append({
                "namespace": ns,
                "type": typ,
                "representative_pod": issue.get("pod"),
                "affected_pods": member_pods,
                "affected_count": n_members,
                "label": final_state.get("label"),
                "severity": final_state.get("severity"),
                "rca": final_state.get("rca_hypothesis"),
                "trace_id": trace_id,
            })
        except Exception as e:
            err_text = str(e)
            _log(f"[调度器] 诊断失败: {err_text}")
            reports.append({
                "namespace": ns,
                "type": typ,
                "representative_pod": issue.get("pod"),
                "affected_pods": member_pods,
                "affected_count": n_members,
                "error": err_text,
                "trace_id": trace_id,
            })

    # 4) low 级异常: 不调 LLM, 只写一行审计日志 (无遗漏的代价仅一笔记录)
    if low_count > 0:
        try:
            from tools.safety_guards import record_audit
            for it in low_issues:
                record_audit({
                    "stage": "low_severity_skip",
                    "trace_id": cycle_id,
                    "namespace": it.get("namespace"),
                    "pod": it.get("pod"),
                    "type": it.get("type"),
                    "restarts": it.get("restarts", 0),
                    "reason": "severity=low, 仅记录审计未诊断",
                })
            _log(f"[调度器] {low_count} 个 low 级异常已写入审计 (无 LLM 调用)")
        except Exception as e:
            _log(f"[调度器] low 审计写入失败 (不影响主流程): {e}")

    # 5) 周期总结
    _log("")
    _log("#" * 70)
    _log(f"# 巡检周期 {cycle_id} 总结")
    _log("#" * 70)
    total_covered = sum(r.get("affected_count", 1) for r in reports)
    _log(f"[调度器] 集群异常总数: {total}")
    _log(f"[调度器] 完成深度诊断: {len(reports)} 组, 覆盖 {total_covered} 个 Pod")
    coverage = round(100 * total_covered / max(total, 1), 1)
    _log(f"[调度器] 覆盖率: {coverage}% "
         f"(LLM 调用 {len(reports)} 次, low {low_count} 个仅审计)")
    _log("")
    for idx, r in enumerate(reports):
        no = idx + 1
        ns = r.get("namespace", "")
        typ = r.get("type", "")
        cnt = r.get("affected_count", 1)
        rep = r.get("representative_pod", "")
        err = r.get("error")
        if err:
            _log(f"  [{no}] [{ns}] {typ} (影响 {cnt} 个 Pod, 代表: {rep})")
            _log(f"      失败: {err}")
        else:
            rca = (r.get("rca") or "")[:200]
            _log(f"  [{no}] [{ns}] {typ} (影响 {cnt} 个 Pod, 代表: {rep})")
            _log(f"      诊断: {rca}")
            # 列出全组成员 (节省篇幅, 超过 5 个用省略号)
            if cnt > 1:
                preview = ", ".join(r.get("affected_pods", [])[:5])
                if cnt > 5:
                    preview += f", ... (共 {cnt} 个)"
                _log(f"      全组: {preview}")

    # 5) 关闭 Langfuse trace, 把统计信息附在 trace 上
    end_cycle_trace(cycle_trace, output={
        "total_issues": total,
        "priority_count": pri_count,
        "selected_groups": sel_count,
        "diagnosed_count": len(reports),
        "covered_pods": total_covered,
        "coverage_pct": coverage,
    })
    return reports


def main_loop(interval_sec=0, top_n=20, dedup=True, deep_max_steps=8):
    while True:
        try:
            run_one_inspection_cycle(
                top_n=top_n,
                deep_max_steps=deep_max_steps,
                dedup=dedup,
            )
        except KeyboardInterrupt:
            _log("[调度器] 收到中断, 退出")
            return
        except Exception as e:
            err = str(e)
            _log(f"[调度器] 周期异常: {err}")
        if interval_sec <= 0:
            _log("[调度器] 单次模式, 退出")
            return
        _log(f"[调度器] 等待 {interval_sec}s 后下一轮...")
        time.sleep(interval_sec)


def _parse_args():
    p = argparse.ArgumentParser(description="AIOps 多 Agent 巡检 + 诊断闭环")
    p.add_argument("--top", type=int, default=50,
                   help="每轮最多诊断的异常组数 (去重后, 含 critical/high/medium). "
                        "默认 50, 配合同类去重和 Memory 已能覆盖大集群. "
                        "设 0 表示不限制")
    p.add_argument("--no-dedup", action="store_true",
                   help="关闭同类去重 (默认开启). 关闭后 Top N 按 Pod 个数计")
    p.add_argument("--interval", type=int, default=0,
                   help="循环间隔秒数, 0=单次模式 (默认 0)")
    p.add_argument("--deep-steps", type=int, default=8,
                   help="Inspector + Investigator 深入调查最大步数 (默认 8, 32B 建议 6-10)")
    p.add_argument("--policies", type=str, default=None,
                   help="忽略策略 YAML 文件路径. 默认 config/policies.yaml. "
                        "示例见 config/policies.yaml.example")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    # v2.6: 让 main_loop 拿到 policies 文件路径 (通过环境变量传给 _run_cycle_body)
    if args.policies:
        os.environ["POLICIES_FILE"] = args.policies
    main_loop(
        interval_sec=args.interval,
        top_n=args.top,
        dedup=not args.no_dedup,
        deep_max_steps=args.deep_steps,
    )
