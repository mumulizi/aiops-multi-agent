# AIOps Multi-Agent — LLM-Powered Kubernetes 自主巡检与根因诊断系统

> 基于 LangGraph 的多 Agent AIOps 平台, 主动巡检 Kubernetes 集群、自主发现异常、
> 调用工具定位真实根因。从"被动接告警"升级为"主动找问题"。

## 项目亮点

- **Agentic AIOps**: Inspector Agent 主动巡检集群, 自主决定查什么, 不依赖外部告警
- **生产监控接入**: 接入 VictoriaMetrics multi-tenant 监控栈与真实 K8s API
- **混合决策**: 异常清单走规则强制收集(无遗漏), Top N 选择/根因分析走 LLM
- **代码兜底**: LLM 任意环节失败都不会丢失已收集证据, 保守 hypothesis 兜底
- **真根因诊断**: 调用 Pod 日志(含 previous 崩溃前日志) + Prometheus + K8s API 三重证据
- **同类去重 + 高覆盖**: 单次巡检 LLM 调用从 27+ 次降到 ~10 次, 异常覆盖率 18% → ~100%
- **Langfuse 全链路追踪**: 所有 Agent / 工具 / LLM 调用的 prompt / completion / token / 耗时全程可视化, 支持失败重放与性能分析
- **自愈闭环 (v2.0)**: 9 节点完整流水线 (Inspector → Triage → Aggregator → Classifier → Investigator → **Remediator → ApprovalGate → Executor → Validator** → Notifier);
  L1-L4 安全分级 + 4 层安全保险 (大开关 / dry-run / 速率限制 / 业务时段); 三重防护避免 LLM 失误执行高风险动作
- **5 个修复动作**: `delete_evicted_pod` / `delete_completed_job_pod` / `restart_pod` (RS+DaemonSet) / `restart_pod_for_image_pull` / `delete_failed_pod` —
  覆盖 80%+ 常见 K8s 异常类型 (CrashLoopBackOff / ImagePullBackOff / OOMKilled / Evicted / Failed)
- **L2 异步审批 (CLI)**: SQLite 持久化待审批操作 (TTL 30min) + IM 推送审批指令 + CLI 工具 (`scripts/aiops_review.py`) 远程审批; 审批通过后双重校验 + 30s Validator 自动验证 + 第二条 IM 推执行结果
- **describe_pod_real 完整字段采集**: 覆盖 `waiting.message` / `last_terminated.message` / `conditions.message` / `pod.status.message` / `init_containers` / `images` 等 8 类字段, 拿不到日志的 Pod (Pending/ImagePull) 也能定位真实根因
- **IM 通知 + 本地审计**: 通用 IM 协议适配层 (如流 / 钉钉 / 企微 / 飞书 一键切换);
  关键事件自动推送群机器人 (critical/high/已执行/被拒/待审批); 同时写本地审计文件 (alerts/) 防 IM 故障丢消息
- **本地 LLM**: Qwen2.5-7B 在 2 卡 Tesla T4 上 vLLM TP=2 部署, OpenAI 兼容接口

## 架构

```
生产 K8s 集群                                       本地 GPU 节点
├── VictoriaMetrics (multi-tenant)                  ├── vLLM 容器 (Qwen2.5-7B)
├── kube-apiserver                                  └── Agent 项目 (本仓库)
└── 异常 Pod (CrashLoopBackOff / OOMKilled / ...)
         │
         ▼
  ┌─────────────────────────────────────────────────────────────────┐
  │  阶段 1: Inspector 主动巡检 (代码强制 + LLM 深入)                 │
  │  ├─ get_cluster_overview() — 集群总览                            │
  │  ├─ list_unhealthy_pods()  — 强制收集所有异常                    │
  │  ├─ list_high_restart_pods()                                     │
  │  ├─ describe_pod_real()    — Top N 深入                          │
  │  └─ 输出: 严重度分类好的 issue 列表 (无遗漏)                      │
  └────────────────────────────┬────────────────────────────────────┘
                               ▼
  ┌─────────────────────────────────────────────────────────────────┐
  │  阶段 2: 调度器 — 同类去重 + Top N 优先级派发                      │
  │  - 同 namespace + type 归一组, 每组只诊断代表 (节省 60%+ LLM 调用) │
  │  - critical / high 优先, 默认覆盖 Top 20 组 (>= 100% 真异常)     │
  └────────────────────────────┬────────────────────────────────────┘
                               ▼
  ┌─────────────────────────────────────────────────────────────────┐
  │  阶段 3: 9 Agent 完整流水线 (LangGraph 状态机, v2.0 自愈闭环)      │
  │                                                                   │
  │  Triage → Aggregator → Classifier → Investigator                 │
  │   (清洗)   (LLM摘要)    (LLM分类)    (ReAct + 工具诊断)            │
  │                              │                                    │
  │                              ├─ get_pod_logs (含 previous 日志)  │
  │                              ├─ kubectl_describe (K8s API)       │
  │                              ├─ prometheus_query (VictoriaMetrics)│
  │                              └─ query_history_alerts             │
  │                              │                                    │
  │                              ▼                                    │
  │   Remediator → ApprovalGate → Executor → Validator → Notifier    │
  │  (LLM 决策)  (L1-L4 分级)  (执行+快照)  (健康检查)  (输出)       │
  │                                                                   │
  │  安全保险: 大开关 + dry-run + 速率限制 + 业务时段 + 三重校验       │
  └────────────────────────────┬────────────────────────────────────┘
                               ▼
  ┌─────────────────────────────────────────────────────────────────┐
  │  阶段 4: 输出 N 份独立诊断报告(根因 + 修复建议 + 执行结果)         │
  └────────────────────────────┬────────────────────────────────────┘
                               ▼
  ┌─────────────────────────────────────────────────────────────────┐
  │  全链路追踪 (Langfuse): 整个周期的 trace 树 + 每次 LLM 调用       │
  │  prompt/completion/token/耗时, 浏览器 UI 一键查看                  │
  └─────────────────────────────────────────────────────────────────┘
```

## 实际效果

对接生产 K8s 集群单次巡检:

```
[Langfuse] enabled

[Inspector] 阶段 1 完成: K8s API 收集到 N 个真实异常 Pod
[Inspector] 严重度分布: critical X / high Y / medium Z / low W

[调度器] 同类去重后 K 组独立根因类型 (节省 M 次 LLM 调用)
[调度器] 选 Top K 组触发完整诊断流水线

[1] [<namespace>] CrashLoopBackOff (影响 2 个 Pod)
    诊断: registry 容器因无法连接到后端存储服务导致启动失败
         (置信度: 高; 关键证据: panic: dial tcp <backend-ip>: connect: connection refused)
    全组: <pod-name-1>, <pod-name-2>

[2] [<namespace>] CrashLoopBackOff (影响 3 个 Pod)
    诊断: 启动参数错误导致程序崩溃
         (置信度: 高; 关键证据: flag provided but not defined: -kubeConfig)

[调度器] 完成深度诊断: K 组, 覆盖 N 个 Pod
[调度器] 覆盖率: 100% critical/high (LLM 调用 K 次)
```

诊断全部 final、置信度全高、根因全部基于容器日志真实错误信息 — SRE 可立即照着排查。

### Langfuse Trace UI 截图

![Langfuse trace screenshot](images/langfuse-trace.png)

> 一个巡检周期 (`inspection_cycle`) 在 Langfuse UI 中的完整呈现:
> 左侧 trace 树展示 Inspector 各阶段 + 调度器 + 多个 pipeline.invoke 嵌套, 每个节点带 token 数与耗时;
> 右侧详情面板可点开看每次 LLM 调用的完整 prompt / completion / metadata。

## 项目结构

```
aiops-multi-agent/
├── agents/                  # Agent 实现
│   ├── state.py             # 共享状态 (TypedDict, 含自愈字段)
│   ├── triage.py            # 告警清洗
│   ├── aggregator.py        # LLM 聚合摘要 (集成 Langfuse callback)
│   ├── classifier.py        # LLM 分类 + 严重度 (集成 Langfuse callback)
│   ├── investigator.py      # ReAct 根因诊断 (集成 Langfuse callback)
│   ├── inspector.py         # 主动巡检 (核心, 三阶段, 集成 Langfuse trace)
│   ├── remediator.py        # 修复决策 Agent (v2.0)
│   ├── approval_gate.py     # 安全分级路由 + 4 层保险 (v2.0) + L2 审批入库
│   ├── executor.py          # 执行 Agent + T0/T2 快照 (v2.0)
│   ├── validator.py         # 健康验证 Agent (v2.0)
│   └── notifier.py          # 通知输出 (含完整自愈链路报告 + IM 推送)
├── tools/                   # 工具集
│   ├── k8s_tools.py         # K8s API 真实工具(异常列表/Pod详情/日志)
│   ├── mock_tools.py        # Investigator 工具集 (PromQL/describe/历史/日志)
│   ├── remediation_actions.py  # L3 白名单 5 个修复动作 (v2.0)
│   ├── safety_guards.py     # 速率限制 + 审计日志 (v2.0)
│   ├── approval_store.py    # SQLite 待审批持久化 + TTL (v2.0+)
│   ├── im_notify.py         # IM 通知统一封装 (如流/钉钉/企微/飞书 + 本地审计)
│   └── langfuse_setup.py    # Langfuse 统一配置 (callback + trace + TraceTimer)
├── scripts/                 # CLI 工具
│   └── aiops_review.py      # L2 审批 CLI (list/show/approve/deny)
├── alerts/                  # 本地审计文件 (.gitignore 中忽略)
├── data/                    # SQLite 持久化 (审批记录, .gitignore 中忽略)
├── tests/                   # 单元/集成测试
├── graph.py                 # LangGraph 编排 (9 节点完整自愈流水线)
├── main_inspect.py          # 主入口: 巡检+诊断+自愈闭环
├── pyproject.toml           # uv 依赖
├── ROADMAP.md               # 21 项前沿迭代方向
└── README.md
```

## 核心设计

### 1. 混合决策架构 (反 LLM 幻觉)

```python
# Inspector 三阶段: 代码强制 + LLM 智能选择
阶段 1: K8s API 强制扫描 → 全部异常 (规则保证, 一个不漏)
阶段 2: LLM 自主决定深入 Top N (节省 API 成本)
阶段 3: 严重度分类走规则 (避免 LLM 误判)
阶段 4: LLM 写整体摘要 (失败也不影响 issues 列表)
```

### 2. ReAct 工具调用 + 代码兜底

```python
# Investigator 在限定步数内 ReAct 循环
for step in range(4):
    decision = llm.invoke(history)
    if decision.action == "use_tool":
        evidence.append(tool.run(args))  # 收集真实证据
    elif decision.action == "final":
        return llm_hypothesis  # LLM 主动收尾

# 关键: LLM 没 final 时不让它二次推理, 用代码兜底拼装保守结论
return _build_fallback_from_real_evidence(evidence)
```

### 3. 同类去重: 同根因故障合并诊断

```python
# 同 namespace + 同异常类型 (CrashLoopBackOff/OOMKilled/...) 归为一组
# 每组挑 restarts 最多的代表诊断, 结论应用到全组成员
groups = group_by(issues, key=("namespace", "type"))
representatives = [max(g, key=restarts) for g in groups]
# 结果: N 个 critical/high 异常 → K 个独立根因 → 节省 (N-K) 次 LLM 调用
```

### 4. VictoriaMetrics multi-tenant 适配

排查到生产环境数据存储于特定 tenant 而非默认 tenant 0:
```python
VMSELECT_URL = "http://<vmselect-cluster-ip>:8481/select/<tenant-id>/prometheus"
```

### 5. Pod 日志含 previous 崩溃前日志 (排障关键)

```python
# 普通 kubectl logs 拿不到崩溃前的内容
_v1.read_namespaced_pod_log(name, namespace, container, previous=True)
# 这才是 CrashLoopBackOff 真根因所在
```

### 6. Langfuse 全链路 Trace 监控

```python
# 一个巡检周期对应一个 Langfuse trace, 内部嵌套所有 Agent / 工具 / LLM 调用
trace = start_cycle_trace("inspection_cycle", session_id=cycle_id)

with TraceTimer("inspector", "phase1:cluster_overview") as t:
    overview = get_cluster_overview()
    t.set_output({"preview": overview[:300]})

# pipeline.invoke 透传 callback, LangChain 自动捕获每次 LLM 调用
pipeline.invoke(state, config={"callbacks": [LANGFUSE_HANDLER]})

end_cycle_trace(trace, output={"reports": len(reports), "coverage": coverage})
```

浏览器打开 Langfuse UI 可看到完整 trace 树:
```
🔍 Trace: inspection_cycle  Duration: 45s   Tokens: 18,234
├─ phase1:cluster_overview                  0.5s
├─ phase1:collect_unhealthy_pods            1.2s   (N issues)
├─ phase2:deep_dive                         5.4s
│   ├─ tool:describe_pod_real               0.4s
│   └─ ChatOpenAI                           2.1s   prompt: ... completion: ...
├─ phase4:overview_summary                  1.8s
└─ pipeline.invoke × K                      ...
```

实际 UI 效果如下 (左侧 trace 树 + 右侧详情面板, 含每次 LLM 调用的 prompt / completion / metadata):

![Langfuse trace screenshot](images/langfuse-trace.png)

### 7. v2.0 自愈闭环: L1-L4 安全分级

```
L1 Dry-run    只输出建议, 永不执行              ← 默认起点 (最稳)
L2 人审       推 IM + CLI 审批才执行
L3 白名单自动 预定义安全动作直接执行              ← AUTO_HEAL_ENABLED=true 才生效
L4 LLM 自由   LLM 决定一切                       ← 永不实现 (生产灾难)
```

**操作分级表 (L3 白名单 5 个动作, 覆盖 80%+ 常见 K8s 异常)**:

| 操作 | 等级 | 适用异常 | 说明 |
|------|------|---------|------|
| `delete_evicted_pod` | L3 | 节点压力驱逐 | 清理 Failed/Evicted Pod, 极低风险 |
| `delete_completed_job_pod` | L3 | Succeeded Job 残留 | 清理已完成态 Pod |
| `delete_failed_pod` | L3 | Failed (非 Evicted) | 清理已死 Pod, 让控制器重建 |
| `restart_pod` | L3 | CrashLoopBackOff / OOMKilled | 删除 Pod 让控制器重建 (仅 ReplicaSet/DaemonSet, StatefulSet 走 L2) |
| `restart_pod_for_image_pull` | L3 | ImagePullBackOff / ErrImagePull | 重启让控制器重新拉镜像 (仅治标) |
| `scale_deployment` | L2 | 副本数调整 | 推 IM 等人审 |
| `evict_pod` / `cordon_node` | L2 | 强驱逐/标记不可调度 | 推 IM 等人审 |
| `delete_pvc` / `drain_node` / `update_image` | L4 | 高危 | **永不自动**, 直接拒绝 |

**4 层安全保险 (任何一个失效都拦得住)**:

```python
# 1. 大开关 (默认关闭)
AUTO_HEAL_ENABLED = os.getenv("AUTO_HEAL_ENABLED", "false") == "true"

# 2. dry-run (默认开启, 即使大开关开了也只打印不动手)
AUTO_HEAL_DRY_RUN = os.getenv("AUTO_HEAL_DRY_RUN", "true") == "true"

# 3. 速率限制 (单 target+action 1h 最多 3 次, 防震荡)
ok, reason = rate_allow(target, action, max_per_hour=3)

# 4. 业务时段保护 (9:00-18:00 自动 → 人审)
if 9 <= datetime.now().hour < 18 and not is_dry_run:
    decision = "human_review"
```

**三重校验防绕过**:

```
ApprovalGate (Layer 1): 校验 safety_level 在白名单 + 大开关 + 速率限制 + 时段保护
        ↓
Executor (Layer 2): 双重校验 action 必须在 L3_ALLOWED_ACTIONS dict 内
        ↓
remediation_actions (Layer 3): 资源真实状态校验 (必须真 Evicted 才删, 必须 RS/DS owner 才能 restart)
```

**Notifier 输出完整链路**:

```
======================================================================
[!!] ALERT NOTIFICATION  severity=CRITICAL
======================================================================
label       : infra
hypothesis  : 容器因连接被拒绝而频繁重启 (置信度: 高; ...)
----------------------------------------------------------------------
REMEDIATION PLAN
  action      : restart_pod
  target      : <namespace>/<pod-name>
  safety      : L3
  rationale   : 临时性问题, 重启可恢复
  rollback    : 重启后问题依旧需检查依赖
----------------------------------------------------------------------
APPROVAL GATE  : ✓ AUTO
  reason      : L3 whitelist + safety checks passed
----------------------------------------------------------------------
EXECUTION      : dry_run
  log         : [DRY-RUN] would restart pod ... (owner=ReplicaSet will recreate it)
  before      : phase=Running restarts=15227
----------------------------------------------------------------------
VALIDATION     : - skipped
  reason      : no real execution (dry-run)
======================================================================
```

### 8. L2 异步审批 (CLI + IM)

L2 操作 (如 `cordon_node` / `scale_deployment`) 不能自动执行, 需人工审批. 实现方式:

```
[流水线] L2 决策
   ↓
[approval_store] SQLite 持久化 (id=A1B2C3D4, ttl=30min)
   ↓
[IM 推送] 含 approval_id + CLI 命令 (审批者复制粘贴即可)
   ↓
[流水线继续] 不阻塞主流程
   ↓
人收 IM → SSH 服务器 → 跑 CLI:
   uv run python -m scripts.aiops_review approve A1B2C3D4
   ↓
[CLI] 双重校验 (白名单 + 速率 + 大开关) → execute_action() → 30s Validator → 推 IM 第二条
```

**CLI 子命令**:

```bash
# 列出所有待审批 (未过期)
uv run python -m scripts.aiops_review list

# 看某条审批的详情 (含 plan / state / RCA)
uv run python -m scripts.aiops_review show <approval_id>

# 批准 (立即执行 + 30s 验证 + 自动推 IM 结果)
uv run python -m scripts.aiops_review approve <approval_id> [--note "..."]

# 拒绝
uv run python -m scripts.aiops_review deny <approval_id> [--reason "..."]
```

**审批安全设计**:
- TTL 30 分钟过期, 防止历史决策被误执行
- 双重校验: CLI `approve` 时再次检查 `is_l3_allowed(action)`, 防止 plan 被篡改
- 沿用环境变量保护: `AUTO_HEAL_ENABLED` 关闭时即使审批通过也不执行
- 审计完整记录: SQLite + safety_guards 内存日志双写

### 9. IM 推送 + 本地审计兜底

通用 IM 通知层 (`tools/im_notify.py`) 适配 4 种 IM 协议, 通过环境变量切换:

```bash
# 一键切换 IM
export IM_PROVIDER=infoflow      # infoflow / dingtalk / wecom / feishu
export IM_WEBHOOK_URL='http://<im-api-host>/...?access_token=xxx'
export IM_TOID='[群id]'           # 仅 infoflow 需要
```

**核心设计**:

```python
def send_message(text):
    # 1. 永远先写本地审计文件 (alerts/<timestamp>.txt)
    #    防 IM 故障导致告警丢失
    write_local_audit(text)

    # 2. POST 到 IM webhook (10s 超时, 失败不阻塞主流程)
    try:
        httpx.post(IM_WEBHOOK_URL, json=builders[IM_PROVIDER](text))
    except Exception:
        pass  # 永不抛异常
```

**防刷屏策略 (`should_push`)**: 仅对以下事件推送:
- 严重度 critical / high
- 真实执行过 (executed / failed) — 修复结果必须让人知道
- L4 高危被拒 — 高风险动作被系统拦下需告警

**IM 消息格式** (精简文本, 兼容所有 IM 协议):

```
🔴 [CRITICAL] AIOps 告警

📍 <namespace> / <pod-name>
📝 容器持续 CrashLoopBackOff
🔍 根因: 后端服务连接被拒绝 (置信度: 高)
🔧 修复方案: restart_pod (L3)
🟢 安全门: ✓ 自动执行
✅ 验证: 修复生效 (新 Pod ready=True)

🆔 trace: a1b2c3d4
```

实际收到的群消息:

![IM notification screenshot](images/im-notification.png)

## 运行环境要求

- Python 3.10+ (实测 3.11)
- uv (https://astral.sh/uv)
- containerd / docker (跑 vLLM 与 Langfuse 容器)
- NVIDIA GPU (实测 2 卡 Tesla T4 跑 Qwen2.5-7B TP=2)
- 可访问 K8s 集群 (`~/.kube/config`)
- 可访问 VictoriaMetrics 或 Prometheus (PromQL 兼容)
- (可选) Langfuse 服务 (开源, 支持本地容器化部署)

## 快速开始

### 1. 启动本地 LLM (vLLM 容器)

```bash
# 拉镜像
nerdctl pull docker.m.daocloud.io/vllm/vllm-openai:v0.6.6.post1

# 下模型 (从 ModelScope, 国内最快)
mkdir -p /root/models
uv run --with modelscope python -c "
from modelscope import snapshot_download
snapshot_download(
    'Qwen/Qwen2.5-7B-Instruct',
    local_dir='/root/models/Qwen2.5-7B-Instruct'
)"

# 启动 (2 卡 T4 + TP=2)
nerdctl run -d --name vllm-qwen --gpus all --net=host \
    -v /root/models:/models --shm-size=8g --ipc=host \
    docker.m.daocloud.io/vllm/vllm-openai:v0.6.6.post1 \
    --model /models/Qwen2.5-7B-Instruct \
    --served-model-name qwen2.5-7b \
    --host 0.0.0.0 --port 8001 \
    --dtype=half --tensor-parallel-size 2 \
    --gpu-memory-utilization 0.85 --max-model-len 8192 --enforce-eager

# 验证
curl -s http://localhost:8001/v1/models
```

### 2. 部署 Langfuse (可选, 但强烈推荐)

```bash
# 在监控节点起 Langfuse + PostgreSQL (host network 模式)
docker run -d --name langfuse-postgres --network host \
  -e POSTGRES_PASSWORD=langfuse_pwd \
  -e POSTGRES_USER=langfuse \
  -e POSTGRES_DB=langfuse \
  -v langfuse-pg-data:/var/lib/postgresql/data \
  docker.m.daocloud.io/library/postgres:15

NEXTAUTH_SECRET=$(openssl rand -base64 32)
SALT=$(openssl rand -base64 32)
ENCRYPTION_KEY=$(openssl rand -hex 32)

docker run -d --name langfuse --network host \
  -e DATABASE_URL="postgresql://langfuse:langfuse_pwd@localhost:5432/langfuse" \
  -e NEXTAUTH_SECRET="$NEXTAUTH_SECRET" \
  -e SALT="$SALT" \
  -e ENCRYPTION_KEY="$ENCRYPTION_KEY" \
  -e NEXTAUTH_URL="http://<your-host>:3000" \
  -e PORT=3000 \
  docker.m.daocloud.io/langfuse/langfuse:2

# 浏览器打开 http://<your-host>:3000
# 注册账号 → 创建 Project → 拿 API Keys
```

### 3. 安装项目

```bash
git clone https://github.com/mumulizi/aiops-multi-agent
cd aiops-multi-agent
uv sync
```

### 4. 配置环境变量

```bash
# K8s + VictoriaMetrics 配置: 改 tools/mock_tools.py 顶部 VMSELECT_URL
VMSELECT_URL = "http://<your-vmselect>:8481/select/<tenant>/prometheus"

# Langfuse (可选)
export LANGFUSE_PUBLIC_KEY="pk-lf-xxx"
export LANGFUSE_SECRET_KEY="sk-lf-xxx"
export LANGFUSE_HOST="http://<your-langfuse-host>:3000"

# 自愈闭环 (默认 dry-run, 不动生产)
export AUTO_HEAL_ENABLED=true     # 大开关 (默认 false, 关闭时所有 L3 降级人审)
export AUTO_HEAL_DRY_RUN=true     # 仅打印不真执行 (默认 true, 推荐保留直到充分验证)

# IM 通知 (可选, 不设则只写本地 alerts/ 文件)
export IM_PROVIDER=infoflow         # infoflow / dingtalk / wecom / feishu
export IM_WEBHOOK_URL='http://<im-api-host>/...?access_token=xxx'
export IM_TOID='[<group-id>]'        # 仅 infoflow 需要 (JSON 数组字符串)

# 永久生效:
# echo 'export LANGFUSE_PUBLIC_KEY="..."' >> ~/.bashrc
```

### 5. 跑一轮巡检 + 诊断

```bash
# 默认: Top 20 组 + 同类去重 + 单次模式
uv run python -u main_inspect.py

# 关闭去重对比效果
uv run python -u main_inspect.py --no-dedup --top 5

# 循环模式 (每 10 分钟一轮)
uv run python -u main_inspect.py --interval 600
```

### 6. 单独测试各模块

```bash
uv run python -u -m tests.test_k8s_tools     # K8s 工具
uv run python -u -m tests.test_prom          # Prometheus 工具
uv run python -u -m tests.test_logs          # Pod 日志工具
uv run python -u -m tests.test_inspector     # Inspector 主动巡检
uv run python -u -m tests.test_e2e           # 5 Agent 流水线 (mock 告警)
```

### 7. L2 异步审批 CLI

```bash
# 列出待审批 (流水线触发 L2 决策后, 这里能看到, 同时群里收到 IM)
uv run python -m scripts.aiops_review list

# 看某条审批的详情
uv run python -m scripts.aiops_review show <approval_id>

# 批准 (会自动调 execute_action + 30s Validator + 推 IM)
uv run python -m scripts.aiops_review approve <approval_id> --note "测试审批"

# 拒绝
uv run python -m scripts.aiops_review deny <approval_id> --reason "不合适当前时段"
```

## 命令行参数

```
uv run python main_inspect.py --help

--top INT          每轮最多诊断的异常组数 (去重后), 默认 20
--no-dedup         关闭同类去重 (默认开启)
--interval INT     循环间隔秒数, 0=单次模式 (默认 0)
--deep-steps INT   Inspector 深入调查最大步数 (默认 4)
```

## 技术栈

| 层 | 选型 |
|---|---|
| Agent 编排 | LangGraph 0.2.x + LangChain 0.3.x |
| LLM 推理 | vLLM 0.6.6 (容器化) + Tensor Parallel TP=2 |
| 模型 | Qwen2.5-7B-Instruct (FP16) |
| 监控 | VictoriaMetrics multi-tenant (PromQL 兼容) |
| K8s 接入 | kubernetes Python SDK |
| 可观测性 | Langfuse 2.x (本地容器化部署) |
| HTTP | httpx + FastAPI |
| 包管理 | uv (Python 3.11) |

## 已知局限与 Roadmap

### 已知局限

- 重复异常会重复诊断 (TODO: 故障 fingerprint 去重缓存)
- 诊断推 IM 之外没有持久化(本地有 alerts/, 缺集中检索)
- 本地 Qwen2.5-7B 在复杂决策上偶有 fallback (考虑升级 14B 或接 Claude API)

### Roadmap (摘要)

完整 21 项前沿迭代方向请见 [ROADMAP.md](./ROADMAP.md)

**v1.1 已完成 ✅** (2026.06)
- [x] Top 20 + 同类去重 (覆盖率 ~100%, LLM 调用 ↓60%)
- [x] Langfuse 全链路 Trace 监控

**v2.0 已完成 ✅** (2026.06)
- [x] Remediator Agent (LLM 修复决策, dry-run + safety_level 输出)
- [x] Approval Gate (L1-L4 安全分级 + 4 层保险: 大开关 / dry-run / 速率限制 / 业务时段)
- [x] Executor (T0 前快照 + T2 后快照, 三重校验防绕过, 完整审计日志)
- [x] Validator (修复后 30s 健康检查, 自动判定 success/failed/skipped)
- [x] 9 节点完整 LangGraph 流水线 (Triage → ... → Investigator → Remediator → ApprovalGate → Executor → Validator → Notifier)
- [x] **生产环境真实自愈验证**: 真删 Pod, 控制器 30s 内重建, Validator 识别新 Pod 并判定 success
- [x] **IM 通知 + 本地审计**: 通用 IM 协议层 (如流/钉钉/企微/飞书一键切换) + alerts/ 目录兜底
- [x] **L3 修复动作扩展**: 5 个动作覆盖 80%+ 异常 (Evicted/Completed/Failed/CrashLoopBackOff/ImagePullBackOff)
- [x] **describe_pod_real 全字段采集**: 拿不到日志的 Pod 也能定位真实根因
- [x] **L2 异步审批 (v2.0+)**: SQLite 持久化 + IM 推送审批指令 + CLI 工具 (list/show/approve/deny) + 自动 30s 验证

**v1.2 计划**
- [ ] 历史故障 Memory (LangMem 思路, MTTR ↓60%, 同 fingerprint 1h 内复用上次诊断)
- [ ] Critic Agent (CRITIC 论文范式, 反 LLM 幻觉)
- [ ] Eval Set + LLM-as-Judge (回归测试)
- [ ] Function Calling Native (替代 ReAct 字符串解析)
- [ ] Tool Result Caching (5min TTL)
- [ ] 升级本地模型: Qwen2.5-14B 替代 7B (T4 双卡 TP=2 可跑) 或 接入 Claude API 做混合架构

**v2.1 自愈闭环深化**
- [ ] L2 飞书/钉钉/如流交互式按钮 (双向 webhook, 替代 CLI 审批)
- [ ] Validator 异步 30s/2min/10min 三次检查 (当前仅 30s 同步)
- [ ] 修复历史 SQLite 检索 + 频次统计仪表盘
- [ ] 修复失败自动触发再诊断闭环
- [ ] L2 操作执行端实现: cordon_node / scale_deployment / evict_pod
- [ ] 微软 GraphRAG 知识库 (基于服务依赖图谱回答全局根因)

**v3.0 学术前沿**
- [ ] Topology-Aware 故障传播分析
- [ ] TimesFM/Chronos 时序异常检测 (zero-shot)
- [ ] Multi-Agent Debate (多 LLM 投票)
- [ ] Tool-use SFT 微调 Qwen (用历史 trace 训练专用 AIOps 模型)
- [ ] MCP Protocol 工具协议化

### 设计原则

1. **混合决策**: 数据正确性走代码, 推理走 LLM (避 LLM 幻觉)
2. **代码兜底**: LLM 任意环节失败均不丢失证据
3. **可观测优先**: 每个 Agent / 工具调用都要 trace
4. **生产安全**: 自愈分级, 永远先 dry-run
5. **持续验证**: Eval Set + LLM-as-Judge 防退化

## License

MIT
