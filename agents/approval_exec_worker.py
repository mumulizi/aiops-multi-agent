"""审批命令执行 daemon (v2.14 §3).

跟 agents/verifier_worker 同款设计:
- daemon 线程每 5s 扫 SQLite approvals 表
- 拉 kind='diagnostic_cmd' AND status='approved' AND execution_result IS NULL
- 每条: 真跑命令 (无白名单, 但仍有 timeout / 输出截断 / node 白名单校验)
- 写 execution_result, 推第二条 IM, 写 fault_memory.diagnostic_cmd_history

跟 verifier_worker 的区别:
- verifier_worker: 复查 Pod 状态 (只读 K8s API)
- 本 worker: 跑运维手动 approve 后的诊断命令 (真执行, 只是被人审门槛拦过)
"""
import os
import subprocess
import sys
import threading
import time

from tools import approval_store, approval_exec
from tools.safety_guards import record_audit

try:
    from tools.im_notify import send_message
except Exception:
    send_message = None


_started = False
_start_lock = threading.Lock()

LOOP_INTERVAL = int(os.getenv("APPROVAL_EXEC_LOOP_SEC", "5"))

# 命令执行超时 (跟 ssh_tools 一致)
_CMD_TIMEOUT = int(os.getenv("SSH_CMD_TIMEOUT_SEC", "10"))
_SSH_USER = os.getenv("SSH_USER", "root")
_SSH_KEY_PATH = os.getenv("SSH_KEY_PATH", "")
_SSH_STRICT_HOST_CHECK = os.getenv("SSH_STRICT_HOST_CHECK", "no")
_OUTPUT_MAX_BYTES = 4096


def _log(msg):
    print(f"[approval_exec_worker] {msg}", flush=True)
    sys.stdout.flush()


def _truncate(text: str) -> str:
    if not text:
        return ""
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= _OUTPUT_MAX_BYTES:
        return text
    truncated = raw[:_OUTPUT_MAX_BYTES].decode("utf-8", errors="replace")
    return truncated + f"\n[截断, 共 {len(raw)} 字节]"


def _run_ssh(node: str, cmd: str) -> dict:
    """真跑 ssh, 返回 result dict."""
    ssh_args = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", f"StrictHostKeyChecking={_SSH_STRICT_HOST_CHECK}",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", f"ConnectTimeout={min(_CMD_TIMEOUT, 5)}",
        "-o", "LogLevel=ERROR",
    ]
    if _SSH_KEY_PATH:
        ssh_args.extend(["-i", _SSH_KEY_PATH])
    ssh_args.append(f"{_SSH_USER}@{node}")
    ssh_args.append(cmd)

    _log(f"exec ssh {_SSH_USER}@{node} -- {cmd[:120]}")

    try:
        proc = subprocess.run(
            ssh_args, capture_output=True, text=True,
            timeout=_CMD_TIMEOUT, check=False,
        )
        return {
            "exit_code": proc.returncode,
            "stdout_head": _truncate(proc.stdout or ""),
            "stderr_head": _truncate(proc.stderr or ""),
            "executed_at": int(time.time()),
        }
    except subprocess.TimeoutExpired as e:
        partial = ""
        if e.stdout:
            partial = e.stdout if isinstance(e.stdout, str) else \
                      e.stdout.decode("utf-8", errors="replace")
        return {
            "exit_code": -1,
            "stdout_head": _truncate(partial),
            "stderr_head": f"[超时 {_CMD_TIMEOUT}s]",
            "executed_at": int(time.time()),
        }
    except Exception as e:
        return {
            "exit_code": -2,
            "stdout_head": "",
            "stderr_head": f"[ssh 失败] {type(e).__name__}: {e}",
            "executed_at": int(time.time()),
        }


def _run_kubectl_exec(namespace: str, pod: str, cmd: str) -> dict:
    """真跑 kubectl exec, 返回 result dict."""
    args = [
        "kubectl", "exec", "-n", namespace, pod,
        "--", "sh", "-c", cmd,
    ]
    _log(f"exec kubectl exec -n {namespace} {pod} -- {cmd[:120]}")
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True,
            timeout=_CMD_TIMEOUT, check=False,
        )
        return {
            "exit_code": proc.returncode,
            "stdout_head": _truncate(proc.stdout or ""),
            "stderr_head": _truncate(proc.stderr or ""),
            "executed_at": int(time.time()),
        }
    except subprocess.TimeoutExpired as e:
        partial = ""
        if e.stdout:
            partial = e.stdout if isinstance(e.stdout, str) else \
                      e.stdout.decode("utf-8", errors="replace")
        return {
            "exit_code": -1,
            "stdout_head": _truncate(partial),
            "stderr_head": f"[超时 {_CMD_TIMEOUT}s]",
            "executed_at": int(time.time()),
        }
    except FileNotFoundError:
        return {
            "exit_code": -2,
            "stdout_head": "",
            "stderr_head": "[失败] 系统未安装 kubectl",
            "executed_at": int(time.time()),
        }
    except Exception as e:
        return {
            "exit_code": -2,
            "stdout_head": "",
            "stderr_head": f"[kubectl 失败] {type(e).__name__}: {e}",
            "executed_at": int(time.time()),
        }


def _handle_one(task: dict) -> None:
    """处理单个已 approve 的诊断任务."""
    aid = task["id"]
    payload = task.get("payload") or {}
    kind = payload.get("kind", "")
    cmd = payload.get("cmd", "")
    approved_by = task.get("decided_by", "") or ""
    fp = payload.get("fingerprint", "") or ""

    if not cmd:
        _log(f"task {aid}: 空命令, 跳过")
        return

    # 分派执行
    if kind == "ssh":
        node = payload.get("node", "")
        if not node:
            result = {
                "exit_code": -3,
                "stdout_head": "",
                "stderr_head": "[失败] payload 缺 node",
                "executed_at": int(time.time()),
            }
        else:
            result = _run_ssh(node, cmd)
    elif kind == "kubectl_exec":
        ns = payload.get("namespace", "")
        pod = payload.get("pod", "")
        if not ns or not pod:
            result = {
                "exit_code": -3,
                "stdout_head": "",
                "stderr_head": "[失败] payload 缺 namespace/pod",
                "executed_at": int(time.time()),
            }
        else:
            result = _run_kubectl_exec(ns, pod, cmd)
    else:
        result = {
            "exit_code": -3,
            "stdout_head": "",
            "stderr_head": f"[失败] 未知 kind: {kind!r}",
            "executed_at": int(time.time()),
        }

    # 写 SQLite
    try:
        approval_store.mark_diagnostic_executed(aid, result)
    except Exception as e:
        _log(f"task {aid}: SQLite 写结果失败: {e}")

    # 写 fault_memory 历史 (给下次同指纹故障复用)
    if fp:
        try:
            from tools.fault_memory import record_diagnostic_cmd
            record_diagnostic_cmd(
                fingerprint=fp,
                trace_id=payload.get("trace_id", "") or "",
                node=payload.get("node", "") or "",
                namespace=payload.get("namespace", "") or "",
                pod=payload.get("pod", "") or "",
                cmd=cmd,
                reason=payload.get("reason", "") or "",
                exit_code=result.get("exit_code", 0),
                stdout=result.get("stdout_head", "") or "",
                stderr=result.get("stderr_head", "") or "",
                approved_by=approved_by,
                approval_id=aid,
            )
        except Exception as e:
            _log(f"task {aid}: fault_memory 写入失败 (不影响主流程): {e}")

    # 推第二条 IM
    try:
        if send_message is not None:
            msg = approval_exec.format_result_message(
                aid, payload, result, approved_by=approved_by,
            )
            send_message(msg)
    except Exception as e:
        _log(f"task {aid}: IM 推送失败: {e}")

    # 审计
    try:
        record_audit({
            "stage": "approval_exec",
            "trace_id": payload.get("trace_id", ""),
            "task_id": aid,
            "kind": kind,
            "cmd": cmd[:200],
            "target": (payload.get("node") if kind == "ssh"
                       else f"{payload.get('namespace')}/{payload.get('pod')}"),
            "exit_code": result.get("exit_code"),
            "approved_by": approved_by,
            "reason": payload.get("reason", "")[:120],
        })
    except Exception as e:
        _log(f"task {aid}: 审计写入失败: {e}")

    icon = "✅" if result.get("exit_code") == 0 else "❌"
    _log(f"task {aid} {icon} exit={result.get('exit_code')} "
         f"cmd={cmd[:60]}")


# === 主循环 ===

def _loop():
    _log(f"daemon 启动, 每 {LOOP_INTERVAL}s 扫 diagnostic_cmd 审批任务表")
    while True:
        try:
            # 顺手过期 pending 太久的 (超 TTL)
            expired = approval_store.expire_old_diagnostic()
            if expired:
                _log(f"标记 {expired} 条超时 pending 为 expired")

            tasks = approval_store.list_approved_diagnostic_pending(limit=10)
            if tasks:
                _log(f"拉到 {len(tasks)} 个 approved 待执行任务")
            for t in tasks:
                try:
                    _handle_one(t)
                except Exception as e:
                    _log(f"task {t.get('id')} 处理失败: {e}")
        except Exception as e:
            _log(f"主循环异常 (1s 后重试): {e}")
            time.sleep(1)
        time.sleep(LOOP_INTERVAL)


def start() -> bool:
    """启动 daemon 线程. 幂等, 多次调用只起一次."""
    global _started
    with _start_lock:
        if _started:
            return False
        t = threading.Thread(
            target=_loop, name="approval_exec_worker", daemon=True,
        )
        t.start()
        _started = True
    _log("线程已启动 (daemon=True, 主进程退出会自动结束)")
    return True
