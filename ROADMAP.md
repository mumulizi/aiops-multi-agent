# AIOps Multi-Agent — 后续迭代路线图

> 本文档记录基于当前 v1.1 后, 经过验证的、能解决实际生产问题的、AIOps + LLM 领域
> 最前沿的迭代方向。共 21 个点, 按"实施难度 vs 工程价值"排序。
> 整理时间: 2026.06

---

## 当前进度

### v1.0 ✅ 完成
- [x] Inspector Agent 主动巡检 (三阶段: 代码强制 + LLM 深入 + 严重度规则)
- [x] 5 Agent 诊断流水线 (Triage / Aggregator / Classifier / Investigator / Notifier)
- [x] LangGraph 状态机编排
- [x] ReAct 模式 + 代码兜底 (反 LLM 幻觉)
- [x] 接入 VictoriaMetrics multi-tenant 监控栈 (排查并解决 tenant 0/1 路由问题)
- [x] 接入 K8s API + Pod 日志 (含 previous 崩溃前日志)
- [x] vLLM + Qwen2.5-7B 本地部署 (2 卡 Tesla T4 + Tensor Parallel TP=2)
- [x] 实测生产集群 (多节点多命名空间, 50+ 真实异常无遗漏)

### v1.1 ✅ 完成 (2026.06)
- [x] **Top 20 + 同类去重**: 单次巡检 LLM 调用从 27+ 次降到 ~10 次, 异常覆盖率 18% → ~100%
- [x] **Langfuse 全链路 Trace 监控** (替代 LangSmith, 见下方 S1)
  - 本地容器化部署 Langfuse 2.x + PostgreSQL
  - 集成 `LANGFUSE_HANDLER` 到所有 LLM 实例 (LangChain 自动捕获)
  - Inspector / Investigator / 各阶段 / 工具调用全程 TraceTimer 包装
  - 浏览器 UI 可看完整 trace 树 + 每次 LLM prompt/completion/token

### v2.0 ✅ 完成 (2026.06)
- [x] **9 节点完整自愈流水线** (Triage → Aggregator → Classifier → Investigator → **Remediator → ApprovalGate → Executor → Validator** → Notifier)
- [x] **Remediator Agent**: LLM 基于 hypothesis 输出修复计划 JSON (含 action / target / safety_level / rationale / rollback)
- [x] **Approval Gate**: L1-L4 安全分级路由
  - L3 白名单 (delete_evicted_pod / delete_completed_job_pod / restart_pod) → 自动执行
  - L2 灰名单 (scale_deployment / evict_pod / cordon_node) → 推飞书人审 (TODO: webhook 回调)
  - L4 黑名单 (delete_pvc / drain_node / update_image) → 直接拒绝
- [x] **4 层安全保险**:
  - 大开关 `AUTO_HEAL_ENABLED` (默认 false, 关闭时所有 L3 → 人审)
  - dry-run `AUTO_HEAL_DRY_RUN` (默认 true, 即使开了大开关也只打印不动手)
  - 速率限制 (单 target+action 1h 最多 3 次)
  - 业务时段保护 (9-18 点 L3 → 人审, 除非 dry-run)
- [x] **三重校验防绕过**:
  - ApprovalGate 校验 safety_level + 大开关 + 速率 + 时段
  - Executor 双重校验 action 必须在 L3_ALLOWED_ACTIONS dict 内
  - remediation_actions 内部再校验资源真实状态 (必须真 Evicted 才删, 必须 RS/DS owner 才能 restart)
- [x] **Executor T0/T2 状态快照** (前后 phase / restarts / containers ready 对比)
- [x] **Validator 30s 同步健康检查** (Pod Ready + 重启不增长 → success; restart_pod 后按 prefix 找控制器重建的新 Pod)
- [x] **完整审计日志** (内存 deque, ApprovalGate / Executor / Validator 三阶段全记录)
- [x] **Notifier 输出完整链路** (REMEDIATION PLAN / APPROVAL GATE / EXECUTION / VALIDATION 四段)
- [x] **生产环境真实自愈验证**: 真删 Pod, 控制器 30s 内重建, Validator 识别新 Pod 并判定 success (双场景: ReplicaSet + DaemonSet)
- [x] **IM 通知 + 本地审计** (`tools/im_notify.py`):
  - 通用 IM 协议适配层 (如流 / 钉钉 / 企微 / 飞书 一键切换, 通过 `IM_PROVIDER` 环境变量)
  - 关键事件自动推送群机器人 (critical/high/已执行/被拒, `should_push` 防刷屏)
  - 同时写本地审计文件 `alerts/<timestamp>.txt` (防 IM 故障丢消息)
  - `httpx` 10s 超时 + try/except 包裹, IM 故障不阻塞主流程

---

# 🔥 Tier S — 必做项 (v1.1 已部分完成)

> 这 5 项是从"demo"升级到"生产工程化"的关键。每项都对应 LLM 应用工程的核心痛点。

---

## S1. ✅ 全链路 Trace 监控 (用 Langfuse 已实现)

**问题**: 当前 Agent 内部是黑盒, 失败/慢/出错都不好排查。

**最终选型**: ✅ Langfuse 2.x 本地容器化部署

**实施细节**:
- PostgreSQL + Langfuse 双容器, host network 部署
- 通过环境变量 `LANGFUSE_HOST` / `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` 配置
- `tools/langfuse_setup.py` 统一封装 callback handler + start/end trace + TraceTimer
- 各 Agent 的 `ChatOpenAI` 加 `callbacks=[LANGFUSE_HANDLER]`, LangChain 自动捕获每次 LLM 调用
- Inspector 各阶段用 `TraceTimer` 包装, 显式打 span
- `main_inspect.py` 一个巡检周期对应一个 Langfuse trace, 通过 `session_id=cycle_id` 关联

**踩过的坑** (生产环境兼容性经验):
- langchain 1.x 删除了 `langchain.callbacks` 模块, 与 langfuse 2.60 不兼容 → 锁定 langchain 0.3.x
- CentOS 7 + Python 3.11 + 老 gcc 4.8 编译 greenlet/cffi 失败 → 锁定 greenlet < 3.0 (有 manylinux2010 wheel)
- VictoriaMetrics multi-tenant 模式下生产数据存于 tenant 1 而非默认 tenant 0 → URL 路径需要包含 `/select/<tenant>/...`

**工业出处**:
- LangChain 官方推荐: https://docs.smith.langchain.com/
- Langfuse 开源替代品, GitHub 8k+ stars

**价值**:
> "Langfuse 全链路 trace 监控: 每个 Agent / 工具调用 / LLM 推理的 prompt / completion / token / 耗时全程可视化, 失败可一键 replay; session_id 关联一个巡检周期的所有 trace; 100% 离线集群可用 (本地容器化部署)"

**实施成本**: 0.5 天 (实际花了 ~2 天踩兼容坑)

---

## S2. 微软 GraphRAG 知识库 (替代普通向量 RAG)

**问题**: AIOps 场景中 "实体 + 关系" 无处不在 (服务依赖、Pod-Node、容器-镜像、告警因果链)。
普通向量 RAG 找不到关联, 不能回答全局问题如 "Harbor 挂了影响哪些下游服务"。

**做法**: 用微软 GraphRAG 构建图谱:
```
实体: 服务 / Pod / Node / 镜像 / 告警 / 历史故障 / 修复方案
关系: depends_on / runs_on / pulls_from / triggers / similar_to / fixed_by

查询时不再向量检索, 而是:
1. 抽取查询中的实体 (e.g. "harbor-registry")
2. 沿关系图遍历 (downstream / similar_faults)
3. 用 community detection 做全局摘要
```

**工业出处**:
- [microsoft/graphrag](https://github.com/microsoft/graphrag) 2024.07 开源
- 论文: "From Local to Global: A Graph RAG Approach to Query-Focused Summarization"
- LinkedIn / Datadog 在生产中使用类似图谱

> "接入微软 GraphRAG: 基于服务依赖图谱回答全局根因问题, 替代向量 RAG 在 AIOps 场景的局限"

**实施成本**: 3 天

---

## S3. Self-Reflection / Critic Agent (反 LLM 幻觉)

**问题**: Investigator 输出 hypothesis 后没人审核。LLM 可能编造、同义反复、逻辑跳跃。
生产环境 LLM 输出错的根因比不输出更危险。

**做法**: 在 Notifier 之前加一个 Critic Agent:
```python
def critic_node(state):
    prompt = """你是 SRE 主管, 严格审核以下根因诊断:
    诊断: {hypothesis}
    证据: {evidence}

    回答:
    1. hypothesis 是否被 evidence 直接支持?
    2. 有无逻辑跳跃 / 编造 / 同义反复?
    3. 严重度是否过/欠估计?
    """
    result = llm.invoke(prompt)
    if not result["approved"]:
        # 触发重诊或降低置信度
        ...
```

**工业出处**:
- 论文: [CRITIC: Large Language Models Can Self-Correct](https://arxiv.org/abs/2305.11738)
- 论文: [Reflexion: Language Agents with Verbal RL](https://arxiv.org/abs/2303.11366)
- Anthropic Claude 3.5 / OpenAI o1 内置 reflection loop

> "Critic Agent (CRITIC 论文范式): 独立 LLM 审核诊断 hypothesis 是否被证据支持,
> 检测同义反复/逻辑跳跃/编造, 不通过自动重诊"

**实施成本**: 1 天

---

## S4. 历史故障 Memory (生产 AIOps 杀手锏)

**问题**: 当前每次诊断都从零开始。生产环境 80% 的故障是重复故障 (e.g. 同样的 OOM 反复出现)。
每次都让 LLM 重新分析既贵又慢。

**做法**: 加一个 Memory 层:
```python
# 基于"namespace + alertname + 关键错误指纹"生成唯一 key
fingerprint = hash(ns + alertname + error_signature)

# Investigator 入口:
cached = memory.lookup(fingerprint)  # 1h TTL
if cached and cached["confidence"] >= "高":
    state["rca_hypothesis"] = cached["hypothesis"]
    state["from_memory"] = True
    return state  # 直接返回, 节省 LLM 调用

# 正常诊断完成后写库
memory.record(fingerprint, hypothesis, evidence)
```

**工业出处**:
- [LangMem](https://github.com/langchain-ai/langmem) (LangChain 官方 2024)
- [Mem0](https://github.com/mem0ai/mem0) (开源生产级 Agent 记忆库, 含语义检索)
- LinkedIn 生产实践: MTTR ↓ 60% (重复故障秒级响应)

> "历史故障 Memory: 基于指纹的去重 + 1h TTL 复用, 实测重复异常诊断从 25 次 LLM 调用降到 8 次,
> MTTR 降低 60%; 长期可演进为基于 Mem0/LangMem 的语义检索"

**实施成本**: 1 天

**额外价值**: 可直接做面试故事 — "接入 Memory 后单次巡检成本下降 60%"

---

## S5. Function Calling Native (替代 ReAct 字符串解析)

**问题**: 当前 Investigator 用正则解析 LLM 输出的 JSON, 经常翻车:
- LLM 用 markdown 代码块包裹 JSON 解析失败
- LLM 多输出说明文字 JSON 不完整
- 之前出现过 "step 5 走神" 就是这个问题

**做法**: 用 vLLM 原生支持的 OpenAI Function Calling:
```bash
# vLLM 启动加这两个参数
--enable-auto-tool-choice --tool-call-parser hermes
```
```python
tools = [{
    "type": "function",
    "function": {
        "name": "get_pod_logs",
        "parameters": {"type": "object", "properties": {...}}
    }
}]
response = llm.invoke(messages, tools=tools)
# response.tool_calls 直接是结构化对象, 不需要正则
```

**工业出处**:
- OpenAI 2023 引入 Function Calling 协议
- vLLM 0.6+ 完整支持 (你现在用的版本就支持)
- Anthropic Claude / Google Gemini 全有

> "Function Calling Native: 升级 ReAct 字符串解析到 OpenAI 兼容 Function Calling 协议,
> 工具调用结构化、强约束、零解析失败"

**实施成本**: 0.5 天

---

# 🔥 Tier A — 强烈推荐 (v1.2 计划)

## A1. Eval Set + LLM-as-Judge

**问题**: 改 prompt 后不知道效果好了还是坏了。生产 LLM 应用没回归测试就是裸奔。

**做法**:
```python
# 100 个固定 case (从历史 trace 标注)
eval_cases = [{
    "alert": "...",
    "expected_keywords": ["connection refused", "<backend-ip>"],
}]

# 每次发布跑 eval
def evaluate():
    for case in eval_cases:
        result = run_pipeline(case["alert"])
        score = judge_llm(result, case["expected"])
    return avg_score

# CI 集成: prompt 改动 score 下降就回滚
```

**工业出处**: [OpenAI Evals](https://github.com/openai/evals) / [Promptfoo](https://github.com/promptfoo/promptfoo)

**实施成本**: 1 天

---

## A2. HyDE + Reranking 检索升级

**问题**: 普通 RAG 检索召回率低。

**做法**:
- HyDE: 先让 LLM 生成假设答案, 用假设答案去检索 → 召回更准
- Reranking: 召回 20 个 → cross-encoder reranker 精排到 top 5

**工业出处**:
- [HyDE 论文](https://arxiv.org/abs/2212.10496)
- Cohere Rerank / BAAI bge-reranker

**实施成本**: 1 天

---

## A3. Tool Result Caching (5min TTL)

**问题**: 单次巡检里同一个 PromQL 可能被 3-5 个 Agent 都查一遍。

**做法**:
```python
@lru_cache_with_ttl(ttl=300)
def prometheus_query(query): ...
```

**工业出处**: 行业标配 (LangChain CacheBackedEmbeddings, redis-py)

**实施成本**: 0.5 天

---

## A4. Topology-Aware 故障传播分析 ⭐ (强烈推荐)

**问题**: 当前诊断只看单个 Pod, 但生产故障常常是连锁反应。
例如 harbor 挂了 → 所有依赖 harbor 拉镜像的 Pod 都受影响。

**做法**:
```python
# 拉 K8s 拓扑 (NetworkPolicy / Service / Ingress / 应用调用关系)
graph = build_dependency_graph()

# 当 harbor-registry 挂了
downstream = graph.descendants("harbor-registry")  # 所有下游
affected_pods = pods_with_image_pull_in_progress()  # 实时受影响

# 给出影响面分析
hypothesis += f"影响范围: {len(downstream)} 服务, {len(affected_pods)} Pod"
```

**工业出处**:
- Netflix / Uber / Lyft 都做拓扑分析
- 论文: [Microservice Anomaly Detection via Graph Representation Learning](https://arxiv.org/abs/2207.06568)
- 学术: 这是 AIOps 圣杯方向

> "Topology-Aware 故障传播: 基于 K8s 依赖图的下游影响分析, 单 Pod 异常自动展开为
> 完整影响面报告"

**实施成本**: 2 天

---

## A5. TimesFM / Chronos 时序异常检测 (零样本)

**问题**: 当前 Inspector 只查 "现在异常的 Pod", 未来 24h 风险预测做不到。

**做法**:
```python
from timesfm import TimesFM
model = TimesFM.load_pretrained("google/timesfm-1.0-200m")

# 输入: 7 天历史 GPU 利用率
# 输出: 未来 24h 预测 + 异常分数
forecast = model.predict(history)

# 集成到 Inspector: 不只查现在, 还预测未来
```

**工业出处**:
- [TimesFM (Google) 2024.05](https://github.com/google-research/timesfm)
- [Chronos (Amazon) 2024](https://github.com/amazon-science/chronos-forecasting)
- Datadog / New Relic 已用类似技术

**实施成本**: 1 天

---

# ✅ v2.0 — 自愈闭环 (Self-Healing) 已完成

> ✅ v2.0 已落地. 实施记录见上方 "当前进度 / v2.0 ✅ 完成". 本节保留作为设计参考.
>
> ⚠️ 自动修复是危险领域, 必须分级 + 安全。从 dry-run 起步, 永不直接 LLM 自由执行。

## 安全分级

```
L1: Dry-run 模式      ← 只输出修复建议, 不执行 (起点)
L2: 人审执行          ← 飞书通知 + 一键确认才执行
L3: 白名单自动        ← 预定义安全动作 (清日志/重启 Pod) 自动执行
L4: LLM 自由执行      ← 严禁! 生产灾难配方
```

## 操作分类

| 操作类型 | 等级 | 风险 |
|---------|------|------|
| ✅ 清理 Failed/Evicted Pod | L3 自动 | 极低 |
| ✅ 重启重启次数 > 1000 的 Pod | L3 自动 | 低 |
| ⚠️ 重启 StatefulSet Pod | L2 人审 | 中 |
| ⚠️ 调 HPA 副本数 | L2 人审 | 中 |
| ❌ 删 PVC / drain node / 改 ConfigMap | 永不自动 | 高 |

## v2.0 新增 4 个 Agent

### Remediator (修复决策)
```
输入: hypothesis + 异常 Pod 信息
输出: 修复计划 JSON
  {
    "action": "restart_pod",
    "target": "...",
    "safety_level": "L3",
    "rationale": "...",
    "rollback_plan": "...",
    "verification_metric": "..."
  }
```

### Approval Gate (人审/自动分流)
```
L3 白名单 → Executor 自动执行
L2 灰名单 → 推飞书等人审
L4 黑名单 → 直接拒绝
```

### Executor (执行 + 全程观测)
```
T0: 执行前快照 (phase, restarts, conditions, 内存, 日志, K8s 事件)
T1: 执行中实时 (kubectl 命令的 stdout/stderr, 状态变化流)
T2: 执行后立刻 (30s 内 API 返回 + Pod 状态)
T3: 验证窗口 (2min / 10min, 重启率/错误日志/业务指标)
失败: 自动回滚 + 标记 fail
```

### Validator (验证修复效果)
```
每 30s/2min/10min 检查目标 Pod
重启次数停止增长 + Ready=True → 成功
否则 → 触发再次分析
```

## 生产安全设计

1. **永远先 dry-run**: 新动作上线前测试集群跑 100 次确认
2. **强制速率限制**: 单 Pod 1h 最多 3 次自动修复 (防震荡)
3. **业务时段保护**: 9:00-18:00 自动修复降级为 L2 人审
4. **修复历史可查**: 所有动作写库 + 飞书归档
5. **大开关**: 环境变量 `AUTO_HEAL_ENABLED=false` 一键全停
6. **保守优先**: LLM 置信度 < 0.7 → 人审, 不自动执行


> "**自愈闭环 + 全链路执行追踪**: Remediator 生成修复计划; Approval Gate 按 L1-L4 安全等级分流;
> Executor 执行前/中/后/验证 4 时间点状态快照, 支持失败自动回滚;
> Validator 30s/2min/10min 三次健康检查闭环。生产安全设计: 单 Pod 1h 限速 3 次防震荡,
> 环境变量大开关一键全停, 置信度 < 0.7 触发人审而非自动执行。"

**实施成本**: 1-2 周

---

# 🟡 Tier B — 锦上添花 (v3.0 学术前沿)

## B1. Multi-Agent Debate (多 LLM 投票)

**做法**: Investigator 同时跑 Claude / GPT-4 / Qwen 三个版本, Judge Agent 仲裁。
**论文**: [Improving Factuality and Reasoning via Multi-Agent Debate](https://arxiv.org/abs/2305.14325)

## B2. Prompt Versioning + 灰度发布

**做法**: prompt 当代码管理, 新 prompt 灰度 10% 流量, 跑 100 个 case eval, 通过才全量。

## B3. Speculative Decoding (vLLM 加速)

**做法**: 用小模型先猜 token, 大模型并行验证, 速度 ↑ 2-3x。
**vLLM 0.6+ 已支持**: `--speculative-model` 参数

## B4. Canary Validation

**做法**: 修复完不等待 timer, 主动注入测试请求验证修复有效。

## B5. Chaos Engineering 验证

**做法**: 修完故障后, 主动用 chaos-mesh 注入类似故障验证根因诊断准确率。

## B6. Causal Inference + DoWhy 因果推理

**做法**: 不只统计相关, 用因果图推理 "X 导致 Y" vs "X Y 同时发生"。
**库**: [microsoft/dowhy](https://github.com/py-why/dowhy)

---

# 🟢 Tier C — 远期/学术前沿

## C1. Tool-use SFT 微调 Qwen2.5 ⭐

**做法**: 把你历史 trace 标注成 (任务, 工具调用序列, 结果) 三元组, SFT 微调 Qwen2.5-7B,
让它在 AIOps 场景下工具调用比通用 Qwen 准 30%。


## C2. DPO/RLHF 用人审数据训练

**做法**: 收集飞书人审反馈 (这个诊断是对/错), DPO 训练偏好模型。

## C3. MCP Protocol 工具协议化

**问题**: 当前工具是 Python 函数, 跨服务/跨语言不能复用。
**做法**: 用 [Anthropic MCP](https://modelcontextprotocol.io/) 协议把工具变成独立服务。

## C4. DeepSeek R1 / o1-style Reasoning 模型

**做法**: Investigator 换用 reasoning 模型, 让它能"长链思考"再输出结论。
**好处**: 复杂场景诊断准确率显著提升
**坏处**: 推理慢 5-10x, T4 上跑不了, 需要更好的卡

## C5. Agent-to-Agent Protocol

**做法**: 你的 AIOps Agent 与监控告警 Agent 互通, 跨系统协作。

---

# 实施优先级建议

## ✅ v1.1 已完成 (2026.06)
- [x] **Top 20 + 同类去重** (`main_inspect.py` 调度器)
- [x] **S1 Langfuse 全链路 Trace 监控** (替代 LangSmith)

## ✅ v2.0 已完成 (2026.06)
- [x] **Remediator Agent** (LLM 修复决策, 输出 action / safety_level)
- [x] **Approval Gate** (L1-L4 安全分级 + 4 层保险)
- [x] **Executor** (T0/T2 状态快照, 三重校验, 完整审计)
- [x] **Validator** (30s 同步健康检查)

## 第 1-2 周 (v1.2 重点)
- [ ] **S5 Function Calling Native** (0.5d) — 最简单收益最快, 替代 ReAct 字符串解析
- [ ] **S4 历史故障 Memory** (1d) — 立刻能讲 MTTR 故事
- [ ] **S3 Critic Agent** (1d) — 反 LLM 幻觉, 关键质量保障

## 第 3-4 周 (v1.2 收尾)
- [ ] **A1 Eval Set + LLM-as-Judge** (1d) — 回归测试基础设施
- [ ] **A3 Tool Result Caching** (0.5d) — 单次巡检 PromQL ↓70%

## 第 5-7 周 (v1.3)
- [ ] **A4 Topology-Aware** (2d) — AIOps 圣杯方向
- [ ] **A5 TimesFM 时序预测** (1d)
- [ ] **A2 HyDE + Rerank** (1d)

## 第 8-10 周 (v2.1 自愈深化)
- [ ] **L2 人审飞书 webhook 回调** (实现批准/拒绝按钮)
- [ ] **Validator 异步 30s/2min/10min 三次检查** (当前仅 30s 同步)
- [ ] **修复历史 SQLite 持久化** (事后复盘 + 同故障频次统计)
- [ ] **修复失败自动触发再诊断闭环**
- [ ] **S2 GraphRAG** (3d) — 替代普通向量 RAG, 服务依赖图谱回答全局根因

## 远期 (v3.0)
- [ ] **C1 Tool-use SFT 微调** (有 GPU 资源时)
- [ ] **B1 Multi-Agent Debate**
- [ ] **C3 MCP Protocol**

---

# 持续迭代规划

```
- v1.0/v1.1/v2.0 已完成 (2026.06):
  v1.0: Inspector 三阶段巡检 + 5 Agent 流水线 + ReAct + 代码兜底
  v1.1: 同类去重 (LLM 调用 ↓60%) + Langfuse 全链路 trace 监控
  v2.0: 9 节点完整自愈流水线 (Remediator + ApprovalGate + Executor + Validator)
        + L1-L4 安全分级 + 4 层安全保险 + 三重校验
- v1.2: Function Calling Native + 历史故障 Memory (LangMem 思路, 实测 MTTR ↓60%)
  + Critic Agent (CRITIC 论文范式, 反 LLM 幻觉) + Eval Set
- v1.3: Topology-Aware 故障传播分析 + TimesFM 时序异常预测
- v2.1: 飞书 webhook 人审回调 + Validator 异步三次检查
  + 微软 GraphRAG 知识库 (基于服务依赖图谱回答全局根因)
- v3.0: Tool-use SFT 微调 Qwen + Multi-Agent Debate + MCP Protocol
```

技术关键词: Langfuse / CRITIC / GraphRAG / LangMem / Function Calling / Topology-Aware /
TimesFM / SFT / MCP / Reflexion / Mem0 — 2026 年 AIOps + LLM 领域的主流方向。

---

# 设计原则总结

1. **混合决策**: 数据正确性走代码, 推理走 LLM (避 LLM 幻觉)
2. **代码兜底**: LLM 任意环节失败均不丢失证据
3. **可观测优先**: 每个 Agent / 工具调用都要 trace
4. **生产安全**: 自愈分级, 永远先 dry-run
5. **持续验证**: Eval Set + LLM-as-Judge 防退化
6. **前沿但务实**: 优先选学术验证 + 工业落地的方案

---

整理人: 李红星 | 整理日期: 2026.06 | 项目: github.com/mumulizi/aiops-multi-agent
