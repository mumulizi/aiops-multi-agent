"""诊断命令审批入口 (v2.14).

给 Investigator 提供 submit_diagnostic_approval() 接口, 让 LLM 能申请人审命令:
- 写 SQLite approval_pending (kind='diagnostic_cmd')
- 推 IM 派单消息 @运维
- 立即返回给 LLM 一个 [已派单审批 task_id=xxx] 提示

真正的执行由 agents/approval_exec_worker daemon 异步完成 (下一个 commit 实现).
本模块只负责"入队 + 通知", 不跑命令.
"""
import os
import sys
from typing import Optional

from tools import approval_store
from tools.safety_guards import allow as rate_allow

try:
    from tools.im_notify import send_message
except Exception:
    send_message = None


def _log(msg):
    print(f"[approval_exec] {msg}", flush=True)
    sys.stdout.flush()


# === 硬黑名单: 永远不入审批通道, 直接拒 ===
# 这类操作不可逆或影响面太大 (数据销毁 / 系统断电 / 批量删除 / 防火墙断网),
# 就算运维 approve 也不该由 AIOps 代跑, 必须人工手动 ssh 执行留完整审计痕迹.
HARD_BLACKLIST_TOKENS = (
    # 数据销毁 (rm 用单词边界匹配, 避免误伤 "systemctl status rms" 之类的)
    "rm ", "rm\t", "rm\n", "rm -",
    "dd if=", "dd of=",
    "mkfs", "fdisk", "parted", "wipefs",
    # 直接写块设备
    "> /dev/sd", "> /dev/nvme", "> /dev/vd",
    "of=/dev/sd", "of=/dev/nvme", "of=/dev/vd",
    # 系统断电
    "shutdown", "reboot", "halt", "poweroff",
    "init 0", "init 6",
    # 防火墙清空 (会断网)
    "iptables -F", "iptables --flush",
    "iptables -X", "iptables --delete-chain",
    "nft flush",
    # K8s 批量删
    "kubectl delete --all",
    "kubectl delete namespace",
    # SQL 销毁
    "drop database", "drop table", "truncate table",
    # Fork bomb
    ":(){:|:&};:",
    # 大规模清理
    "docker system prune", "crictl rmi --all",
    "docker rmi --force",
)


def _check_hard_blacklist(cmd: str) -> Optional[str]:
    """返回 None 表示通过, 返回字符串表示命中的黑名单 token."""
    if not cmd:
        return "cmd 为空"
    # 大小写不敏感检查, 但保留原始 token 大小写 (iptables -F 命中即 -F 大写)
    cmd_lower = cmd.lower()
    for tok in HARD_BLACKLIST_TOKENS:
        if tok.lower() in cmd_lower:
            return tok
    return None


def _check_reason(reason: str) -> Optional[str]:
    """reason 校验: 必须非空 + 至少 10 字 + 不能太泛."""
    if not reason:
        return "reason 必填 (给运维看的执行理由)"
    reason_stripped = reason.strip()
    if len(reason_stripped) < 10:
        return f"reason 太短 ({len(reason_stripped)} 字), 至少 10 字, 说清'为什么要跑 + 期望验证什么'"
    # 拒绝几个"我想试试"类的空话
    for banned in ("试一下", "看看", "查一下", "just checking", "try it", "试试"):
        if reason_stripped == banned or reason_stripped == f"{banned}."\
                or reason_stripped == f"{banned}。":
            return f"reason 太泛 ('{banned}'), 请写清具体假设 + 期望验证什么"
    return None


def submit_diagnostic_approval(
    *, kind: str, payload: dict, ttl_sec: Optional[int] = None,
) -> str:
    """提交一条诊断命令审批. 返回给 LLM 的字符串 (立即返回, 不阻塞).

    参数:
      kind: "ssh" 或 "kubectl_exec"
      payload: 必含 cmd/reason, ssh 场景含 node, kubectl_exec 场景含 name+namespace
               可选: trace_id (用于关联), fingerprint (用于历史命令复用)
      ttl_sec: 默认 APPROVAL_EXEC_TTL_SEC (env, 默认 1800)

    错误情况全部返回给 LLM 可读的字符串, 不抛异常.
    """
    if os.getenv("APPROVAL_EXEC_ENABLED", "true").lower() != "true":
        return "[已禁用] APPROVAL_EXEC_ENABLED=false, 该工具已关闭"

    cmd = payload.get("cmd", "") or ""
    reason = payload.get("reason", "") or ""

    # 1. 硬黑名单
    hit = _check_hard_blacklist(cmd)
    if hit:
        return (
            f"[硬黑名单拒] 命令包含 '{hit.strip()}' — 这类不可逆操作不进审批通道, "
            f"必须由运维人工 ssh 跑, 留完整审计. AIOps 不代跑."
        )

    # 2. reason 校验
    reason_err = _check_reason(reason)
    if reason_err:
        return f"[拒] {reason_err}"

    # 3. kind 校验
    if kind not in ("ssh", "kubectl_exec"):
        return f"[拒] kind 必须是 'ssh' 或 'kubectl_exec', 收到 {kind!r}"

    # 4. 参数完整性
    if kind == "ssh":
        node = payload.get("node", "") or ""
        if not node:
            return "[拒] kind=ssh 必须提供 node"
        target = f"node:{node}"
    else:  # kubectl_exec
        pod = payload.get("pod", "") or ""
        ns = payload.get("namespace", "") or ""
        if not pod or not ns:
            return "[拒] kind=kubectl_exec 必须提供 name (pod 名) 和 namespace"
        target = f"pod:{ns}/{pod}"

    # 5. 速率限制: 单 (trace_id, target) 1h 内最多 3 条审批请求
    # 用 trace_id 而不是节点名, 保证同一次诊断周期不会把同 target 派多次
    trace_id = payload.get("trace_id", "") or "no_trace"
    rate_key_target = f"approval_exec/{trace_id}"
    ok, rate_reason = rate_allow(rate_key_target, target, max_per_hour=3)
    if not ok:
        return f"[速率限制] {rate_reason} — 本轮诊断该目标已多次申请, 请基于现有证据 final"

    # 6. 写 SQLite
    try:
        aid = approval_store.create_diagnostic_pending(
            payload={
                "kind": kind,
                "cmd": cmd,
                "reason": reason,
                "trace_id": trace_id,
                "fingerprint": payload.get("fingerprint", ""),
                # kind 特定字段
                "node": payload.get("node", ""),
                "namespace": payload.get("namespace", ""),
                "pod": payload.get("pod", ""),
            },
            ttl_sec=ttl_sec,
        )
    except Exception as e:
        return f"[派单失败] SQLite 写入错: {type(e).__name__}: {e}"

    _log(f"新审批 task_id={aid} kind={kind} target={target} reason={reason[:60]}")

    # 7. 推 IM
    try:
        msg = format_approval_message(aid, kind, payload)
        push_result = send_message(msg) if send_message else {"im_sent": False}
    except Exception as e:
        push_result = {"im_sent": False, "error": str(e)}

    im_status = "已推送" if push_result.get("im_sent") else "IM 未推送 (仍需 CLI approve)"
    ttl_min = (ttl_sec or approval_store.DEFAULT_TTL_SEC) // 60

    # 8. 返回给 LLM 的立即响应
    return (
        f"[已派单审批] task_id={aid}\n"
        f"  目标: {target}\n"
        f"  命令: {cmd}\n"
        f"  理由: {reason}\n"
        f"  TTL: {ttl_min}min ({im_status})\n"
        f"  → 运维审批后异步执行, 结果进 fault_memory 给下次复用\n"
        f"  → 本轮诊断拿不到这条证据, 请基于现有证据 final"
    )


# === IM 消息渲染 ===

def format_approval_message(aid: str, kind: str, payload: dict) -> str:
    """派单消息 (IM 第一条)."""
    cmd = payload.get("cmd", "")
    reason = payload.get("reason", "")
    trace_id = payload.get("trace_id", "")
    if kind == "ssh":
        target_line = f"Node:    {payload.get('node', '')}"
    else:
        target_line = f"Pod:     {payload.get('namespace', '')}/{payload.get('pod', '')}"
    return (
        f"🤖 [AIOps 诊断助手] 申请执行诊断命令\n"
        f"TaskID:  {aid}\n"
        f"Trace:   {trace_id}\n"
        f"{target_line}\n"
        f"Cmd:     {cmd}\n"
        f"Reason:  {reason}\n"
        f"\n"
        f"回复:\n"
        f"  approve {aid}       → 同意执行 (daemon 5s 内 pick up)\n"
        f"  deny {aid} <原因>   → 拒绝\n"
        f"TTL: 30min"
    )


def format_result_message(aid: str, payload: dict, result: dict,
                           approved_by: str = "") -> str:
    """执行结果消息 (IM 第二条)."""
    cmd = payload.get("cmd", "")
    exit_code = result.get("exit_code", "?")
    stdout_head = (result.get("stdout_head") or "").strip()
    stderr_head = (result.get("stderr_head") or "").strip()

    if payload.get("kind") == "ssh":
        target_line = f"Node:      {payload.get('node', '')}"
    else:
        target_line = (
            f"Pod:       {payload.get('namespace', '')}/{payload.get('pod', '')}"
        )

    icon = "✅" if exit_code == 0 else "❌"
    lines = [
        f"🤖 [AIOps 诊断助手] {icon} 命令执行结果",
        f"TaskID:    {aid}",
        target_line,
        f"Cmd:       {cmd}",
        f"ExitCode:  {exit_code}",
    ]
    if stdout_head:
        lines.append(f"Stdout:    {stdout_head[:600]}")
    if stderr_head:
        lines.append(f"Stderr:    {stderr_head[:600]}")
    if approved_by:
        lines.append(f"ApprovedBy: {approved_by}")
    if payload.get("fingerprint"):
        lines.append(
            f"→ 已写入 fault_memory (fp={payload['fingerprint'][:8]}), "
            f"下次同指纹故障可秒级复用"
        )
    return "\n".join(lines)
