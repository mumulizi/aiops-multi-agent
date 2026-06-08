"""主调度器: Inspector 主动巡检 + 触发现有 5 Agent 流水线诊断"""
import argparse
import sys
import time
import uuid
from collections import defaultdict

from agents.inspector import run_inspector
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
    parts = [f"Pod {ns}/{pod} on node {node}"]
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
        },
        "annotations": {
            "summary": summary,
            "description": description,
        },
        "startsAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _group_similar_issues(issues):
    """同类去重: 把 (namespace + type) 一致的 issue 归一组,
    每组挑 restarts 最多的当代表诊断, 结论应用到全组成员。
    返回: [(代表 issue, [组内成员 pod 列表]), ...]
    """
    groups = defaultdict(list)
    for i in issues:
        key = (i.get("namespace", ""), i.get("type", ""))
        groups[key].append(i)

    result = []
    for key, members in groups.items():
        # 组内按 restarts 降序排, 第一个当代表
        members.sort(key=lambda x: x.get("restarts", 0), reverse=True)
        representative = members[0]
        member_pods = [m.get("pod", "") for m in members]
        result.append((representative, member_pods))

    # 按代表的 severity + restarts 排序
    sev_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    result.sort(key=lambda x: (sev_rank.get(x[0].get("severity"), 9), -x[0].get("restarts", 0)))
    return result


def run_one_inspection_cycle(top_n=20, deep_max_steps=4, dedup=True):
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
    if not issues:
        _log("[调度器] 集群无异常, 周期结束")
        end_cycle_trace(cycle_trace, output={"reports": [], "total_issues": 0})
        return []

    # 1) 优先级筛选
    priority = [i for i in issues if i.get("severity") in ("critical", "high")]
    total = len(issues)
    pri_count = len(priority)

    # 2) 去重 (默认开启): 同 namespace + type 归组, 每组只诊断代表
    if dedup:
        grouped = _group_similar_issues(priority)
        selected_groups = grouped[:top_n]
        sel_count = len(selected_groups)
        saved = pri_count - sel_count
        _log("")
        _log("=" * 70)
        _log(f"[调度器] 集群异常 {total} 个, 优先级 critical/high {pri_count} 个")
        _log(f"[调度器] 同类去重后 {len(grouped)} 组独立根因类型 (节省 {saved} 次 LLM 调用)")
        _log(f"[调度器] 选 Top {sel_count} 组触发完整诊断流水线")
        _log("=" * 70)
    else:
        selected_groups = [(i, [i.get("pod", "")]) for i in priority[:top_n]]
        sel_count = len(selected_groups)
        _log("")
        _log("=" * 70)
        _log(f"[调度器] 共 {total} 个异常, 优先级 critical/high {pri_count} 个")
        _log(f"[调度器] 选 Top {sel_count} 触发完整诊断流水线 (未去重)")
        _log("=" * 70)

    # 3) 逐组诊断
    pipeline = build_graph()
    # langfuse callback 透传到流水线 (LangGraph + LangChain 自动捕获)
    runtime_config = {"callbacks": [LANGFUSE_HANDLER]} if LANGFUSE_HANDLER else None
    reports = []
    for idx, (issue, member_pods) in enumerate(selected_groups):
        sm = issue.get("summary", "")
        progress = f"{idx+1}/{sel_count}"
        ns = issue.get("namespace", "")
        typ = issue.get("type", "")
        n_members = len(member_pods)
        _log("")
        _log("-" * 70)
        if n_members > 1:
            _log(f"[调度器] 诊断 {progress}: [{ns}] {typ} 类型, 代表 Pod: {issue.get('pod')}")
            _log(f"          组内共 {n_members} 个同类 Pod, 结论将应用到全组")
        else:
            _log(f"[调度器] 诊断 {progress}: {sm}")
        _log("-" * 70)

        alert = _issue_to_alert(issue)
        trace_id = f"{cycle_id}-{idx}"
        initial_state = {
            "raw_alerts": [alert],
            "trace_id": trace_id,
            "alert_count": 0,
            "evidence": [],
            "notification_sent": False,
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

    # 4) 周期总结
    _log("")
    _log("#" * 70)
    _log(f"# 巡检周期 {cycle_id} 总结")
    _log("#" * 70)
    total_covered = sum(r.get("affected_count", 1) for r in reports)
    _log(f"[调度器] 集群异常总数: {total}")
    _log(f"[调度器] 完成深度诊断: {len(reports)} 组, 覆盖 {total_covered} 个 Pod")
    coverage = round(100 * total_covered / max(total, 1), 1)
    _log(f"[调度器] 覆盖率: {coverage}% (LLM 调用 {len(reports)} 次)")
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


def main_loop(interval_sec=0, top_n=20, dedup=True, deep_max_steps=4):
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
    p.add_argument("--top", type=int, default=20,
                   help="每轮最多诊断的异常组数 (去重后), 默认 20")
    p.add_argument("--no-dedup", action="store_true",
                   help="关闭同类去重 (默认开启). 关闭后 Top N 按 Pod 个数计")
    p.add_argument("--interval", type=int, default=0,
                   help="循环间隔秒数, 0=单次模式 (默认 0)")
    p.add_argument("--deep-steps", type=int, default=4,
                   help="Inspector 深入调查最大步数 (默认 4)")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    main_loop(
        interval_sec=args.interval,
        top_n=args.top,
        dedup=not args.no_dedup,
        deep_max_steps=args.deep_steps,
    )
