"""只读 shell 执行工具 (v2.13).

为什么需要:
- Investigator 之前只能 kubectl_describe / get_pod_logs / prometheus_query 这种间接观测
- Host 层故障 (driver / kernel / 设备文件 / systemd) 拿不到实锤
- 加 ssh_node_readonly + kubectl_exec_readonly 后, LLM 能像 Claude Code 那样
  自主跑只读命令再下结论

安全设计 (代码硬规则, 不依赖 LLM 自觉):
- 命令前缀白名单 (READONLY_PREFIXES)
- 子命令二级白名单 (SUBCMD_WHITELIST, 给 kubectl/systemctl 这种多义命令用)
- dangerous token 黑名单 (整条命令含任一立即拒)
- shlex.split 解析后逐 token 检查, 不靠字符串 contains
- 拼接命令 (; && || |) 每段都过白名单
- ssh 节点必须在 kubectl get nodes 列表
- 单命令 10s 超时, 输出截断 4KB, 速率 (node, prefix) 1min 5 次

跟现有 L3/L2/L4 Approval Gate 的分工:
- 这里只管只读诊断
- 任何状态变更 (restart_pod / cordon_node / scale_deployment / systemctl restart)
  都走老路 (Remediator → ApprovalGate → Executor)
- 重启 kubelet 这种 Host 级写操作目前不在自愈范围, 不在本期实现
"""
import os
import re
import shlex
import subprocess
import sys
import time
from typing import Optional

from tools.safety_guards import allow as rate_allow

# === 白名单 ===
# 第一个 token (shlex.split 后) 必须命中
READONLY_PREFIXES = {
    # 文件系统只读
    "ls", "cat", "head", "tail", "find", "grep", "stat",
    "pwd", "wc", "du", "df", "file", "readlink",
    # 进程/系统状态
    "ps", "top", "uptime", "free", "env", "uname", "hostname",
    "id", "whoami", "lsof", "date",
    # 网络只读
    "ip", "route", "netstat", "ss", "nslookup", "dig",
    "ping",  # 限 -c (DANGEROUS 不拦 ping 因 -c 默认带, 但加 max-count 校验)
    # 内核 / 硬件
    "dmesg", "lsmod", "lspci", "lsblk", "lscpu", "lsusb",
    # systemd 只读 (二级白名单进一步过滤)
    "systemctl", "journalctl",
    # NVIDIA 只读
    "nvidia-smi", "nvcc", "ldconfig",
    # kubectl 只读 (二级白名单)
    "kubectl",
    # crictl 容器运行时只读 (节点排查最常用)
    "crictl",
    # 其他
    "echo",  # echo 本身无害, 不过常被组合恶意; dangerous token 黑名单兜底
    "true", "false",
}

# 第一个 token 命中后, 第二个 token 也必须在白名单内
SUBCMD_WHITELIST = {
    "kubectl": {
        "get", "describe", "logs", "top", "version", "explain",
        "api-resources", "api-versions", "auth", "config", "cluster-info",
    },
    "systemctl": {
        "status", "show", "list-units", "list-unit-files", "list-jobs",
        "list-dependencies", "is-active", "is-enabled", "is-failed",
        "cat",  # systemctl cat 看 unit 文件内容, 只读
    },
    "crictl": {
        # 全部只读子命令 (排除 pull/rm/run/start/stop/exec)
        "images", "imagefsinfo", "info", "inspect", "inspecti", "inspectp",
        "logs", "ps", "pods", "stats", "statsp", "version",
    },
}

# 任一 dangerous token 出现在整条命令里 → 立即拒
# 注意: 这里的 token 用单词边界匹配 (re.search r"\b<token>\b"), 不是 substring,
# 否则 'cat' 会被 'at ' 误命中. 多字符串/特殊符号自动 escape.
DANGEROUS_TOKENS = (
    # 重定向 / 写
    ">", ">>", "2>", "2>>",
    "tee",
    "dd",
    # 删除 / 移动 / 复制
    "rm", "rmdir", "mv", "cp",
    # 权限 / 链接
    "chmod", "chown", "chgrp", "ln", "setfacl",
    # 编辑 / 修改 (含 -i 写选项)
    "sed -i", "awk -i",
    "vi", "vim", "nano", "emacs", "ed",
    # 进程控制
    "kill", "killall", "pkill",
    # 服务变更
    "systemctl start", "systemctl stop", "systemctl restart",
    "systemctl reload", "systemctl enable", "systemctl disable",
    "systemctl mask", "systemctl unmask", "systemctl daemon-reload",
    "systemctl reset-failed",
    # 包管理
    "apt", "apt-get", "yum", "dnf", "rpm", "dpkg",
    "pip install", "pip uninstall", "npm install",
    # 编辑器 / 解释器 (代码注入)
    "python", "python3", "ruby", "perl",
    "bash -c", "sh -c", "zsh -c", "fish -c",
    "eval", "source",
    # kubectl 写
    "kubectl apply", "kubectl delete", "kubectl edit", "kubectl patch",
    "kubectl create", "kubectl replace", "kubectl rollout",
    "kubectl drain", "kubectl cordon", "kubectl uncordon",
    "kubectl scale", "kubectl label", "kubectl annotate",
    "kubectl taint", "kubectl run", "kubectl set",
    "kubectl expose", "kubectl autoscale", "kubectl exec",
    "kubectl cp", "kubectl port-forward", "kubectl proxy",
    # GPU 写
    "nvidia-smi -r", "nvidia-smi --reset", "nvidia-smi -ac",
    "nvidia-smi -pm", "nvidia-smi --gpu-reset",
    "nvidia-smi --persistence-mode",
    # journalctl 写
    "journalctl --rotate", "journalctl --vacuum",
    "journalctl --flush", "journalctl --sync",
    # 内核模块
    "modprobe", "rmmod", "insmod",
    # 防火墙
    "iptables", "nft", "ip6tables", "firewall-cmd",
    # 系统级
    "shutdown", "reboot", "halt", "poweroff", "init",
    "swapoff", "swapon", "mount", "umount",
    "mkfs", "fdisk", "parted", "lvm", "lvcreate",
    "useradd", "userdel", "groupadd", "passwd",
    # 计划任务
    "crontab", "at", "batch",
    # 反向 shell / 网络写
    "nc", "ncat", "socat",
    "curl -X POST", "curl -X PUT", "curl -X DELETE",
    "curl --data", "curl -d", "wget --post",
    # docker / containerd 写 (crictl 已经走子命令白名单, 这里只黑 docker/podman/ctr)
    "docker", "podman", "ctr",
)


def _log(msg):
    print(f"[ssh_tools] {msg}", flush=True)
    sys.stdout.flush()


# === 命令校验 ===

def _split_pipeline(cmd: str) -> list:
    """按 ; && || | 拆成多段, 每段单独校验.

    用 shlex 不够 (它不处理 shell 操作符), 这里手动拆.
    """
    # 拆操作符 — 注意 || 要在 | 之前匹配
    segments = [cmd]
    for sep in (";", "&&", "||", "|"):
        new_segments = []
        for seg in segments:
            new_segments.extend(seg.split(sep))
        segments = new_segments
    return [s.strip() for s in segments if s.strip()]


def _check_one_segment(seg: str) -> tuple:
    """校验单段命令. 返回 (ok, reason)."""
    if not seg:
        return False, "空命令"
    try:
        tokens = shlex.split(seg)
    except ValueError as e:
        return False, f"shlex 解析失败 ({e}), 拒绝执行"
    if not tokens:
        return False, "解析后无 token"

    first = tokens[0]
    # 去 PATH 前缀 (/usr/bin/cat → cat)
    if "/" in first:
        first = first.rsplit("/", 1)[-1]

    if first not in READONLY_PREFIXES:
        return False, f"命令 '{first}' 不在只读白名单"

    # 子命令二级白名单
    if first in SUBCMD_WHITELIST and len(tokens) >= 2:
        sub = tokens[1]
        # 跳过 flag 形式 (-h / --help) 找第一个 subcommand
        for t in tokens[1:]:
            if not t.startswith("-"):
                sub = t
                break
        else:
            sub = tokens[1]
        if sub.startswith("-"):
            # 全是 flag, 比如 systemctl --version, 放行
            return True, "ok"
        if sub not in SUBCMD_WHITELIST[first]:
            return False, f"{first} 子命令 '{sub}' 不在白名单 (允许: {sorted(SUBCMD_WHITELIST[first])})"

    return True, "ok"


def _check_command(cmd: str) -> tuple:
    """完整校验. 返回 (ok, reason)."""
    if not cmd or not cmd.strip():
        return False, "命令为空"

    # 1a. 字符级危险符号 (重定向 / 不在单词边界匹配范围)
    # 必须先拆 pipeline 之外的: 这里看的是整条 cmd
    # 注意我们把 | 当成合法管道, 它后面的段会单独校验, 但写文件类重定向都拦
    #
    # v2.13 fix: 区分"写文件的重定向" vs "stderr 重定向到合并/丢弃" 两种情况
    # - 写文件: cmd > /path/file, cmd >> /path/file, cmd 2> /path/file, cmd &> /path/file
    # - 仅丢弃/合并 stderr (诊断常见, 应放行): 2>/dev/null, 2>&1, &>/dev/null
    # 用更精确的模式匹配
    char_redirect_patterns = [
        # > 跟路径 (写文件), 但允许 >&  (>&1 / >&2 是 fd 重定向不写文件) 和 >=  (比较)
        # 也允许 &> 后跟 /dev/null (诊断常见)
        (r"(?<![<>&\d])>(?!=|&|\s*/dev/null\b)", "> 写文件重定向"),
        (r"(?<![<>&\d])>>(?!=)", ">> 追加写文件"),
        # 2> 后必须是 &1 (合并) 或 /dev/null (丢弃) 才放行; 写到具体文件拦
        (r"\b2>(?!&\d|\s*/dev/null\b)", "2> 写文件重定向"),
        # &> 类似
        (r"&>(?!\s*/dev/null\b)", "&> 写文件重定向"),
        (r"`[^`]+`", "反引号命令替换 (绕过校验风险)"),
        (r"\$\([^)]+\)", "$() 命令替换 (绕过校验风险)"),
    ]
    for pattern, desc in char_redirect_patterns:
        if re.search(pattern, cmd):
            return False, f"命令包含禁用符号: {desc}"

    # 1b. dangerous token 黑名单 — 单词边界匹配
    for tok in DANGEROUS_TOKENS:
        pattern = r"\b" + re.escape(tok) + r"\b"
        if re.search(pattern, cmd):
            return False, f"命令包含禁用 token: '{tok}'"

    # 2. 拆段, 每段必须过白名单
    segments = _split_pipeline(cmd)
    if not segments:
        return False, "拆分后无有效段"
    if len(segments) > 5:
        return False, f"管道段数过多 ({len(segments)}), 限 5 段"

    for seg in segments:
        ok, reason = _check_one_segment(seg)
        if not ok:
            return False, f"段 '{seg[:60]}' 拒: {reason}"

    return True, "ok"


# === ssh 节点白名单 ===

_NODE_CACHE = {"nodes": set(), "fetched_at": 0}
_NODE_CACHE_TTL = 60  # 1min 缓存, 避免每次 ssh 查 K8s


def _fetch_live_nodes() -> set:
    """从 K8s 拿当前所有 node 名. 失败返回空集合."""
    now = time.time()
    if _NODE_CACHE["nodes"] and (now - _NODE_CACHE["fetched_at"]) < _NODE_CACHE_TTL:
        return _NODE_CACHE["nodes"]
    try:
        from tools.k8s_tools import _v1, _kube_ok
    except Exception:
        return set()
    if not _kube_ok:
        return set()
    try:
        nodes = _v1.list_node(timeout_seconds=10).items
    except Exception as e:
        _log(f"K8s node 列表查询失败: {e}")
        return _NODE_CACHE["nodes"]  # 返回旧缓存兜底
    names = set()
    for n in nodes:
        names.add(n.metadata.name)
        # 部分集群 K8s node 名是 IP, 部分是 hostname, 都允许
        for addr in (n.status.addresses or []):
            if addr.type in ("InternalIP", "Hostname"):
                names.add(addr.address)
    _NODE_CACHE["nodes"] = names
    _NODE_CACHE["fetched_at"] = now
    return names


def _node_allowed(node: str) -> bool:
    live = _fetch_live_nodes()
    if not live:
        # K8s 不可用兜底: 至少校验格式像合法 hostname/IP
        # (不能完全放过去, 否则 LLM 编造 'example.com' 都能 ssh)
        if not node or len(node) > 253:
            return False
        # IP 或者 K8s 合法 hostname 字符
        import re
        return bool(re.match(r"^[a-zA-Z0-9._-]+$", node))
    return node in live


# === 执行 ===

_SSH_TIMEOUT = int(os.getenv("SSH_CMD_TIMEOUT_SEC", "10"))
_SSH_USER = os.getenv("SSH_USER", "root")
_SSH_KEY_PATH = os.getenv("SSH_KEY_PATH", "")  # 空则用默认 ssh-agent
_SSH_STRICT_HOST_CHECK = os.getenv("SSH_STRICT_HOST_CHECK", "no")
_OUTPUT_MAX_BYTES = 4096
_RATE_LIMIT_PER_MIN = 5


def _truncate(text: str) -> str:
    if not text:
        return ""
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= _OUTPUT_MAX_BYTES:
        return text
    truncated = raw[:_OUTPUT_MAX_BYTES].decode("utf-8", errors="replace")
    return truncated + f"\n[输出截断, 共 {len(raw)} 字节, 仅显示前 {_OUTPUT_MAX_BYTES}]"


def _enabled() -> bool:
    return os.getenv("READONLY_EXEC_ENABLED", "true").lower() == "true"


def _rate_check(node: str, cmd: str) -> tuple:
    """按 (node, 第一个命令 token) 限速, 1min 5 次."""
    try:
        first = shlex.split(cmd)[0]
        if "/" in first:
            first = first.rsplit("/", 1)[-1]
    except Exception:
        first = "_unknown"
    target = f"readonly_exec/{node}"
    action = first
    # 复用 allow, max_per_hour=300 ≈ 1min 5 次的上限
    # (rate_limit 表用的是 1h 窗口, 这里把上限调成 1h 内 5 次更严, 避免刷爆)
    ok, reason = rate_allow(target, action, max_per_hour=5)
    return ok, reason


def ssh_run(node: str, cmd: str) -> str:
    """登节点跑只读命令.

    参数:
      node: K8s 节点名 (必须出现在 kubectl get nodes 列表)
      cmd: 只读 shell 命令

    返回值:
      字符串 (供 LLM 阅读, 含 stdout + stderr 摘要 + 退出码)
      错误时返回 [SSH 失败] / [Blocked] / [超时] 等开头的字符串, 不抛
    """
    if not _enabled():
        return "[Blocked] READONLY_EXEC_ENABLED=false, 该工具已关闭"
    if not node:
        return "[Blocked] node 参数为空"
    if not _node_allowed(node):
        return f"[Blocked] 节点 {node!r} 不在 K8s nodes 列表 (防止编造目标)"

    ok, reason = _check_command(cmd)
    if not ok:
        return f"[Blocked] 命令未通过只读校验: {reason}"

    ok, reason = _rate_check(node, cmd)
    if not ok:
        return f"[Blocked] 速率限制: {reason}"

    # 构造 ssh 命令. -o BatchMode=yes 防止交互卡住
    ssh_args = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", f"StrictHostKeyChecking={_SSH_STRICT_HOST_CHECK}",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", f"ConnectTimeout={min(_SSH_TIMEOUT, 5)}",
        "-o", "LogLevel=ERROR",
    ]
    if _SSH_KEY_PATH:
        ssh_args.extend(["-i", _SSH_KEY_PATH])
    ssh_args.append(f"{_SSH_USER}@{node}")
    # 把命令原样作为单参数传给 ssh, ssh 会在远端 shell 解释
    ssh_args.append(cmd)

    _log(f"ssh {_SSH_USER}@{node} -- {cmd[:120]}")

    try:
        proc = subprocess.run(
            ssh_args,
            capture_output=True,
            text=True,
            timeout=_SSH_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        partial = ""
        if e.stdout:
            partial = e.stdout if isinstance(e.stdout, str) else \
                      e.stdout.decode("utf-8", errors="replace")
        return f"[超时 {_SSH_TIMEOUT}s] node={node} cmd={cmd[:80]}\n部分输出:\n{_truncate(partial)}"
    except Exception as e:
        return f"[SSH 失败] {type(e).__name__}: {e}"

    stdout = _truncate(proc.stdout or "")
    stderr = _truncate(proc.stderr or "")
    rc = proc.returncode

    lines = [f"[ssh {node}] exit_code={rc}"]
    if stdout:
        lines.append(f"--- stdout ---\n{stdout}")
    if stderr:
        lines.append(f"--- stderr ---\n{stderr}")
    if not stdout and not stderr and rc == 0:
        lines.append("(无输出)")
    return "\n".join(lines)


# === kubectl exec readonly ===

def kubectl_exec_readonly(name: str, namespace: str, cmd: str,
                           container: Optional[str] = None) -> str:
    """在 Pod 里跑只读命令.

    参数:
      name: pod 完整名
      namespace: pod 所在 namespace
      cmd: 只读 shell 命令
      container: 可选, 指定容器名 (Pod 多容器时建议指定)

    返回值: 同 ssh_run, 字符串供 LLM 阅读
    """
    if not _enabled():
        return "[Blocked] READONLY_EXEC_ENABLED=false, 该工具已关闭"
    if not name or not namespace:
        return "[Blocked] name 和 namespace 必填"

    ok, reason = _check_command(cmd)
    if not ok:
        return f"[Blocked] 命令未通过只读校验: {reason}"

    target = f"readonly_exec/{namespace}/{name}"
    ok, reason = rate_allow(target, "kubectl_exec", max_per_hour=5)
    if not ok:
        return f"[Blocked] 速率限制: {reason}"

    args = ["kubectl", "exec", "-n", namespace, name]
    if container:
        args.extend(["-c", container])
    # 用 sh -c 跑用户的 cmd. 注意: 这里 sh -c 在远端 Pod 内, 不是绕过校验
    # (cmd 本身已经过白名单, sh -c 只是 kubectl exec 的标准用法)
    args.extend(["--", "sh", "-c", cmd])

    _log(f"kubectl exec -n {namespace} {name} -- {cmd[:120]}")

    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=_SSH_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        partial = ""
        if e.stdout:
            partial = e.stdout if isinstance(e.stdout, str) else \
                      e.stdout.decode("utf-8", errors="replace")
        return f"[超时 {_SSH_TIMEOUT}s] pod={namespace}/{name} cmd={cmd[:80]}\n部分输出:\n{_truncate(partial)}"
    except FileNotFoundError:
        return "[失败] 系统未安装 kubectl 命令"
    except Exception as e:
        return f"[失败] {type(e).__name__}: {e}"

    stdout = _truncate(proc.stdout or "")
    stderr = _truncate(proc.stderr or "")
    rc = proc.returncode

    lines = [f"[kubectl exec {namespace}/{name}] exit_code={rc}"]
    if stdout:
        lines.append(f"--- stdout ---\n{stdout}")
    if stderr:
        lines.append(f"--- stderr ---\n{stderr}")
    if not stdout and not stderr and rc == 0:
        lines.append("(无输出)")
    return "\n".join(lines)


# === v2.14: 需人审的命令 (突破白名单, 走 IM 审批异步执行) ===

def ssh_node_with_approval(node: str, cmd: str, reason: str,
                            trace_id: str = "",
                            fingerprint: str = "") -> str:
    """提交需人审的节点 shell 命令.

    用途: 想跑超出只读白名单的命令 (systemctl restart / crictl pull /
          mount 检查 / 修改任何状态) — 必须由运维 IM approve 后才执行.

    参数:
      node: K8s 节点名
      cmd: 完整 shell 命令 (不过只读白名单, 但过硬黑名单)
      reason: 给运维看的执行理由 (>=10 字, 不能太泛)
      trace_id: (可选) LangGraph trace ID, 关联诊断周期
      fingerprint: (可选) 故障指纹, 用于写 diagnostic_cmd_history 下次复用

    立即返回 [已派单审批 task_id=xxx], LLM 不阻塞.
    运维在 IM 群里 approve 后, daemon 自动跑, 结果进 fault_memory.
    """
    # 校验最低要求: node 必须在 K8s nodes 列表 (跟只读工具一致, 防编造)
    if not node:
        return "[拒] node 必填"
    if not _node_allowed(node):
        return f"[拒] 节点 {node!r} 不在 K8s nodes 列表 (防止编造目标)"

    from tools.approval_exec import submit_diagnostic_approval
    return submit_diagnostic_approval(
        kind="ssh",
        payload={
            "node": node,
            "cmd": cmd,
            "reason": reason,
            "trace_id": trace_id,
            "fingerprint": fingerprint,
        },
    )


def kubectl_exec_with_approval(name: str, namespace: str, cmd: str,
                                reason: str,
                                trace_id: str = "",
                                fingerprint: str = "") -> str:
    """同上, 但在 Pod 内执行. 适用要 kubectl exec 进容器跑诊断的场景.

    参数:
      name: pod 完整名
      namespace: pod 所在 namespace
      cmd: 完整 shell 命令
      reason: 给运维看的执行理由
      trace_id: (可选)
      fingerprint: (可选)
    """
    if not name or not namespace:
        return "[拒] name 和 namespace 必填"
    from tools.approval_exec import submit_diagnostic_approval
    return submit_diagnostic_approval(
        kind="kubectl_exec",
        payload={
            "pod": name,
            "namespace": namespace,
            "cmd": cmd,
            "reason": reason,
            "trace_id": trace_id,
            "fingerprint": fingerprint,
        },
    )
