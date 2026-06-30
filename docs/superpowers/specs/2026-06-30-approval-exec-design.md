# Approval Exec — 模型可申请人审命令 (v2.14)

> 日期: 2026-06-30
> 目标: 让 Investigator 突破白名单, 申请人审命令; 运维 IM 回 approve 后异步执行;
>       结果写 fault_memory.diagnostic_cmd_history 形成学习闭环
> 状态: 设计已确认, 待实现

## 背景

用户反馈: "为什么 LLM 不能像 Claude Code 那样自己判断? 现在白名单挡了它的手脚."

回答清楚边界后, 用户认可正确做法是**让模型敢于提出高危操作, 但每次都过人审**.
这就是本期要实现的能力.

## 架构

```
[Investigator (DeepSeek/Qwen)]
  ssh_node_with_approval(node, cmd, reason)
  kubectl_exec_with_approval(name, namespace, cmd, reason)
      ↓
[硬黑名单检查] rm/dd/mkfs/shutdown/iptables -F/kubectl delete --all → 直接拒, 不入审批
      ↓
[approval_exec.submit_diagnostic_approval]
  → 写 SQLite approval_pending (kind='diagnostic_cmd')
  → 推 IM @运维 "申请执行节点命令"
  → 立即返回 [已派单审批 task_id=xxx], LLM 不阻塞
      ↓
[Investigator] 基于现有证据先 final, 不等

═══════════════════ 异步分割线 ═══════════════════

[运维 IM 回 approve <task_id>]
  → scripts/aiops_review.py 改 status='approved'
      ↓
[approval_exec_worker daemon, 每 5s 扫表]
  → 拉 status='approved' AND kind='diagnostic_cmd'
  → subprocess.run (无白名单, 仅 10s timeout + 输出 4KB)
  → 写 execution_result
  → 推第二条 IM "执行结果"
  → fault_memory.record_diagnostic_cmd(fp, cmd, result)
      ↓
[下次同指纹故障]
  → Investigator 自动看 diagnostic_cmd_history
  → "上次审批过 X 命令, 结果是 Y, 这次直接 final"
```

## 关键设计原则

1. **复用现有 L2 Approval Gate 基础设施** (SQLite approval_pending + aiops_review CLI),
   不另起一套
2. **异步不阻塞**: 派单后 Investigator 立即拿到提示, 用现有证据 final, 审批结果进 Memory
3. **硬黑名单永远拒**: rm/dd/mkfs 等不可逆操作不进审批通道
4. **学习闭环**: 审批成功的命令进 `diagnostic_cmd_history`, 同指纹故障复用

## §1 数据库 schema 扩展

### `approval_pending` 表加 3 列

```sql
ALTER TABLE approval_pending ADD COLUMN kind TEXT DEFAULT 'remediation';
-- 'remediation' (现有 plan 审批) | 'diagnostic_cmd' (新, 诊断命令审批)
ALTER TABLE approval_pending ADD COLUMN cmd_payload TEXT;
-- JSON: {node, cmd, reason, trigger="ssh"|"kubectl_exec",
--        namespace?, pod?, trace_id, fingerprint}
ALTER TABLE approval_pending ADD COLUMN execution_result TEXT;
-- 审批通过执行后写, JSON: {exit_code, stdout_head, stderr_head, executed_at}
```

### 新建 `diagnostic_cmd_history` 表 (在 `data/aiops.db`)

```sql
CREATE TABLE IF NOT EXISTS diagnostic_cmd_history (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  fingerprint TEXT NOT NULL,           -- 跟 fault_memory 指纹同一套生成方式
  trace_id    TEXT,
  node        TEXT,
  namespace   TEXT,
  pod         TEXT,
  cmd         TEXT NOT NULL,
  reason      TEXT,
  exit_code   INTEGER,
  stdout_head TEXT,                    -- 截断到 4KB
  stderr_head TEXT,
  approved_by TEXT,
  approval_id TEXT,
  executed_at INTEGER NOT NULL
);
CREATE INDEX idx_diag_fp ON diagnostic_cmd_history(fingerprint, executed_at);
```

## §2 新建 `tools/approval_exec.py` (~150 行)

```python
def submit_diagnostic_approval(
    *, kind: str = "ssh" | "kubectl_exec",
    payload: dict,    # {node/pod/namespace, cmd, reason, trace_id, fingerprint}
    ttl_sec: int = 1800,
) -> str:
    """写 SQLite + 推 IM, 返回 task_id (8 位短 ID)."""

def execute_approved_cmd(task_id: str) -> dict:
    """daemon 调用. 真跑命令, 写 execution_result + diagnostic_cmd_history,
    推 IM, 返回 dict."""

def list_approved_diagnostic_pending() -> list:
    """daemon 拉到期任务."""

def format_diagnostic_approval_message(task_id: str, payload: dict,
                                        ttl_min: int) -> str:
    """IM 第一条派单消息. 复用 im_notify."""

def format_diagnostic_result_message(task_id: str, payload: dict,
                                      result: dict) -> str:
    """IM 第二条执行结果消息."""
```

## §3 新建 `agents/approval_exec_worker.py` (~150 行)

跟 verifier_worker 同款 daemon 线程:

```python
def start() -> bool:
    """幂等启动, 多次调用只起一次."""

def _loop():
    while True:
        try:
            tasks = approval_exec.list_approved_diagnostic_pending(limit=10)
            for t in tasks:
                _handle_one(t)
        except Exception as e:
            _log(f"主循环异常 (1s 后重试): {e}")
            time.sleep(1)
        time.sleep(5)

def _handle_one(task: dict) -> None:
    """跑命令 → 写结果 → 推 IM → 写 fault_memory."""
```

## §4 新工具: `tools/ssh_tools.py` 加 2 个

```python
def ssh_node_with_approval(node: str, cmd: str, reason: str) -> str:
    """提交需人审的节点 shell 命令.

    用途: 想跑超出只读白名单的命令 (systemctl restart / crictl pull /
          mount 检查 / 修改任何状态) — 必须由运维 IM approve 后才执行.

    硬黑名单 (永不入审批): rm / dd / mkfs / fdisk / shutdown / reboot /
    halt / iptables -F / kubectl delete --all / drop database / /dev/sd 直写

    返回值: 立即返回 [已派单审批 task_id=xxx ...], LLM 不阻塞, 异步执行,
            结果进 fault_memory.diagnostic_cmd_history 给下次复用.
    """

def kubectl_exec_with_approval(name: str, namespace: str, cmd: str,
                                reason: str) -> str:
    """同上, 但在 Pod 内执行. 适用要 kubectl exec 进容器跑诊断."""
```

### 硬黑名单

```python
HARD_BLACKLIST_TOKENS = (
    # 数据销毁
    "rm ", "rm\t", "rm\n", "rm -",
    "dd if=", "dd of=",
    "mkfs", "fdisk", "parted", "wipefs",
    # 文件系统块设备
    "> /dev/sd", "> /dev/nvme", "> /dev/vd",
    "of=/dev/sd", "of=/dev/nvme", "of=/dev/vd",
    # 系统断电
    "shutdown", "reboot", "halt", "poweroff", "init 0", "init 6",
    # 防火墙清空 (会断网)
    "iptables -F", "iptables --flush",
    "iptables -X", "iptables --delete-chain",
    "nft flush",
    # K8s 批量删
    "kubectl delete --all",
    # SQL 销毁
    "drop database", "drop table", "truncate table",
    # Fork bomb
    ":(){:|:&};:",
    # 镜像/容器批量删
    "docker system prune", "crictl rmi --all", "docker rmi --force",
)
```

任一命中 → 立即返回 `[硬黑名单拒] 这类不可逆操作不进审批通道, 必须人工 ssh 跑`, 不写 SQLite, 不推 IM.

### IM 派单消息格式

```
🤖 [AIOps 诊断助手] 申请执行节点命令
TaskID:  abc12345
Trace:   cycle-67249d69-7
Node:    192.168.48.9
Cmd:     crictl pull baremetal-operator:9b07599
Reason:  验证镜像仓库可达 + 拉取是否成功

回复:
  approve abc12345        → 同意执行
  deny abc12345 不必要    → 拒绝
TTL: 30min
```

### IM 执行结果消息格式

```
🤖 [AIOps 诊断助手] 命令执行结果
TaskID:    abc12345
Node:      192.168.48.9
Cmd:       crictl pull baremetal-operator:9b07599
ExitCode:  1
Stderr:    rpc error: code = NotFound desc = failed to pull and unpack ...
ApprovedBy: opsuser
→ 已写入 fault_memory, 下次同指纹故障 (baremetal-operator/ImagePullBackOff)
  可秒级复用
```

## §5 注册到 Investigator 工具集

`tools/mock_tools.py` + `tools/tool_schemas.py` 加 2 个新工具.

schema 中明确写清:
- 当只读白名单不够用时使用
- 必须提供 `reason` (一句话)
- 返回 `[已派单审批]` 立即提示, LLM 应基于现有证据 final
- 硬黑名单永远拒

## §6 Investigator prompt 增量 (第七节)

```
================================================================
七. 申请人审命令 (v2.14)
================================================================
当只读白名单不够用时, 你可以用 ssh_node_with_approval / kubectl_exec_with_approval
提交人审请求. 运维在 IM 群里 approve 后, daemon 真跑命令, 结果进 fault_memory,
下次同指纹故障可秒级复用.

何时申请:
- 只读白名单挡了关键诊断 (例: crictl pull 验证镜像可达)
- 需要轻量状态变更才能诊断 (例: systemctl restart kubelet 后再观察)
- 必须有明确假设 + 验证理由, 不要"试一下"
- reason 必须一句话写清"为什么要跑 + 期望验证什么"

调用后立即返回 [已派单审批 task_id=xxx], **本轮拿不到这条证据**.
你应该:
1. 基于现有证据先 final 一个临时结论 (置信度"中", 注明缺什么证据)
2. 运维审批通过后, daemon 自动跑 + 结果进 Memory

硬黑名单 (永远不入审批通道, 别试):
- rm/dd/mkfs/fdisk/shutdown/reboot/iptables -F/kubectl delete --all
- 数据销毁/系统断电/批量删 这种"出了事没法回滚"的操作

期望行为:
✗ 看到 ImagePullBackOff → 直接 final "镜像不存在" 不申请验证 (失之保守)
✓ 看到 ImagePullBackOff → 申请 crictl pull <image> 验证, 同时基于
   现有 events final 临时结论
```

## §7 scripts/aiops_review.py 扩展

现有 CLI 支持 list/show/approve/deny, 需要:
- `list` 时区分显示 kind (remediation / diagnostic_cmd) 用不同图标
- `show` 时对 diagnostic_cmd 显示 cmd_payload + execution_result
- `approve` 时 daemon 自动 pick up 跑命令, 不需要 CLI 真跑

## §8 fault_memory 扩展

```python
def record_diagnostic_cmd(
    fingerprint: str, trace_id: str,
    node: str = "", namespace: str = "", pod: str = "",
    cmd: str = "", reason: str = "",
    exit_code: int = 0, stdout: str = "", stderr: str = "",
    approved_by: str = "", approval_id: str = "",
) -> None:
    """审批执行后写 diagnostic_cmd_history."""

def list_diagnostic_history(fingerprint: str, limit: int = 5) -> list:
    """Investigator 拉同指纹历史 (按 executed_at 倒序)."""
```

Investigator 在 retry_count=0 命中 fault_memory 后, **同时**查 diagnostic_cmd_history,
把历史命令 + 结果一并塞进 user_msg 给 LLM 看:

```
此故障曾审批执行过以下命令:
[2026-06-28] crictl pull foo:bar (approved by opsuser1)
  → exit=1, stderr=NotFound
[2026-06-29] kubectl exec dcgm-exporter ldconfig -p (approved by opsuser2)
  → exit=0, stdout=...

根据历史, 可能根因是: ... 你可以直接 final 或申请新命令排查.
```

## §9 main_inspect.py 启动 daemon

```python
if __name__ == "__main__":
    ...
    # 启动两个 daemon
    if os.getenv("VALIDATOR_ASYNC", "true").lower() == "true":
        from agents.verifier_worker import start as start_verifier
        start_verifier()
    if os.getenv("APPROVAL_EXEC_ENABLED", "true").lower() == "true":
        from agents.approval_exec_worker import start as start_approval_exec
        start_approval_exec()
    ...
```

## 风险点 & 缓解

| 风险 | 缓解 |
|------|------|
| 运维被 IM 轰炸 | 单 (trace_id, node) 1h 内最多 3 个审批请求, 复用 safety_guards.allow |
| LLM 滥用审批通道 | reason 字段强制非空, 太短 (< 10 字) 拒 |
| TTL 内未审批 → 任务 pending 堆积 | 30min TTL, 到期自动 status='expired' |
| 审批通过命令失败 | 写 execution_result 含 exit_code, IM 推真实结果 |
| 命令跑挂阻塞 daemon | 10s timeout 强杀 (同 ssh_tools) |
| 多个并发审批 | task_id uuid 唯一, daemon 串行处理 |
| 历史命令 SQL 注入 | sqlite3 参数化全程, 不拼字符串 |

## 不做 (YAGNI)

- 运维在 IM 里改命令再 approve (太复杂, 下版本)
- 自动同类故障复用 (本期只记录, Investigator 主动查; 自动复用容易出错)
- 审批人 RBAC (你的环境运维都信任)
- web UI 审批 (CLI + IM 够用)
- ssh 写命令安全等级再分级 (硬黑名单 + 人审通道二级足够)

## 实施顺序

按风险递增, 4 个独立 commit:

1. **§1 数据库 schema 扩展** (~80 行)
   - `tools/approval_store.py` 加 kind/cmd_payload/execution_result 列
   - `tools/fault_memory.py` 加 diagnostic_cmd_history 表 + 读写函数
   - 验证: 旧的 remediation 审批不受影响

2. **§2+§4 approval_exec 工具 + ssh_tools 加 with_approval 函数** (~250 行)
   - 不接 daemon, 先跑通"派单 + IM 推送"
   - 验证: 调 ssh_node_with_approval 应该返回 task_id, SQLite 有记录, IM 收到消息

3. **§3 daemon worker + §7 aiops_review 显示** (~200 行)
   - 启动 daemon 拉到 approve 任务, 真跑命令, 写结果, 推第二条 IM
   - 验证: CLI approve 一个 task_id, 5s 后看 IM 收到执行结果

4. **§5+§6+§8+§9 Investigator 集成** (~150 行)
   - 工具注册 + prompt 第七节 + main_inspect 启动 daemon + Memory 接 diagnostic_cmd_history
   - 验证: 跑一轮 main_inspect, LLM 应该在合适时主动调 with_approval

## 兼容性

- 默认 `APPROVAL_EXEC_ENABLED=true` (允许小幅 breaking)
- false 一键关掉 (新工具仍存在, 但调用返回 [已禁用])
- 现有 L2 Approval Gate (remediation 审批) 不受影响
- 现有 ssh_node_readonly / kubectl_exec_readonly 不动
- 新增 env: `APPROVAL_EXEC_ENABLED`, `APPROVAL_EXEC_TTL_SEC` (默认 1800)

## 测试策略

按用户之前要求 — 不写新单测, 靠 e2e 验证.

人工测试:
- 派单流: 调 ssh_node_with_approval('192.168.48.9', 'crictl pull foo:bar',
  '验证仓库可达') → SQLite 应有记录 + IM 有消息
- 审批流: scripts/aiops_review.py approve <task_id> → daemon 应在 5s 内 pick up
- 执行流: 命令应在节点上真跑 (10s 超时), 结果回写
- 学习流: fault_memory.list_diagnostic_history(fp) 应能拉到历史
- 拒绝流: deny <task_id> → daemon 跳过, IM 推"已拒绝"
- 黑名单流: 'rm -rf /tmp' 直接拒, 不入审批
- TTL 流: 30min 不审批 → 自动 expired
