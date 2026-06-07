"""主调度器: Inspector 主动巡检 + 触发现有 5 Agent 流水线诊断"""
import sys
import time
import uuid
from agents.inspector import run_inspector
from graph import build_graph


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


def run_one_inspection_cycle(top_n=5, deep_max_steps=4):
    cycle_id = str(uuid.uuid4())[:8]
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")
    _log("")
    _log("#" * 70)
    _log(f"# 巡检周期 {cycle_id} 开始 @ {now_str}")
    _log("#" * 70)

    issues = run_inspector(top_n=None, deep_max_steps=deep_max_steps)
    if not issues:
        _log("[调度器] 集群无异常, 周期结束")
        return []

    priority = [i for i in issues if i.get("severity") in ("critical", "high")]
    selected = priority[:top_n]
    total = len(issues)
    pri_count = len(priority)
    sel_count = len(selected)
    _log("")
    _log("=" * 70)
    _log(f"[调度器] 共 {total} 个异常, 优先级 critical/high {pri_count} 个")
    _log(f"[调度器] 选 Top {sel_count} 触发完整诊断流水线")
    _log("=" * 70)

    pipeline = build_graph()
    reports = []
    for idx, issue in enumerate(selected):
        sm = issue.get("summary", "")
        progress = f"{idx+1}/{sel_count}"
        _log("")
        _log("-" * 70)
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
            final_state = pipeline.invoke(initial_state)
            reports.append({
                "issue": sm,
                "label": final_state.get("label"),
                "severity": final_state.get("severity"),
                "rca": final_state.get("rca_hypothesis"),
                "trace_id": trace_id,
            })
        except Exception as e:
            err_text = str(e)
            _log(f"[调度器] 诊断失败: {err_text}")
            reports.append({"issue": sm, "error": err_text, "trace_id": trace_id})

    _log("")
    _log("#" * 70)
    _log(f"# 巡检周期 {cycle_id} 总结")
    _log("#" * 70)
    _log(f"[调度器] 集群异常总数: {total}")
    _log(f"[调度器] 完成深度诊断: {len(reports)}")
    _log("")
    for idx, r in enumerate(reports):
        no = idx + 1
        issue_text = r.get("issue", "")
        err = r.get("error")
        if err:
            _log(f"  [{no}] {issue_text}")
            _log(f"      失败: {err}")
        else:
            rca = (r.get("rca") or "")[:200]
            _log(f"  [{no}] {issue_text}")
            _log(f"      诊断: {rca}")
    return reports


def main_loop(interval_sec=0):
    while True:
        try:
            run_one_inspection_cycle(top_n=5)
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


if __name__ == "__main__":
    main_loop(interval_sec=0)
