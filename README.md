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
  │  阶段 3: 5 Agent 诊断流水线 (LangGraph 状态机)                    │
  │                                                                   │
  │  Triage → Aggregator → Classifier → Investigator → Notifier      │
  │   (清洗)   (LLM摘要)    (LLM分类)    (ReAct + 工具)    (通知)     │
  │                              │                                    │
  │                              ├─ get_pod_logs (含 previous 日志)  │
  │                              ├─ kubectl_describe (K8s API)       │
  │                              ├─ prometheus_query (VictoriaMetrics)│
  │                              └─ query_history_alerts             │
  └────────────────────────────┬────────────────────────────────────┘
                               ▼
  ┌─────────────────────────────────────────────────────────────────┐
  │  阶段 4: 输出 N 份独立诊断报告(基于真实日志线索的 actionable 根因)│
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
│   ├── state.py             # 共享状态 (TypedDict)
│   ├── triage.py            # 告警清洗
│   ├── aggregator.py        # LLM 聚合摘要 (集成 Langfuse callback)
│   ├── classifier.py        # LLM 分类 + 严重度 (集成 Langfuse callback)
│   ├── investigator.py      # ReAct 根因诊断 (核心, 集成 Langfuse callback)
│   ├── inspector.py         # 主动巡检 (核心, 三阶段, 集成 Langfuse trace)
│   └── notifier.py          # 通知输出
├── tools/                   # 工具集
│   ├── k8s_tools.py         # K8s API 真实工具(异常列表/Pod详情/日志)
│   ├── mock_tools.py        # Investigator 工具集 (PromQL/describe/历史/日志)
│   └── langfuse_setup.py    # Langfuse 统一配置 (callback + trace + TraceTimer)
├── tests/                   # 单元/集成测试
├── graph.py                 # LangGraph 编排
├── main_inspect.py          # 主入口: 巡检+诊断闭环 (含 Top 20 + 去重 + Langfuse trace)
├── pyproject.toml           # uv 依赖
├── ROADMAP.md               # 21 项前沿迭代方向 (v2.0 自愈 / GraphRAG / Critic ...)
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

- 重复异常会重复诊断 (TODO: SQLite 持久化 + 去重缓存)
- 诊断结果仅 stdout 输出 (TODO: 飞书/钉钉机器人推送)
- 修复仅停留在"建议"阶段, 未真正执行 (Roadmap v2.0 自愈闭环)

### Roadmap (摘要)

完整 21 项前沿迭代方向请见 [ROADMAP.md](./ROADMAP.md)

**v1.1 已完成 ✅**
- [x] Top 20 + 同类去重 (覆盖率 ~100%, LLM 调用 ↓60%)
- [x] Langfuse 全链路 Trace 监控

**v1.2 计划**
- [ ] 历史故障 Memory (LangMem 思路, MTTR ↓60%)
- [ ] Critic Agent (CRITIC 论文范式, 反 LLM 幻觉)
- [ ] Eval Set + LLM-as-Judge (回归测试)
- [ ] Function Calling Native (替代 ReAct 字符串解析)
- [ ] Tool Result Caching (5min TTL)

**v2.0 自愈闭环**
- [ ] Remediator Agent (修复决策, 含 dry-run / 安全等级)
- [ ] Approval Gate (L1-L4 安全分级 + 人审分流)
- [ ] Executor (执行 + 4 时间点快照, 失败回滚)
- [ ] Validator (30s/2min/10min 健康检查)
- [ ] 微软 GraphRAG 知识库 (基于服务依赖图谱回答全局根因)

**v3.0 学术前沿**
- [ ] Topology-Aware 故障传播分析
- [ ] TimesFM/Chronos 时序异常检测 (zero-shot)
- [ ] Multi-Agent Debate (多 LLM 投票)
- [ ] Tool-use SFT 微调 Qwen
- [ ] MCP Protocol 工具协议化

### 设计原则

1. **混合决策**: 数据正确性走代码, 推理走 LLM (避 LLM 幻觉)
2. **代码兜底**: LLM 任意环节失败均不丢失证据
3. **可观测优先**: 每个 Agent / 工具调用都要 trace
4. **生产安全**: 自愈分级, 永远先 dry-run
5. **持续验证**: Eval Set + LLM-as-Judge 防退化

## License

MIT
