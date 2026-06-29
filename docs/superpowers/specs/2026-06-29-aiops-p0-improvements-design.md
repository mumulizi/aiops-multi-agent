# AIOps Multi-Agent P0 改善设计 (v2.12)

> 日期: 2026-06-29
> 范围: 四项 P0 改善 — 速率限制持久化 / 变更感知工具 / MetricsInspector / Validator 异步化
> 兼容性: 允许小幅 breaking change (`VALIDATOR_ASYNC` 默认 true)
> 状态: 设计已确认, 待实现

## 背景

当前 v2.11 是一个工程实践扎实的"K8s Pod 层自愈系统", 但还不够格做 AIOps 平台:
- 速率限制是进程内存, 重启丢状态 + 多副本不可用
- 没有"变更感知" — RCA 准确率被锁死
- 只看 Pod phase/waiting, 漏掉 Pod Running 但慢/错的故障
- Validator 30s 同步等待阻塞调度

本期 P0 解决这四块。

## 总体策略

- 拆 4 个独立可验证的 commit, 每完成一项可单独跑通验证
- 不引入 Redis / Celery, 保持单进程 + SQLite + daemon thread 的轻量风格
- 数据合并到统一的 `data/aiops.db` (现有 fault_memory.db 不动, 新增 aiops.db 给 rate_limit + verification_tasks)
- 失败兜底优先: 任一新模块 crash 不应阻塞主流程

---

## §1 速率限制迁 SQLite

### 改动

**`tools/safety_guards.py`**
- 新增 SQLite 表 `rate_limit_records(target, action, ts)` 在 `data/aiops.db`
- `allow(target, action, max_per_hour=3)` 改为:
  1. `DELETE FROM rate_limit_records WHERE ts < now-3600`
  2. `SELECT COUNT(*) FROM rate_limit_records WHERE target=? AND action=?`
  3. 满 → 返回 False; 未满 → INSERT 并返回 True
- `_audit_log` 内存 deque 不动 (本期不迁审计日志)
- `get_rate_status()` 改为查 SQLite

### Schema
```sql
CREATE TABLE IF NOT EXISTS rate_limit_records (
  id     INTEGER PRIMARY KEY AUTOINCREMENT,
  target TEXT NOT NULL,
  action TEXT NOT NULL,
  ts     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rate_target_action_ts ON rate_limit_records(target, action, ts);
```

### 接口
`allow / record_audit / get_audit_log / get_rate_status` 签名零改动, 调用方不需要任何修改。

### 验证方式
- 跑一次 main_inspect 触发 L3 自愈 (dry-run)
- kill -9 进程
- 立刻再跑, 同 target 触发 → 应该被限流

---

## §2 变更感知工具

### 改动

**新建 `tools/change_tracker.py`**

```python
def get_recent_changes(namespace: str, hours: int = 2) -> list[dict]:
    """实时查 K8s API 收集近 N 小时变更."""
```

数据源:
1. **Deployments / StatefulSets / DaemonSets**:
   - 拿 `metadata.annotations["deployment.kubernetes.io/revision"]`
   - 看 `status.conditions` 里最新一条 `lastUpdateTime` 是否落在窗口内
2. **ReplicaSets**:
   - `metadata.creationTimestamp` 落在窗口内 = 新版本部署
3. **ConfigMaps / Secrets**:
   - `metadata.creationTimestamp` 或 (有 managedFields 的话) 最新一条 update 时间在窗口内
4. **Events**:
   - `reason in {ScalingReplicaSet, SuccessfulCreate, Killing, FailedCreate, BackOff}`
   - 且 `lastTimestamp` 在窗口内

### 输出
```python
[
  {
    "kind": "Deployment",
    "name": "user-svc",
    "change_type": "image_update | scaled | config_modified | created | deleted",
    "changed_at": "2026-06-29T10:32:00Z",   # ISO8601
    "detail": "revision 5 → 6",
  },
]
```
按 `changed_at` 倒序, 上限 50 条。

### Schema 注册 (`tools/tool_schemas.py`)

```python
{
  "type": "function",
  "function": {
    "name": "get_recent_changes",
    "description": "查询指定 namespace 最近 N 小时的 Deployment/ReplicaSet/ConfigMap/Secret 变更, "
                   "辅助判断故障是否由近期发布/配置变更引起。返回按时间倒序的变更列表 (最多 50 条).",
    "parameters": {
      "type": "object",
      "properties": {
        "namespace": {"type": "string"},
        "hours": {"type": "integer", "default": 2, "minimum": 1, "maximum": 24},
      },
      "required": ["namespace"],
    },
  },
}
```

### `tools/mock_tools.py`
注册到 `TOOLS` 字典:
```python
"get_recent_changes": change_tracker.get_recent_changes,
```
同步更新 `TOOL_DESCRIPTIONS` (给 ReAct 兜底模式用)。

### 错误兜底
- K8s API 失败 → 返回 `[]`, 不抛异常
- 超时 10s

### 不做
- 不接 CI/CD webhook (后续真有需要再加)
- 不持久化变更历史

---

## §3 MetricsInspector

### 改动

**新建 `agents/metrics_inspector.py`**

```python
def run_metrics_inspector() -> list[dict]:
    """跑一组 PromQL 规则, 返回 issue 列表 (跟 Inspector 输出结构对齐)."""
```

**新建 `tools/metrics_rules.py`**

内置 6 条 K8s 通用规则:

| Rule ID | PromQL | severity | Pod 维度 |
|---------|--------|----------|---------|
| `pod_cpu_throttling` | `sum by (pod, namespace) (rate(container_cpu_cfs_throttled_seconds_total[5m])) > 0.5` | high | ✓ |
| `pod_memory_near_limit` | `max by (pod, namespace) (container_memory_working_set_bytes / on(pod, namespace, container) group_left container_spec_memory_limit_bytes) > 0.9` | high | ✓ |
| `node_disk_pressure` | `(node_filesystem_size_bytes{mountpoint="/"} - node_filesystem_avail_bytes{mountpoint="/"}) / node_filesystem_size_bytes{mountpoint="/"} > 0.85` | high | — |
| `node_load_high` | `node_load5 / count by (instance) (node_cpu_seconds_total{mode="idle"}) > 2` | medium | — |
| `apiserver_5xx_high` | `sum(rate(apiserver_request_total{code=~"5.."}[5m])) > 1` | critical | — |
| `kubelet_down` | `up{job="kubelet"} == 0` | critical | — |

### 配置
规则写死在 `_RULES` 字典里 (YAGNI, 后续真有人定制再加 YAML)。

新增 env:
- `METRICS_INSPECTOR_ENABLED` (默认 `true`)
- `PROM_BASE_URL` 复用已有的

### Issue 结构
跟现有 Pod issue 对齐, 新增 `source` / `metric_value` / `metric_query`:
```python
{
  "namespace": "kube-system",
  "pod": "(metric)" or 实际 pod,
  "type": "ApiServer5xxHigh",
  "severity": "critical",
  "summary": "apiserver 5xx 速率 12.3 req/s 超过阈值 1.0",
  "owner_kind": "Unknown",
  "restarts": 0,
  "phase": "",
  "reason": "metric_anomaly",
  "source": "metrics",
  "metric_value": 12.3,
  "metric_query": "sum(rate(...))",
}
```

### 调度器接入 `main_inspect.py`

```python
pod_issues = run_inspector(top_n=None, deep_max_steps=...)
metric_issues = []
if os.getenv("METRICS_INSPECTOR_ENABLED", "true").lower() == "true":
    try:
        metric_issues = run_metrics_inspector()
    except Exception as e:
        _log(f"[MetricsInspector] 失败, 跳过: {e}")
issues = pod_issues + metric_issues
```

去重时 `(ns, type, prefix)` — metric 类的 type 都不一样, 不会跟 Pod 异常误合。

### `_issue_to_alert` 处理 metric 来源
```python
if issue.get("source") == "metrics":
    description = (
        f"指标异常: {issue['metric_query']} 当前值 {issue['metric_value']} "
        f"超过阈值"
    )
```
让 Investigator 拿到时知道这是指标层问题, 优先用 prometheus_query 工具深入。

### Prom 不可达
单条规则查询失败 → 跳过 + 日志一行
整个 MetricsInspector crash → main_inspect catch, 不阻塞 Inspector

---

## §4 Validator 异步化

### 架构图

```
[主流程同步]
  Executor → Validator(轻量, 立即返回 pending_async) → Notifier(派单通知) → END
                       ↓
                       └→ 写 verification_tasks 表

[后台 daemon 线程]
  verifier_worker (每 5s 扫表)
    ↓
  查到期任务 (status=pending and check_at<=now)
    ↓
  _verify_once(task) — 复用 validator 的 _capture_pod_state / _diagnose_restart_futility
    ↓
  更新状态:
    - success / escalate_human → 推第二条 IM, 终止
    - failed + round<3 → 排下一次检查 (30s / 120s / 600s)
    - timeout (round=3 仍未 ok) → 推 IM, 终止
```

### 改动

#### 1. 新建 `tools/verifier_store.py`

```python
DB_PATH = "data/aiops.db"

def init_db(): ...
def enqueue(state: AlertState, plan: dict) -> str: ...  # 返回 task_id
def claim_due(limit: int = 10) -> list[dict]: ...
def update_status(task_id: str, status: str, last_result: dict,
                  next_check_at: int = None, check_round: int = None): ...
def list_pending(limit: int = 50) -> list[dict]: ...    # CLI 用
```

#### 2. 新建表 `verification_tasks`

```sql
CREATE TABLE IF NOT EXISTS verification_tasks (
  task_id       TEXT PRIMARY KEY,
  trace_id      TEXT,
  namespace     TEXT,
  pod           TEXT,
  action        TEXT,
  plan_json     TEXT,
  state_json    TEXT,
  created_at    INTEGER NOT NULL,
  check_at      INTEGER NOT NULL,
  check_round   INTEGER NOT NULL DEFAULT 0,
  status        TEXT NOT NULL DEFAULT 'pending',
  last_result   TEXT,
  updated_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_verif_due ON verification_tasks(status, check_at);
```

#### 3. 新建 `agents/verifier_worker.py`

```python
def start():
    """启动 daemon 线程. 幂等, 多次调用只起一次."""

def _loop():
    while True:
        try:
            tasks = verifier_store.claim_due(limit=10)
            for t in tasks:
                _verify_once(t)
        except Exception as e:
            log("[verifier_worker] loop crash: %s", e)
            time.sleep(1)
        time.sleep(5)

def _verify_once(task):
    """复用 validator.py 现有的检查逻辑.
    成功/逃逸 → 终态 + IM 通知; 失败 + round<3 → 排下次 check_at.
    """
```

#### 4. 改 `agents/validator.py`

新 env: `VALIDATOR_ASYNC` (默认 `true`)

异步路径流程:
1. 不 sleep, 立即调一次 `_capture_pod_state` 拿当前快照
2. 检查 `_diagnose_restart_futility` — 命中 → 立即 `escalate_human` (这个能力保留, 异步无意义)
3. 否则 → `verifier_store.enqueue(state, plan)` + 设 `validation_result = {status: "pending_async", task_id: ...}`
4. 直接 return

同步路径 (`VALIDATOR_ASYNC=false`) 保持原行为, 走旧的 30s sleep。

#### 5. 改 `graph.py`

```python
def _route_after_validator(state):
    result = state.get("validation_result") or {}
    status = result.get("status", "")
    retry_count = state.get("retry_count", 0)
    # 异步路径: pending_async 直接走 notifier (不阻塞)
    if status == "pending_async":
        return "notifier"
    # 同步路径不变
    if status == "failed" and retry_count < _max_retries():
        return "investigator"
    return "notifier"
```

#### 6. 改 `main_inspect.py`

```python
from agents.verifier_worker import start as start_verifier_worker

def main_loop(...):
    if os.getenv("VALIDATOR_ASYNC", "true").lower() == "true":
        start_verifier_worker()
    while True:
        ...
```

#### 7. 改 `agents/notifier.py`

`VALIDATION` 段加 `pending_async` 显示:
```python
_VALIDATION_ICON["pending_async"] = "⏳"
```
显示 "等待异步验证 (3 轮 / 12 分钟覆盖)"

#### 8. 新建 `scripts/aiops_verify_status.py`

```
$ python scripts/aiops_verify_status.py
TASK_ID   AGE     NS/POD              ACTION       ROUND  STATUS          NEXT_IN
abc12345  1m20s   app/svc-xxx-aaa     restart_pod  1/3    pending         +40s
def34567  8m     db/pg-0             restart_sts  3/3    success         -
ghi56789  12m    monitor/proms-0     scale_dep    3/3    timeout         -
```

显示 status=pending 的任务 + 最近 5 个终态任务。

### IM 通知格式

终态触发第二条 IM (复用 `tools/im_notify.format_alert_message`, 加上 verification 段):
```
🔍 修复验证结果
Pod: app/svc-xxx-aaa
Action: restart_pod
Result: success (round 2/3 in 2m10s)
```

### 不打通"异步失败自动重诊"

`failed` 终态触发 IM 告诉运维, 写一条 audit `should_re_diagnose`, 但**不自动调回 LangGraph**:
- 原因 1: 异步线程触发主图工程量大、调试难
- 原因 2: 失败模式多样, 自动重诊容易死循环
- 后续若稳定再开

### 兼容性

- `VALIDATOR_ASYNC=false` 一键回到 v2.11 同步行为
- 同步路径下 v2.3 失败再诊断闭环保留不变
- `validation_result.status` 新增 `pending_async`, 增量字段

### 风险点 & 缓解

1. **K8s client 多线程**: kubernetes-python client 默认线程安全 (v25+), 但确认一遍。若有问题, 用 `threading.local()` 给 worker 单独建 client 实例
2. **SQLite 并发**: 已用 WAL 模式 (fault_memory.py 验证过), 跨线程读写安全
3. **daemon 线程 crash**: 主循环外层 try/except + 1s 退避, 永不退出
4. **进程 kill 时未完成任务**: 重启后 worker 启动会自动 pick up status=pending 任务继续, 但 `check_at` 可能已过期 → 立即重试一次, OK

---

## 实施顺序

按风险递增, 每完成一项独立 commit:

1. **§1 速率限制 (~80 行)** — 改动最小, 接口零变化, 先做练手
2. **§2 变更感知 (~250 行)** — 新工具加入, 不动现有流程
3. **§3 MetricsInspector (~400 行)** — 新 Agent + 接入调度器
4. **§4 Validator 异步化 (~600 行)** — 改 graph + 改 validator + 新 worker

每项完成后:
- 跑一次 `python main_inspect.py` 验证不报错
- 现有 pytest 套件不破坏 (`pytest tests/ -x`)
- README 用一段话补充新能力

## 测试策略

按用户要求 — 不写新单测, 靠 e2e 验证。但确保:
- 现有 210+ 测试不变红 (主要是 `test_validator_futility.py` 跟 §4 相关, 可能要补一个异步路径的 happy path)
- §4 完成后人工跑一遍 dry-run 流水线, 看 verification_tasks 表确实写入了, 30s 后 daemon 把它推到 success

## 不做的事 (明确 YAGNI)

- 不接 Redis / Celery
- 不做 CI/CD webhook
- 不做语义检索 Memory (P1, 后续做)
- 不做服务拓扑 (P1)
- 不做业务影响 Agent (P1)
- 不做异步失败自动重诊
- 不做 MetricsInspector 的 YAML 规则配置

这些都在 ROADMAP 里, 不在本期。
