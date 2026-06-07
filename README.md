# AIOps Multi-Agent — LLM-Powered Kubernetes 自主巡检与根因诊断系统

> 基于 LangGraph 的多 Agent AIOps 平台, 主动巡检 Kubernetes 集群、自主发现异常、
> 调用工具定位真实根因。从"被动接告警"升级为"主动找问题"。

## 项目亮点

- **Agentic AIOps**: Inspector Agent 主动巡检集群, 自主决定查什么, 不依赖外部告警
- **真生产数据**: 接入VictoriaMetrics 监控栈与真实 K8s API
- **混合决策**: 异常清单走规则强制收集(无遗漏), Top N 选择/根因分析走 LLM
- **代码兜底**: LLM 任意环节失败都不会丢失已收集证据, 保守 hypothesis 兜底
- **真根因诊断**: 调用 Pod 日志(含 previous 崩溃前日志) + Prometheus + K8s API 三重证据
- **本地 LLM**: Qwen2.5-7B 在 2 卡 Tesla T4 上 vLLM TP=2 部署, OpenAI 兼容接口

## 架构

```
生产 K8s 集群                                       本地 GPU 节点
├── VictoriaMetrics (multi-tenant)                  ├── vLLM 容器 (Qwen2.5-7B)
├── kube-apiserver                                  └── Agent 项目 (本仓库)
└── 50+ 真实异常 Pod
         │
         ▼
  ┌─────────────────────────────────────────────────────────────────┐
  │  阶段 1: Inspector 主动巡检 (代码强制 + LLM 深入)                 │
  │  ├─ get_cluster_overview() — 集群总览                            │
  │  ├─ list_unhealthy_pods()  — 强制收集所有异常                    │
  │  ├─ list_high_restart_pods()                                     │
  │  ├─ describe_pod_real()    — Top 5 深入                          │
  │  └─ 输出: 严重度分类好的 issue 列表 (50 个真异常)                 │
  └────────────────────────────┬────────────────────────────────────┘
                               ▼
  ┌─────────────────────────────────────────────────────────────────┐
  │  阶段 2: 调度器选 Top N critical/high → 喂入诊断流水线            │
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
  └─────────────────────────────────────────────────────────────────┘
```

## 实际效果

对接生产 K8s 集群(8 节点 / 389 Pod / 18 命名空间)单次巡检:

```
[Inspector] 阶段 1 完成: K8s API 收集到 50 个真实异常 Pod
[Inspector] 严重度分布: critical 16 / high 11 / medium 8 / low 15
[调度器] 选 Top 5 critical 触发完整诊断流水线

[1] harbor-system/harbor-registry-68b7bcb984-tkxlz CrashLoopBackOff restarts=32584
    诊断: registry 容器因无法连接到 192.168.48.71:8080 (后端存储)
         (置信度: 高; 关键证据: panic: dial tcp 192.168.48.71:8080: connect: connection refused)

[2] kube-system/kube-external-auditor-192.168.48.78 CrashLoopBackOff restarts=27011
    诊断: 启动参数错误导致程序崩溃
         (置信度: 高; 关键证据: flag provided but not defined: -kubeConfig)
```

5 份诊断全部 final、置信度全高、根因全部基于容器日志真实错误信息——SRE 可立即照着排查。

## 项目结构

```
aiops-multi-agent/
├── agents/                  # Agent 实现
│   ├── state.py             # 共享状态 (TypedDict)
│   ├── triage.py            # 告警清洗
│   ├── aggregator.py        # LLM 聚合摘要
│   ├── classifier.py        # LLM 分类 + 严重度
│   ├── investigator.py      # ReAct 根因诊断 (核心)
│   ├── inspector.py         # 主动巡检 (核心, 三阶段)
│   └── notifier.py          # 通知输出
├── tools/                   # 工具集
│   ├── k8s_tools.py         # K8s API 真实工具(异常列表/Pod详情/日志)
│   └── mock_tools.py        # Investigator 工具集 (PromQL/describe/历史/日志)
├── tests/                   # 单元/集成测试
├── graph.py                 # LangGraph 编排
├── main_inspect.py          # 主入口: 巡检+诊断闭环
├── pyproject.toml           # uv 依赖
└── README.md
```

## 核心设计

### 1. 混合决策架构 (反 LLM 幻觉)

```python
# Inspector 三阶段: 代码强制 + LLM 智能选择
阶段 1: K8s API 强制扫描 → 50 个真实异常 (规则保证, 一个不漏)
阶段 2: LLM 自主决定深入 Top 5 (节省 API 成本)
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

### 3. VictoriaMetrics multi-tenant 适配

排查到生产环境数据存储于 tenant 1 而非默认 tenant 0:
```python
VMSELECT_URL = "http://10.16.120.255:8481/select/1/prometheus"  # tenant 1
```

### 4. Pod 日志含 previous 崩溃前日志 (排障关键)

```python
# 普通 kubectl logs 拿不到崩溃前的内容
_v1.read_namespaced_pod_log(name, namespace, container, previous=True)
# 这才是 CrashLoopBackOff 真根因所在
```

## 运行环境要求

- Python 3.10+
- uv (https://astral.sh/uv)
- containerd / docker (跑 vLLM 推理服务)
- NVIDIA GPU (实测 2 卡 Tesla T4 跑 Qwen2.5-7B TP=2)
- 可访问 K8s 集群 (~/.kube/config)
- 可访问 VictoriaMetrics 或 Prometheus (PromQL 兼容)

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
    \"Qwen/Qwen2.5-7B-Instruct\",
    local_dir=\"/root/models/Qwen2.5-7B-Instruct\"
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

### 2. 安装项目

```bash
git clone https://github.com/mumulizi/aiops-multi-agent
cd aiops-multi-agent
uv sync
```

### 3. 配置 K8s 与 Prometheus

确保 `~/.kube/config` 可用; 修改 `tools/mock_tools.py` 顶部的 `VMSELECT_URL`:
```python
VMSELECT_URL = "http://<你的 vmselect ClusterIP>:8481/select/<tenant>/prometheus"
```

### 4. 跑一轮巡检 + 诊断

```bash
uv run python -u main_inspect.py
```

### 5. 单独测试各模块

```bash
uv run python -u -m tests.test_k8s_tools     # K8s 工具
uv run python -u -m tests.test_prom          # Prometheus 工具
uv run python -u -m tests.test_logs          # Pod 日志工具
uv run python -u -m tests.test_inspector     # Inspector 主动巡检
uv run python -u -m tests.test_e2e           # 5 Agent 流水线 (mock 告警)
```

## 技术栈

| 层 | 选型 |
|---|---|
| Agent 编排 | LangGraph + LangChain |
| LLM 推理 | vLLM 0.6.6 (容器化) + Tensor Parallel TP=2 |
| 模型 | Qwen2.5-7B-Instruct (FP16) |
| 监控 | VictoriaMetrics multi-tenant (PromQL 兼容) |
| K8s 接入 | kubernetes Python SDK |
| HTTP | httpx + FastAPI |
| 包管理 | uv (Python 3.11) |

## 已知局限与 Roadmap

### 已知局限

- 重复异常会重复诊断 (TODO: SQLite 持久化 + 去重)
- 诊断结果仅 stdout 输出 (TODO: 飞书/钉钉机器人推送)
- 巡检间隔需手动改代码 (TODO: 配置化)

### Roadmap

- [ ] 诊断结果持久化 (SQLite + 历史对比)
- [ ] 重复异常去重 (上次诊断结果直接复用)
- [ ] 飞书/钉钉机器人推送
- [ ] Remediator Agent (基于 Runbook 知识库的 RAG 修复建议)
- [ ] 接入 Alertmanager Webhook (被动告警 + 主动巡检双模式)
- [ ] LangSmith / structlog 全链路 trace
- [ ] 多模型交叉验证 (Claude + Qwen 投票)

## License

MIT
