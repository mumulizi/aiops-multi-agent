"""审批 CLI: 在服务器上手动审批 L2 操作.

用法:
  uv run python -m scripts.aiops_review list
  uv run python -m scripts.aiops_review show <approval_id>
  uv run python -m scripts.aiops_review approve <approval_id> [--by <name>] [--note "..."]
  uv run python -m scripts.aiops_review deny <approval_id> [--by <name>] [--reason "..."]

审批流程:
  approve → 校验 ID 存在 + 未过期 + 状态 pending
         → 校验 action 在 L3 白名单 (双重保险, 防绕过)
         → 调 execute_action() 执行 (复用 Executor 逻辑)
         → 等 30s 调 Validator 验证
         → 推 IM 第二条消息: 执行结果 + 验证结果
"""
import argparse
import getpass
import json
import os
import sys
import time

from tools.approval_store import (
    list_pending, list_recent, get, mark_approved, mark_rejected, mark_executed,
    is_expired, DEFAULT_TTL_SEC,
    get_diagnostic,  # v2.14: 诊断命令审批
)
from tools.remediation_actions import (
    is_action_allowed, execute_action, _split_target, _capture_pod_state,
)
from tools.safety_guards import allow as rate_allow, record_audit
from tools.im_notify import send_message
from tools.k8s_tools import _v1, _kube_ok


def _log(msg):
    print(msg, flush=True)
    sys.stdout.flush()


def _is_dry_run() -> bool:
    return os.getenv("AUTO_HEAL_DRY_RUN", "true").lower() == "true"


def _heal_enabled() -> bool:
    return os.getenv("AUTO_HEAL_ENABLED", "false").lower() == "true"


def _fmt_time(ts):
    if not ts:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _check_pod_recreated(namespace, old_pod_name):
    """简化版 validator 检查 (从 agents.validator 抽取核心逻辑)"""
    if not _kube_ok:
        return {"found": False}
    try:
        pods = _v1.list_namespaced_pod(namespace=namespace, timeout_seconds=10).items
    except Exception:
        return {"found": False}
    parts = old_pod_name.rsplit("-", 2)
    prefix = parts[0] if len(parts) >= 2 else old_pod_name
    candidates = [p for p in pods if p.metadata.name.startswith(prefix)
                  and p.metadata.name != old_pod_name]
    if not candidates:
        return {"found": False}
    latest = max(candidates, key=lambda x: x.metadata.creation_timestamp or 0)
    ready = bool(latest.status.container_statuses) and \
        all(cs.ready for cs in latest.status.container_statuses)
    return {
        "found": True,
        "new_pod": latest.metadata.name,
        "phase": latest.status.phase,
        "ready": ready,
    }


# ===== Commands =====

def cmd_list(args):
    items = list_pending(limit=50)
    if not items:
        _log("(无待审批操作)")
        return
    _log(f"待审批操作: {len(items)} 条")
    _log("-" * 90)
    for it in items:
        # v2.14: 区分 remediation vs diagnostic_cmd
        diag = get_diagnostic(it["id"])
        if diag and diag.get("kind") == "diagnostic_cmd":
            p = diag.get("payload") or {}
            kind = p.get("kind", "?")
            if kind == "ssh":
                tgt = f"node:{p.get('node', '?')}"
            else:
                tgt = f"pod:{p.get('namespace', '?')}/{p.get('pod', '?')}"
            _log(f"[{it['id']}]  🔍 diagnostic_cmd  age={it['age_sec']}s  remaining={it['remaining_sec']}s")
            _log(f"   target: {tgt}")
            _log(f"   cmd:    {(p.get('cmd', ''))[:100]}")
            _log(f"   reason: {(p.get('reason', ''))[:100]}")
            _log("")
            continue
        # 老 remediation 审批
        plan = it["plan"] or {}
        _log(f"[{it['id']}]  🔧 remediation  age={it['age_sec']}s  remaining={it['remaining_sec']}s")
        _log(f"   action={plan.get('action', '?')}  target={plan.get('target', '?')}  "
             f"safety={plan.get('safety_level', '?')}")
        rationale = plan.get('rationale', '')
        if rationale:
            _log(f"   rationale: {rationale[:120]}")
        _log("")


def cmd_show(args):
    # v2.14: 先看是不是 diagnostic_cmd
    diag = get_diagnostic(args.approval_id)
    if diag and diag.get("kind") == "diagnostic_cmd":
        _log(f"=== Approval {diag['id']}  (kind=diagnostic_cmd) ===")
        _log(f"created_at  : {_fmt_time(diag['created_at'])}")
        _log(f"status      : {diag['status']}")
        _log(f"ttl_sec     : {diag['ttl_sec']}")
        if diag.get('decided_by'):
            _log(f"decided_by  : {diag['decided_by']}")
            _log(f"decided_at  : {_fmt_time(diag['decided_at'])}")
        _log("")
        _log("=== Payload ===")
        _log(json.dumps(diag["payload"], ensure_ascii=False, indent=2))
        if diag.get("execution_result"):
            _log("")
            _log("=== Execution Result ===")
            _log(json.dumps(diag["execution_result"], ensure_ascii=False, indent=2))
        return

    # 老 remediation
    rec = get(args.approval_id)
    if not rec:
        _log(f"❌ 未找到 approval_id={args.approval_id}")
        sys.exit(1)
    _log(f"=== Approval {rec['id']} ===")
    _log(f"created_at  : {_fmt_time(rec['created_at'])}")
    _log(f"status      : {rec['status']}")
    _log(f"ttl_sec     : {rec['ttl_sec']}")
    _log(f"expired     : {is_expired(rec)}")
    if rec.get('decided_by'):
        _log(f"decided_by  : {rec['decided_by']}")
        _log(f"decided_at  : {_fmt_time(rec['decided_at'])}")
    _log("")
    _log("=== Plan ===")
    _log(json.dumps(rec["plan"], ensure_ascii=False, indent=2))
    _log("")
    _log("=== State (key fields) ===")
    s = rec["state"] or {}
    for k in ("trace_id", "severity", "label", "event_summary", "rca_hypothesis",
              "approval_reason"):
        if k in s:
            v = s[k]
            v_str = str(v)
            if len(v_str) > 200:
                v_str = v_str[:200] + "..."
            _log(f"  {k}: {v_str}")
    if rec.get("result"):
        _log("")
        _log("=== Execution Result ===")
        _log(json.dumps(rec["result"], ensure_ascii=False, indent=2))


def cmd_approve(args):
    # v2.14: diagnostic_cmd 走独立分支, 只标 approved, 不代执行 (daemon 会 pick up)
    diag = get_diagnostic(args.approval_id)
    if diag and diag.get("kind") == "diagnostic_cmd":
        if diag["status"] != "pending":
            _log(f"❌ 当前状态={diag['status']}, 无法批准")
            sys.exit(1)
        by = args.by or os.getenv("USER") or getpass.getuser() or "unknown"
        ok, reason = mark_approved(args.approval_id, by)
        if not ok:
            _log(f"❌ {reason}")
            sys.exit(1)
        p = diag["payload"] or {}
        _log(f"✓ {args.approval_id} (diagnostic_cmd) 已批准 by {by}")
        _log(f"   cmd: {p.get('cmd', '')}")
        _log(f"   → approval_exec_worker daemon 会在几秒内 pick up 执行")
        _log(f"   → 结果会推 IM + 写入 fault_memory")
        # 发一条 IM 提示
        try:
            send_message(
                f"✅ [APPROVED diagnostic_cmd] task_id={args.approval_id}\n"
                f"  by: {by}\n"
                f"  cmd: {p.get('cmd', '')[:120]}\n"
                f"  → daemon 即将异步执行, 结果稍后另发"
            )
        except Exception:
            pass
        return

    # 老 remediation 走原路径
    rec = get(args.approval_id)
    if not rec:
        _log(f"❌ 未找到 approval_id={args.approval_id}")
        sys.exit(1)
    if is_expired(rec):
        _log(f"❌ 已过期 (age={int(time.time()) - rec['created_at']}s > ttl={rec['ttl_sec']}s)")
        sys.exit(1)
    if rec["status"] != "pending":
        _log(f"❌ 当前状态={rec['status']}, 无法批准")
        sys.exit(1)

    plan = rec["plan"] or {}
    action = plan.get("action", "")
    target = plan.get("target", "")

    # 双重保险: 必须在 L3 自动白名单 或 L2 人审灰名单 中
    if not is_action_allowed(action):
        _log(f"❌ 拒绝执行: action '{action}' 不在 L3/L2 白名单, 不能通过 CLI 执行")
        _log(f"   (检查: tools/remediation_actions.py 中 ALLOWED_ACTIONS 和 ALLOWED_L2_ACTIONS)")
        sys.exit(1)

    by = args.by or os.getenv("USER") or getpass.getuser() or "unknown"
    note = args.note or ""

    # 标记已批准
    ok, reason = mark_approved(args.approval_id, by)
    if not ok:
        _log(f"❌ 标记批准失败: {reason}")
        sys.exit(1)
    _log(f"✓ {args.approval_id} 已批准 by {by}")

    # 推 IM: 已批准, 即将执行
    sev_summary = (rec["state"] or {}).get("event_summary", "")
    send_message(
        f"✅ [APPROVED] approval_id={args.approval_id}\n"
        f"  by: {by}    action: {action}    target: {target}\n"
        f"  事件: {sev_summary}\n"
        + (f"  备注: {note}\n" if note else "")
        + f"  即将执行..."
    )

    # 安全护栏: 大开关 + 速率
    if not _heal_enabled():
        msg = "AUTO_HEAL_ENABLED=false, 即使审批通过也不执行 (kill switch)"
        _log(f"⚠ {msg}")
        send_message(f"⚠ [HALT] approval_id={args.approval_id} {msg}")
        return

    rate_ok, rate_reason = rate_allow(target, action, max_per_hour=3)
    if not rate_ok:
        _log(f"⚠ 速率限制: {rate_reason}")
        send_message(f"⚠ [RATE LIMIT] approval_id={args.approval_id} {rate_reason}")
        mark_executed(args.approval_id, {"ok": False, "reason": rate_reason})
        return

    # T0 快照
    ns, name = _split_target(target)
    snap_before = _capture_pod_state(ns, name) if (ns and name) else {}
    if snap_before and not snap_before.get("error"):
        _log(f"  T0: phase={snap_before.get('phase')} restarts={snap_before.get('total_restarts')}")

    # 执行 (scale_deployment 透传 plan.extra 里的 replicas / delta)
    dry_run = _is_dry_run()
    _log(f"  执行: {action}({target})  dry_run={dry_run}")
    extra = (plan.get("extra") or {}) if isinstance(plan.get("extra"), dict) else {}
    if action == "scale_deployment":
        kw = {k: v for k, v in extra.items() if k in ("replicas", "delta")}
        result = execute_action(action, target, dry_run=dry_run, **kw)
    else:
        result = execute_action(action, target, dry_run=dry_run)
    _log(f"  结果: {result}")

    # 审计
    record_audit({
        "stage": "manual_approval_execute",
        "approval_id": args.approval_id,
        "approved_by": by,
        "action": action,
        "target": target,
        "result": result.get("ok"),
        "dry_run": dry_run,
        "log": result.get("message") or result.get("reason", ""),
    })

    if result.get("dry_run"):
        send_message(
            f"🧪 [DRY-RUN] approval_id={args.approval_id}\n"
            f"  {result.get('message', '')}"
        )
        mark_executed(args.approval_id, result)
        return

    if not result.get("ok"):
        send_message(
            f"❌ [EXEC FAILED] approval_id={args.approval_id}\n"
            f"  reason: {result.get('reason')}"
        )
        mark_executed(args.approval_id, result)
        return

    # 验证 (等 30s)
    wait_sec = int(os.getenv("VALIDATOR_WAIT_SEC", "30"))
    _log(f"  等待 {wait_sec}s 验证...")
    time.sleep(wait_sec)

    if not (ns and name):
        validation = {"status": "skipped", "reason": "invalid target"}
    else:
        snap_now = _capture_pod_state(ns, name)
        if snap_now.get("error"):
            # Pod 不见了
            if action in ("delete_evicted_pod", "delete_completed_job_pod", "delete_failed_pod"):
                validation = {"status": "success", "reason": "pod deleted as expected"}
            elif action in ("restart_pod", "restart_pod_for_image_pull"):
                rec_check = _check_pod_recreated(ns, name)
                if rec_check.get("found"):
                    validation = {
                        "status": "success",
                        "reason": (
                            f"pod recreated by controller "
                            f"(new pod: {rec_check['new_pod']}, ready={rec_check['ready']})"
                        ),
                    }
                else:
                    validation = {"status": "pending", "reason": "controller in progress"}
            else:
                validation = {
                    "status": "failed",
                    "reason": f"pod gone after {action} - unexpected",
                }
        else:
            phase = snap_now.get("phase")
            ready = not snap_now.get("any_not_ready", True)
            before_restarts = (snap_before or {}).get("total_restarts", 0)
            now_restarts = snap_now.get("total_restarts", 0)

            # "重启无救" 型故障升级人审 (RunContainerError / ImagePullBackOff / ConfigError ...)
            from agents.validator import _diagnose_restart_futility
            is_futile, futile_reasons = _diagnose_restart_futility(snap_now)
            if is_futile:
                validation = {
                    "status": "escalate_human",
                    "reason": (
                        f"重启无救型故障 (waiting.reason={','.join(futile_reasons)}); "
                        f"根因不在 runtime, 重启不解决问题, 请人工检查配置/镜像/启动参数"
                    ),
                    "futile_reasons": futile_reasons,
                }
            elif phase == "Running" and ready and now_restarts <= before_restarts + 1:
                validation = {"status": "success", "reason": f"ready, restart_delta={now_restarts - before_restarts}"}
            elif now_restarts > before_restarts + 5:
                validation = {"status": "failed", "reason": f"restarts +{now_restarts - before_restarts}"}
            else:
                validation = {"status": "pending", "reason": f"phase={phase} ready={ready}"}

    _log(f"  验证: {validation}")
    final_result = {"ok": result.get("ok"), "execution": result, "validation": validation}
    mark_executed(args.approval_id, final_result)

    # 推 IM 第二条 (执行 + 验证结果)
    icon = {
        "success": "✅", "pending": "⏳", "failed": "❗", "skipped": "-",
        "escalate_human": "🚨",
    }.get(validation.get("status"), "?")
    send_message(
        f"{icon} [EXECUTED] approval_id={args.approval_id}\n"
        f"  by: {by}    action: {action}    target: {target}\n"
        f"  执行: {result.get('message') or result.get('reason')}\n"
        f"  验证 ({validation.get('status')}): {validation.get('reason', '')}"
    )


def cmd_deny(args):
    # v2.14: diagnostic_cmd 也走 mark_rejected (同一张表, 逻辑通用)
    diag = get_diagnostic(args.approval_id)
    if diag and diag.get("kind") == "diagnostic_cmd":
        if diag["status"] != "pending":
            _log(f"❌ 当前状态={diag['status']}, 无法拒绝")
            sys.exit(1)
        by = args.by or os.getenv("USER") or getpass.getuser() or "unknown"
        reason = args.reason or "no reason"
        ok, msg = mark_rejected(args.approval_id, by, reason)
        if not ok:
            _log(f"❌ {msg}")
            sys.exit(1)
        p = diag["payload"] or {}
        _log(f"✗ {args.approval_id} (diagnostic_cmd) 已拒绝 by {by}, reason={reason}")
        try:
            send_message(
                f"🚫 [DENIED diagnostic_cmd] task_id={args.approval_id}\n"
                f"  by: {by}\n"
                f"  cmd: {p.get('cmd', '')[:120]}\n"
                f"  reason: {reason}"
            )
        except Exception:
            pass
        return

    # 老 remediation
    rec = get(args.approval_id)
    if not rec:
        _log(f"❌ 未找到 approval_id={args.approval_id}")
        sys.exit(1)
    if rec["status"] != "pending":
        _log(f"❌ 当前状态={rec['status']}, 无法拒绝")
        sys.exit(1)

    by = args.by or os.getenv("USER") or getpass.getuser() or "unknown"
    reason = args.reason or "no reason"
    ok, msg = mark_rejected(args.approval_id, by, reason)
    if not ok:
        _log(f"❌ {msg}")
        sys.exit(1)
    _log(f"✗ {args.approval_id} 已拒绝 by {by}, reason={reason}")
    plan = rec["plan"] or {}
    send_message(
        f"🚫 [DENIED] approval_id={args.approval_id}\n"
        f"  by: {by}    action: {plan.get('action')}    target: {plan.get('target')}\n"
        f"  reason: {reason}"
    )


def main():
    ap = argparse.ArgumentParser(description="AIOps L2 审批 CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="列出待审批操作")

    p_show = sub.add_parser("show", help="查看审批详情")
    p_show.add_argument("approval_id")

    p_app = sub.add_parser("approve", help="批准并执行")
    p_app.add_argument("approval_id")
    p_app.add_argument("--by", help="审批人 (默认从 $USER 获取)")
    p_app.add_argument("--note", default="", help="备注")

    p_deny = sub.add_parser("deny", help="拒绝")
    p_deny.add_argument("approval_id")
    p_deny.add_argument("--by", help="审批人")
    p_deny.add_argument("--reason", default="", help="拒绝原因")

    args = ap.parse_args()

    if args.cmd == "list":
        cmd_list(args)
    elif args.cmd == "show":
        cmd_show(args)
    elif args.cmd == "approve":
        cmd_approve(args)
    elif args.cmd == "deny":
        cmd_deny(args)


if __name__ == "__main__":
    main()
