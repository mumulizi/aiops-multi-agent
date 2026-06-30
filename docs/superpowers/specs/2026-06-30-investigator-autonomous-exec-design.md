# Investigator 自主执行 (Readonly Tier A) 设计

> 日期: 2026-06-30
> 目标: 给 Investigator 加只读 shell 工具, 让它像 Claude Code 那样自主诊断 — 找问题、自动查日志、跑只读命令、给出实锤根因
> 状态: 设计已确认, 待实现

## 背景

三次生产实跑后发现一个共性短板:

```
[Investigator] 结论: Host 层 NVIDIA 驱动 / NVML 库在 node 192.168.48.9 上未正确加载,
              节点运维需检查驱动安装和 /dev/nvidia* 设备
```

**结论是个建议**, 不是实锤. 运维拿到还得 ssh 上去查 lsmod / dmesg / nvidia-smi, 走一遍 LLM 已经"推测"过的步骤. 闭环没合上.

Claude Code 的 Opus 4.7 / Sonnet 之所以好用, 是因为它能自主跑只读命令 — `cat / grep / ls / ps` — 看实际数据再给结论. 我们的 Investigator 现在只能 `kubectl_describe / get_pod_logs / prometheus_query` 这种间接观测, 拿不到节点本地真相 (内核日志/驱动模块/设备文件/systemd 状态).

## 目标 / 非目标

### 目标
- Investigator 能自主执行**只读** shell 命令: 进 Pod (`kubectl exec`) 或登节点 (`ssh`)
- 节点级故障 (NVML / mount / driver / kernel) 能拿到实锤证据再下结论
- **不打破现有 L3/L2/L4 + Approval Gate 安全分级**

### 非目标 (明确不做)
- 不做任何状态变更 (无 `systemctl restart` / `nvidia-smi -r` / 文件写)
- 不做 FixSuggester (废掉之前讨论的方案 — 诊断够深就不需要再生成"人工命令")
- 不做 ssh write 操作
- 不替换 Approval Gate (修复路径继续走 Remediator → ApprovalGate → Executor)

## 整体分工

```
诊断阶段 (Investigator) — 本期新增能力
  └─ 只读工具
      ├─ kubectl_exec_readonly (进 Pod 排查)
      └─ ssh_node_readonly (登节点排查)
     → 自主执行无门槛 (但有 4 道安全闸)

修复阶段 (Remediator → ApprovalGate → Executor) — 保持现状不动
  └─ L3 自动 / L2 人审 / L4 拒绝 / R3 黑名单 — 全部保留
```

**关键原则**: 现有那套针对"状态变更"的安全防线 (审批 / 速率 / 业务时段 / R3) 已经够用. 不重新造一个 ssh 通道绕过它. 只读的事新加, 写的事走老路.

## §1 新工具实现

### 新建 `tools/ssh_tools.py` (~250 行)

```python
def ssh_run(node: str, cmd: str, *, timeout: int = 10) -> str:
    """登节点跑只读命令. 节点白名单 + 命令白名单双重校验."""

def kubectl_exec_readonly(name: str, namespace: str,
                          cmd: str, *, timeout: int = 10) -> str:
    """进 Pod 跑只读命令. 同样双重校验."""
```

### 命令白名单 (代码硬规则, 不依赖 LLM 自觉)

```python
# 前缀必须命中 (shlex.split 后第一个 token)
READONLY_PREFIXES = {
    # 文件系统只读
    "ls", "cat", "head", "tail", "find", "grep", "stat",
    "pwd", "wc", "du", "df", "file",
    # 进程/系统状态
    "ps", "top", "uptime", "free", "env", "uname", "hostname",
    "id", "whoami", "lsof",
    # 网络只读
    "ip", "route", "netstat", "ss", "nslookup",
    "ping",  # 限 -c
    # 内核 / 硬件
    "dmesg", "lsmod", "lspci", "lsblk", "lscpu",
    # systemd 只读
    "systemctl",  # 只允许 status / show / list-units
    "journalctl",  # 只允许 --no-pager 形式
    # NVIDIA 只读
    "nvidia-smi",  # 不允许 -r / --reset
    "nvcc", "ldconfig",  # 后者只允许 -p
    # kubectl 只读
    "kubectl",  # 限 get / describe / logs / top / version / api-resources
}

# 任何命令包含这些 token 直接拒绝
DANGEROUS_TOKENS = (
    # 重定向 / 写
    ">", ">>", "tee", "dd",
    # 删除 / 修改
    "rm", "rmdir", "mv", "cp ",  # cp 太危险也禁
    "chmod", "chown", "ln ",
    "sed -i", "awk -i",
    # 进程控制
    "kill", "killall", "pkill",
    # 服务变更
    "systemctl start", "systemctl stop", "systemctl restart",
    "systemctl reload", "systemctl enable", "systemctl disable",
    # 包管理
    "apt", "yum", "dnf", "rpm", "dpkg",
    # 编辑器 / 解释器
    "vi ", "vim ", "nano ", "python", "bash -c", "sh -c", "eval",
    # kubectl 写
    "kubectl apply", "kubectl delete", "kubectl edit", "kubectl patch",
    "kubectl create", "kubectl replace", "kubectl rollout",
    "kubectl drain", "kubectl cordon", "kubectl scale",
    # 其他高危
    "nvidia-smi -r", "nvidia-smi --reset",
    "journalctl --rotate", "journalctl --vacuum",
    "modprobe", "rmmod", "insmod",
    "iptables", "nft",
    "shutdown", "reboot", "halt", "poweroff",
)

# subcommand 二级白名单 (前缀通过后还要看第二段)
SUBCMD_WHITELIST = {
    "kubectl": {"get", "describe", "logs", "top", "version",
                "api-resources", "explain"},
    "systemctl": {"status", "show", "list-units", "list-unit-files",
                  "is-active", "is-enabled", "is-failed"},
}
```

### 安全闸 (4 道)

| # | 闸 | 做法 |
|---|----|------|
| 1 | **命令解析** | `shlex.split(cmd)` 解析, 失败直接拒. 拼接 (`;` / `&&` / `\|\|` / `\|`) 必须每段都过白名单 |
| 2 | **前缀+子命令白名单** | 第一个 token 在 `READONLY_PREFIXES`, 有子命令的看 `SUBCMD_WHITELIST` |
| 3 | **dangerous token 黑名单** | 整条命令包含任一 `DANGEROUS_TOKENS` 立即拒 (不管在哪个位置) |
| 4 | **节点白名单** | ssh 目标节点必须出现在 `kubectl get nodes` 列表里, 编造 IP 直接拒 |

### 资源闸 (3 道)

- **超时**: 单命令 10s 强杀 (`subprocess.run(timeout=10)`)
- **输出截断**: stdout/stderr 各最多 4KB, 超过截断
- **速率限制**: 单 `(node, cmd_prefix)` 1 分钟最多 5 次, 复用 `safety_guards.allow(max_per_hour=300)`

### 失败兜底

- ssh 连接失败 → 返回 `[SSH 失败] {error}`, 不抛
- 命令超时 → 返回 `[超时 10s] {partial_output}`
- 白名单拒 → 返回 `[Blocked] 命令未通过只读校验: {reason}` (LLM 看到会换更窄的查询)
- 节点不在白名单 → 返回 `[Blocked] 节点 {node} 不在 K8s nodes 列表`

## §2 注册到 Investigator 工具集

### `tools/mock_tools.py`

```python
from tools.ssh_tools import ssh_run, kubectl_exec_readonly

TOOLS["ssh_node_readonly"] = ssh_run
TOOLS["kubectl_exec_readonly"] = kubectl_exec_readonly

TOOL_DESCRIPTIONS["ssh_node_readonly"] = (
    "登节点跑只读命令排查 Host 层故障. "
    "参数: node (节点名/IP, 必须是真实节点), cmd (只读 shell 命令). "
    "适用: 怀疑 driver/kernel/设备文件/系统服务 类问题. "
    "白名单: ls/cat/df/free/dmesg/journalctl --no-pager/nvidia-smi/lspci/"
    "lsmod/systemctl status/ip/netstat 等. 写操作/服务重启全部拒绝."
)

TOOL_DESCRIPTIONS["kubectl_exec_readonly"] = (
    "在 Pod 里跑只读命令查容器内部状态. "
    "参数: name (pod 名), namespace, cmd (只读 shell 命令). "
    "适用: 看容器内的 /etc/config 实际内容 / env / 进程状态 / 网络. "
    "白名单跟 ssh_node_readonly 一致."
)
```

### `tools/tool_schemas.py`

加 2 个 Function Calling schema, 字段同 description.

## §3 Investigator Prompt 增量

在 `_SYSTEM_TPL` 末尾追加一段:

```
================================================================
六. 自主执行只读命令 (v2.13)
================================================================
你新增了两个能进入 K8s 内部跑只读命令的工具:
- ssh_node_readonly(node, cmd): 登节点, 适合查 Host 层
- kubectl_exec_readonly(name, namespace, cmd): 进 Pod, 适合查容器内部

何时强烈建议用:
- 怀疑 Host 层 (driver/NVML/mount/kernel) → ssh + lsmod / dmesg / ls /dev/* / nvidia-smi
- 怀疑容器内配置错 → kubectl_exec_readonly + cat /etc/xxx/config.yaml
- 怀疑 kubelet/containerd 异常 → ssh + journalctl --no-pager -u kubelet -n 100
- 怀疑设备文件丢 → ssh + ls /dev/nvidia* /dev/nvidia-uvm
- 怀疑内核版本不匹配 → ssh + uname -r && lsmod | grep nvidia

绝对禁止的命令 (会被安全闸拒, 返回 [Blocked]):
- 任何写操作 (rm/mv/cp/sed -i/重定向)
- 服务重启 (systemctl restart/start/stop)
- 包管理 (apt/yum)
- 进程 kill
- kubectl 写操作 (apply/delete/edit)

被 [Blocked] 时不要硬刚, 换更窄的只读查询绕过去.

期望行为对比:
✗ 旧: 看到 panic could not load NVML → 直接 final "Host 层 driver 问题"
✓ 新: 看到 panic → ssh node + lsmod | grep nvidia (空!) + dmesg | grep -i nvidia
      → final "节点 X 的 nvidia.ko 因内核升级到 5.10 未重装 (dmesg 原文: ...)
      节点运维需运行 nvidia-uninstall && 重装 535.86"
```

## §4 环境变量

新增:
- `SSH_USER` (默认 `root`)
- `SSH_KEY_PATH` (默认 `~/.ssh/id_rsa`, 用项目所在机器的 ssh 身份)
- `SSH_STRICT_HOST_CHECK` (默认 `no`, 兼容运维跳板机)
- `SSH_CMD_TIMEOUT_SEC` (默认 10)
- `READONLY_EXEC_ENABLED` (默认 `true`, 一键关掉 ssh/kubectl_exec 两个工具)

## §5 配置文件 (新)

`config/node_aliases.yaml.example`:
```yaml
# 节点别名 → 真实 ssh 目标 (可选, 大部分人用不到)
# 默认从 kubectl get nodes 拿 .metadata.name, 直接 ssh <name>
# 如果 K8s 节点名跟 ssh 目标不一样 (比如 K8s 用 IP, ssh 走 hostname), 这里映射
aliases:
  "192.168.48.9": "gpu-node-9.internal"
```

YAML 不存在 → 直接用 K8s node 名做 ssh 目标. YAGNI 默认.

## §6 期望效果对比 (用上次跑的真实 case)

### Before (这次的 dcgm-exporter 诊断)
```
[Investigator] step 0: kubectl_describe → CrashLoopBackOff, exit_code=1
[Investigator] step 1: get_pod_logs → "Failed to initialize NVML"
[Investigator] step 2: query_history_alerts → 同集群 5 个 Pod 都有 CrashLoop
[Investigator] step 3: final
  结论: Host 层 NVIDIA driver / NVML 库未正确加载, 节点运维需检查驱动安装和 /dev/nvidia*
  → 是建议, 不是实锤
```

### After (加了 ssh_node_readonly)
```
[Investigator] step 0-2: 同上 (从日志拿到 NVML 错)
[Investigator] step 3: ssh_node_readonly(192.168.48.9, "lsmod | grep nvidia")
                       → (空)  # 模块根本没加载
[Investigator] step 4: ssh_node_readonly(192.168.48.9, "ls /dev/nvidia*")
                       → ls: cannot access '/dev/nvidia*': No such file
[Investigator] step 5: ssh_node_readonly(192.168.48.9, "dmesg | grep -i nvidia | tail -20")
                       → [Sun Jun 28] NVRM: nvidia.ko was built with kernel 5.4
                         but running kernel 5.10 — rejected
[Investigator] step 6: ssh_node_readonly(192.168.48.9, "uname -r")
                       → 5.10.0-baidu
[Investigator] step 7: final
  结论: 节点 192.168.48.9 nvidia.ko 因内核从 5.4 升到 5.10 后驱动未重新编译/安装,
        导致模块加载被拒、/dev/nvidia* 设备文件不存在、NVML 初始化失败.
        节点运维需在该节点执行:
          1) nvidia-uninstall (卸旧)
          2) 下载匹配 5.10 内核的 driver 包重装
          3) systemctl restart kubelet (走 L2 审批通道触发)
  置信度: 高
  关键证据 (从工具原文摘):
    - lsmod 中无 nvidia
    - /dev/nvidia* 不存在
    - dmesg: "NVRM: nvidia.ko was built with kernel 5.4 but running kernel 5.10"
    - uname -r = 5.10.0-baidu
```

**从"猜根因 + 建议"到"实锤 + 具体修复路径"**.

## §7 风险点 & 缓解

| 风险 | 缓解 |
|------|------|
| LLM 用单引号包恶意命令绕白名单 | `shlex.split` 解析后逐 token 校验, 不靠字符串 contains |
| ssh 连接挂住主流程 | `subprocess.run(timeout=10)`, 单条强杀 |
| 速率失控刷爆节点 | `safety_guards.allow((node, cmd_prefix), max_per_hour=300)` |
| ssh 凭证泄露 | 走系统级 `~/.ssh/id_rsa`, 不在项目里存 |
| LLM 编造节点名 ssh 出去 | 4 闸: 必须出现在 `kubectl get nodes` |
| 输出过长撑爆 LLM context | stdout/stderr 各截断 4KB |
| 多容器 Pod kubectl exec 选错容器 | 默认选 `container_statuses[0]`, prompt 提示 LLM 可显式指定 |

## §8 实施顺序

按风险递增, 2 个独立 commit:

1. **§1+§2 新工具 + 注册** (~350 行)
   - 重点是白名单 + dangerous token 黑名单 + 4 闸的代码硬规则
   - 单元测试: `tests/test_ssh_tools.py` 测各类绕过攻击都拒
   - 调试: 在你的 stack-ai-qa-5 上用 `python3 -c "from tools.ssh_tools import ssh_run; print(ssh_run('192.168.48.9', 'ls /dev/nvidia*'))"` 验证

2. **§3 Investigator prompt 增量 + e2e** (~50 行)
   - `agents/investigator.py` 加 prompt 段
   - 跑一遍 `main_inspect.py` 看 LLM 是否真的会用这俩工具

## §9 不做 (明确 YAGNI)

- 不接 `systemctl restart` 类: 走现有 L2 Approval Gate (本期不新增 L2 action, 未来真要再加)
- 不持久化 ssh session: 每次新连接, 简化设计
- 不做 FixSuggester: 这个能力让诊断本身够深就够了
- 不做命令历史回放: 现有 Langfuse trace 已经能看
- 不上 ssm/agent 这种中间层 (你的环境 ssh 直连够用)
- 不做命令 dry-run 模拟器 (只读命令本身就接近 dry-run)

## §10 兼容性

- 默认 `READONLY_EXEC_ENABLED=true` (小幅 breaking — 这是 Investigator 行为变化)
- `READONLY_EXEC_ENABLED=false` 一键关掉, 回到 v2.12 行为
- 如果 ssh 没打通 / `~/.ssh/id_rsa` 不存在 → 这俩工具调用返回 `[SSH 失败]`, Investigator 会自动放弃用这些工具, 不影响主流程
