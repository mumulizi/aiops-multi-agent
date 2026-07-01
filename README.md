# AIOps Multi-Agent — LLM-Powered Kubernetes 自主巡检与根因诊断系统

> 基于 LangGraph 的多 Agent AIOps 平台, 主动巡检 Kubernetes 集群、自主发现异常、
> 调用工具定位真实根因。从"被动接告警"升级为"主动找问题"。

## 项目亮点

- **Agentic AIOps**: Inspector Agent 主动巡检集群, 自主决定查什么, 不依赖外部告警
- **生产监控接入**: 接入 VictoriaMetrics multi-tenant 监控栈与真实 K8s API
- **混合决策**: 异常清单走规则强制收集(无遗漏), Top N 选择/根因分析走 LLM
- **代码兜底**: LLM 任意环节失败都不会丢失已收集证据, 保守 hypothesis 兜底
- **真根因诊断**: 调用 Pod 日志(含 previous 崩溃前日志, v2.5 加 init containers) + Prometheus + K8s API 三重证据
- **同类去重 + 高覆盖**: 单次巡检 LLM 调用从 27+ 次降到 ~10 次, 异常覆盖率 18% → ~100%;
  v2.5 升级分组键 `(ns, type, service_prefix)` 三维度, 同 ns 不同服务不再被误合并
- **Langfuse 全链路追踪**: 所有 Agent / 工具 / LLM 调用的 prompt / completion / token / 耗时全程可视化, 支持失败重放与性能分析
- **自愈闭环 (v2.0+)**: 9 节点完整流水线 (Inspector → Triage → Aggregator → Classifier → Investigator → **Remediator → ApprovalGate → Executor → Validator** → Notifier);
  L1-L4 安全分级 + 4 层安全保险 (大开关 / dry-run / 速率限制 / 业务时段); 三重防护避免 LLM 失误执行高风险动作
- **v2.3 失败再诊断闭环**: Validator failed 自动跳回 Investigator 重诊 (默认最多 2 次),
  带上 last_failed_plan + 失败原因, 让 LLM 知道"刚才试过 X 但没救"
- **v2.3 故障 Memory (SQLite)**: 同指纹 (ns + alertname + RCA 前 100 字符) 1h 内复用, 重复故障秒级响应, 跳过 4-8 次 LLM 调用
- **修复动作库 (v2.0 + v2.2 扩展)**: L3 自动 7 个 (`delete_evicted_pod` / `delete_completed_job_pod` /
  `restart_pod` / `restart_pod_for_image_pull` / `delete_failed_pod` / `cordon_node` / `uncordon_node`),
  L2 人审 3 个 (`restart_statefulset_pod` / `scale_deployment` / `rollback_deployment`),
  覆盖 80%+ 常见 K8s 异常 (CrashLoop / ImagePull / OOM / Evicted / 节点失联 / 副本不足 / 上线崩了)
- **R1/R2/R3 强制规则 (v2.1+v2.2)**: 抵御 LLM 幻觉,
  R1 BarePod/Job → none; R2 StatefulSet+CrashLoop → restart_statefulset_pod L2;
  R3 "重启无救"故障 (镜像错/配置错/启动参数错) → none + escalate_human
- **L2 异步审批 (CLI)**: SQLite 持久化待审批操作 (TTL 30min) + IM 推送审批指令 + CLI 工具 (`scripts/aiops_review.py`) 远程审批; 审批通过后双重校验 + 30s Validator 自动验证 + 第二条 IM 推执行结果
- **v2.6 YAML 忽略策略**: `config/policies.yaml` 配置 namespace / Pod 黑名单, 改完不重启自动加载
- **v2.8 多集群部署**: REGION 环境变量区分告警来源 (IM 第一行 `🔴 [CRITICAL] [prod-bj] AIOps 告警`);
  LLM 工厂 (`tools/llm_factory.py`) 支持全局 + 角色级 env 切换, 可让关键 Agent 走云 API 强模型, 其他保留本地
- **describe_pod_real 完整字段采集**: 覆盖 `waiting.message` / `last_terminated.message` / `conditions.message` / `pod.status.message` / `init_containers` / `images` 等 8 类字段, 拿不到日志的 Pod (Pending/ImagePull) 也能定位真实根因
- **IM 通知 + 本地审计**: 通用 IM 协议适配层 (如流 / 钉钉 / 企微 / 飞书 一键切换);
  关键事件自动推送群机器人 (critical/high/已执行/被拒/待审批); 同时写本地审计文件 (alerts/) 防 IM 故障丢消息
- **210+ pytest 单测**: 纯函数 + monkeypatch, 不连外部, 覆盖 R1/R2/R3 强制规则 + target 校验 +
  Memory + 闭环路由 + 策略匹配 + LLM 工厂 + 分级诊断 + Function Calling final 解析 等关键路径
- **v2.9 Function Calling Native**: Investigator 默认走 OpenAI Function Calling 协议
  (vLLM `--enable-auto-tool-choice --tool-call-parser hermes`), tool_calls 结构化、零正则解析失败;
  保留 ReAct 字符串解析路径作兜底 (`USE_FUNCTION_CALLING=false` 一键回退)
- **v2.10 LLM 防呆三道闸**: (1) 同一 (tool, args) 第 3 次重复调用自动中断 (拦"瞎猜 Pod 名循环");
  (2) 工具 TypeError 自动注入参数纠错提示 (拦 schema 不匹配); (3) `get_pod_logs` 兼容 LLM 显式传 `previous=true`
- **v2.11 草稿检测器**: Function Calling 模式下 LLM 返回空 `tool_calls` 时,
  二次校验是真 final 还是"思考过程当 final 输出"
  (匹配 `假设/第一步/```json` 等草稿特征 + 缺 "根因:" 标签), 草稿强制重试 1 次后才走兜底,
  彻底治住 32B 模型偶发"用文字描述工具调用"问题
- **v2.12 P0 改善 (AIOps 平台化第一步)**:
  - **速率限制 SQLite 持久化** (`data/aiops.db`): 重启不丢状态, 为多副本部署铺路
  - **变更感知工具 `get_recent_changes`**: Investigator 新工具, 查 namespace 近 N 小时 Deployment/RS/StatefulSet/ConfigMap/Secret/Event 变更, 关联近期发布 → 故障 (业界 80% 故障由变更引起)
  - **MetricsInspector**: 跟 Inspector 并行的指标层巡检 Agent (无 LLM), 6 条内置 PromQL 规则 (CPU 节流 / 内存逼近 limit / 节点磁盘压力 / load 高 / apiserver 5xx / kubelet 失联), 把"Pod 崩了再修"升级为"SLO 异常→主动诊断"
  - **Validator 异步化**: 主流程内 T+0 立即返回 `pending_async` 不阻塞调度; SQLite 任务表 + daemon 线程 30s/2min/10min 三轮异步验证; 终态推第二条 IM; `VALIDATOR_ASYNC=false` 一键回退到 v2.11 同步行为
  - 配套 CLI: `scripts/aiops_verify_status.py` 看异步任务表 (`--pending` / `--json` / `--limit`)
- **v2.13 自主执行 (Readonly Tier A)**: Investigator 新增 `ssh_node_readonly` + `kubectl_exec_readonly` 两个只读工具, 让它像 Claude Code 那样自主跑 `lsmod / dmesg / journalctl / cat /etc/...` 等命令实地查证再下结论 (而不是只给"建议节点运维检查").
  - 安全闸: 命令前缀白名单 + 子命令二级白名单 + dangerous token 黑名单 (rm/sed -i/systemctl restart/kubectl 写操作/curl POST 等 60+ token) + 重定向/反引号/`$()` 字符级拦截 + ssh 节点必须在 K8s nodes 列表
  - 资源闸: 单命令 10s 超时 + 输出截断 4KB + (node, prefix) 1h 5 次速率
  - 跟 L3/L2/L4 修复路径不重叠: 只管只读诊断, 任何状态变更继续走 Remediator → ApprovalGate → Executor
- **v2.14 人审突破白名单 (审批命令通道)**: Investigator 新增 `ssh_node_with_approval` + `kubectl_exec_with_approval` 两个工具, 让 LLM 能申请**突破白名单**的命令 (如 `crictl pull` 验证镜像可达, `systemctl restart kubelet` 后再观察), 运维在 IM 群里 approve 后 daemon 异步执行, 结果进 `fault_memory.diagnostic_cmd_history` 形成学习闭环.
  - **不阻塞诊断**: LLM 调用后立即返回 `[已派单审批 task_id=xxx]`, 应基于现有证据先 final 临时结论; 审批结果异步进 Memory, 下次同指纹故障可秒级复用
  - **硬黑名单**: rm/dd/mkfs/shutdown/reboot/iptables -F/kubectl delete --all/`:(){:|:&};:` 永远不入审批通道 (不可逆或影响面太大, 必须人工 ssh 跑)
  - **reason 强制**: 必须一句话写清"为什么要跑 + 期望验证什么" (>=10 字), 拒 "试一下"/"看看" 等空话
  - **异步执行**: daemon 线程每 5s 扫 SQLite, approve → subprocess.run (10s 超时 + 输出 4KB 截断) → 写结果 → 推第二条 IM → 写 fault_memory
  - **学习闭环**: Investigator 命中 Memory 时自动读 `diagnostic_cmd_history` 最近 5 条, 把历史命令+结果塞进 user_msg 给 LLM 看. 上次证实过的根因这次可直接 final
  - **速率限制**: 单 (trace_id, target) 1h 最多 3 条审批请求, 防 LLM 刷屏
  - 配套 CLI: `scripts/aiops_review.py list/show/approve/deny` 自动区分 🔍 diagnostic_cmd vs 🔧 remediation
- **本地 LLM**: Qwen2.5-32B-Instruct-AWQ 在 2 卡 Tesla T4 上 vLLM TP=2 部署 (从 7B 升级而来),
  OpenAI 兼容接口; 也可通过 env 切换关键 Agent 走云 API 强模型 (混合架构,
  DeepSeek-V3 / 通义千问 API / 任何 OpenAI 兼容端点)

## 架构

```
生产 K8s 集群                                       本地 GPU 节点
├── VictoriaMetrics (multi-tenant)                  ├── vLLM 容器 (Qwen2.5-32B-AWQ)
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
  │  阶段 2: 调度器 — 同类去重 + Top N 优先级派发 (v2.4 分级诊断)        │
  │  - 同 (ns, type, service_prefix) 归一组, 代表诊断 (节省 60%+ LLM)  │
  │  - critical/high 完整 ReAct, medium 轻量 (3 步), low 仅审计       │
  │  - 默认 --top 50, 配合去重+Memory 已能覆盖大集群, --top 0 不限   │
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
│   ├── investigator.py      # 根因诊断 (v2.9 Function Calling + v2.10 防循环 + v2.11 草稿检测, 集成 Langfuse)
│   ├── inspector.py         # 主动巡检 (核心, 三阶段, 集成 Langfuse trace)
│   ├── metrics_inspector.py # 指标层巡检 Agent (v2.12, 6 条内置 PromQL 规则, 无 LLM)
│   ├── remediator.py        # 修复决策 Agent (v2.0)
│   ├── approval_gate.py     # 安全分级路由 + 4 层保险 (v2.0) + L2 审批入库
│   ├── executor.py          # 执行 Agent + T0/T2 快照 (v2.0)
│   ├── validator.py         # 健康验证 Agent (v2.12 异步路径, T+0 派单 + 不阻塞)
│   ├── verifier_worker.py   # 异步验证 daemon 线程 (v2.12, 30s/2min/10min 三轮)
│   └── notifier.py          # 通知输出 (含完整自愈链路报告 + IM 推送 + region 标识)
├── tools/                   # 工具集
│   ├── k8s_tools.py         # K8s API 真实工具(异常列表/Pod 详情/多容器日志)
│   ├── mock_tools.py        # Investigator 工具集 (PromQL/describe/历史/日志/变更追踪/ssh+exec, v2.13)
│   ├── tool_schemas.py      # Function Calling 严格 JSON Schema (v2.13, 7 工具的 type/required/enum 约束)
│   ├── change_tracker.py    # 变更追踪 (v2.12, K8s Deployment/RS/CM/Secret/Event 变更查询)
│   ├── ssh_tools.py         # 只读 shell 执行 (v2.13, ssh+kubectl exec 白名单/黑名单/4 道安全闸)
│   ├── metrics_rules.py     # MetricsInspector 内置 PromQL 规则集 (v2.12)
│   ├── remediation_actions.py  # L3/L2 修复动作 (Pod/Node/Deployment 级)
│   ├── safety_guards.py     # 速率限制 (v2.12 SQLite 持久化) + 审计日志 (内存)
│   ├── approval_store.py    # SQLite 待审批持久化 + TTL (v2.0+)
│   ├── verifier_store.py    # SQLite 异步验证任务表 (v2.12)
│   ├── fault_memory.py      # SQLite 故障 Memory (v2.3, 同指纹复用)
│   ├── policy.py            # YAML 忽略策略加载 + 匹配 (v2.6)
│   ├── llm_factory.py       # 统一 LLM 工厂 + region 标识 (v2.8 多集群)
│   ├── im_notify.py         # IM 通知统一封装 (如流/钉钉/企微/飞书 + 本地审计)
│   └── langfuse_setup.py    # Langfuse 统一配置 (callback + trace + TraceTimer)
├── scripts/                 # CLI 工具
│   ├── aiops_review.py      # L2 审批 CLI (list/show/approve/deny)
│   └── aiops_verify_status.py # 异步验证任务表 CLI (v2.12, --pending/--json/--limit)
├── config/                  # 配置文件
│   └── policies.yaml.example  # 忽略策略模板 (复制成 policies.yaml 后改)
├── alerts/                  # 本地审计文件 (.gitignore 中忽略)
├── data/                    # SQLite 持久化 (审批 + Memory, .gitignore 中忽略)
├── tests/                   # 单元/集成测试 (~210 个 pytest 用例)
├── graph.py                 # LangGraph 编排 (9 节点完整自愈流水线 + v2.3 闭环重诊)
├── main_inspect.py          # 主入口: 巡检+诊断+自愈闭环 + 分级诊断 (v2.4)
├── pyproject.toml           # uv 依赖
├── ROADMAP.md               # 21 项前沿迭代方向 + v2.x 落地章节
└── README.md
```

## 核心设计

### 1. 混合决策架构 (反 LLM 幻觉)

```
Inspector (代码兜底, v2.7 后 0 次 LLM):
  阶段 1: K8s API 强制扫描 → 全部异常 (规则保证, 一个不漏)
  阶段 2: 应用 YAML 忽略策略 (v2.6, 提前过滤已知噪音 ns/Pod)
  阶段 3: 严重度分类走规则 (avoid LLM 误判, 含 ImagePullError 等"卡死状态"优先)
  输出:  代码生成 Top N 概览 (无 LLM, 直接给运维一眼看到优先级)

调度器 (v2.4 分级诊断):
  critical/high → 完整 ReAct (8 步)
  medium        → 轻量 ReAct (3 步)
  low           → 仅写审计, 不调 LLM
```

LLM 推理全部留给后续 Investigator / Remediator / Validator, 三处都有"代码强制规则"兜底:
- R1 强制规则: BarePod / Job → action=none (重启没用)
- R2 强制规则: StatefulSet + CrashLoop → restart_statefulset_pod L2
- R3 强制规则: RCA 命中"重启无救"黑名单 → action=none + escalate_human (v2.2)
- 调度器分组按 `(ns, type, service_prefix)` 三维度 (v2.5, 避免同 ns 不同服务被误合并)

### 2. ReAct / Function Calling 双路径 + 代码兜底

```python
# Function Calling 模式 (v2.9 默认, vLLM bind_tools 自动透传)
for step in range(_MAX_STEPS):
    resp = _llm_with_tools.invoke(msgs)
    tool_calls = resp.tool_calls or []

    # 空 tool_calls → 二次校验是真 final 还是草稿 (v2.11)
    if not tool_calls:
        if _looks_like_draft(resp.content) or (step == 0 and not evidence):
            # 草稿 / 零工具就 final → 注入纠错提示重试 1 次
            empty_call_retry += 1
            if empty_call_retry >= 2: return None  # 走代码兜底
            msgs.append(HumanMessage("[严重错误] 你没调用工具也没给合法 final, 必须二选一..."))
            continue
        return _parse_fc_final(resp.content)  # 真 final, 解析三行格式

    # 防循环 (v2.10): 同 (tool, sorted_args) 第 3 次重复 → 强制中断
    for tc in tool_calls:
        call_counter[(tc.name, args_key)] += 1
        if call_counter[...] >= 3: return None
        # 第 2 次重复 → 追加 nudge 提示让 LLM 换工具或收尾

# ReAct 字符串解析模式 (v2.0-v2.8, 通过 USE_FUNCTION_CALLING=false 回退)
# LLM 输出 {"action":"use_tool"/"final", ...} 用正则提取

# 关键: LLM 没 final 时不让它二次推理, 用代码兜底拼装保守结论
return _build_fallback_from_real_evidence(evidence)
```

**Investigator system prompt 方法论化 (v2.10+)**: 不再写"碰到 X 用 Y", 改成教 4 层根因模型 +
Hypothesis-Verify-Refine 方法论. LLM 自己推理 "重启 2500 次还崩 → 排除容器进程层 → 落 Host 层",
而不是关键词匹配. 实测 Qwen2.5-32B / DeepSeek-V3 在同一 prompt 下诊断质量几乎一致.

### 3. 同类去重: 同根因故障合并诊断

```python
# 同 namespace + 同异常类型 + 同服务前缀 (v2.5) 才归为一组
# v2.5 修复: 之前只看 (ns, type) 会把 dcgm-exporter / device-plugin-patch /
# kube-external-auditor 都归到 (kube-system, CrashLoopBackOff) 一组, 误诊
groups = group_by(issues, key=("namespace", "type", "service_prefix"))
representatives = [max(g, key=restarts) for g in groups]
# 结果: N 个异常 → K 个真实独立服务/根因 → 节省 (N-K) 次 LLM 调用

# 排序优先级 (v2.5):
# 1. severity (critical > high > medium > low)
# 2. 卡死状态 (ImagePullError/ConfigError 等永不自愈) 优先
# 3. restarts 数 (大 → 小)
# → ImagePullError (restart=0) 不会被 high+restart=666 的 Pod 挤掉
```

### 4. 故障 Memory + 失败再诊断闭环 (v2.3)

```python
# Investigator 入口: 同指纹 (ns + alertname + RCA 前 100 字符) 直接复用
# → 同样的故障下次秒级响应, 0 次 LLM 调用
cached = lookup(fingerprint)
if cached and confidence == "高":
    state["from_memory"] = True
    return cached  # 跳过 4-8 次 LLM

# Validator failed 时: 跳回 Investigator 重新诊断 (默认最多 2 次)
# 带上 last_failed_plan + 失败原因, 让 LLM 知道"刚才试过 X 但没救"
if validation_result["status"] == "failed" and retry_count < SELF_HEAL_MAX_RETRIES:
    return "investigator"  # 闭环重诊
```

### 5. VictoriaMetrics multi-tenant 适配

排查到生产环境数据存储于特定 tenant 而非默认 tenant 0:
```python
VMSELECT_URL = "http://<vmselect-cluster-ip>:8481/select/<tenant-id>/prometheus"
```

### 6. Pod 日志含 previous 崩溃前日志 + 多容器全覆盖 (排障关键)

```python
# 普通 kubectl logs 拿不到崩溃前的内容
# v2.5 升级: 同时遍历 spec.containers + spec.init_containers (避免 init 阶段故障漏读)
for c in init_containers + main_containers:
    current = _v1.read_namespaced_pod_log(name, ns, container=c.name)
    prev    = _v1.read_namespaced_pod_log(name, ns, container=c.name, previous=True)
# 多容器 Pod (DaemonSet 通常 2-3 个 sidecar) 都能拿到, 不再漏
```

### 7. Langfuse 全链路 Trace 监控

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

### 8. v2.0 自愈闭环: L1-L4 安全分级

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

### 9. L2 异步审批 (CLI + IM)

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

### 10. IM 推送 + 本地审计兜底

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
- uv (https://astral.sh/uv) — Python 包管理器, 安装见 [3.1 安装 uv](#31-安装-uv-python-包管理器-一次性-后续命令都靠它)
- containerd / docker (跑 vLLM 与 Langfuse 容器)
- NVIDIA GPU (实测 2 卡 Tesla T4 跑 Qwen2.5-32B-AWQ TP=2; 7B/14B FP16 也支持)
- 可访问 K8s 集群 (`~/.kube/config`)
- 可访问 VictoriaMetrics 或 Prometheus (PromQL 兼容)
- (可选) Langfuse 服务 (开源, 支持本地容器化部署)

## 快速开始

### 1. 启动本地 LLM (vLLM 容器)

**推荐: Qwen2.5-32B-Instruct-AWQ (INT4 量化, T4×2 够用, 能力远超 7B)**

```bash
# 拉镜像
nerdctl pull docker.m.daocloud.io/vllm/vllm-openai:v0.6.6.post1

# 下模型 (从 ModelScope, 国内最快). 注意 AWQ 量化版才能塞进 T4×2,
# FP16 全量需要 ~64GB 显存. 整体下载约 18GB.
mkdir -p /root/models
uv run --with modelscope python -c "from modelscope import snapshot_download; snapshot_download('Qwen/Qwen2.5-32B-Instruct-AWQ', cache_dir='/root/models')"

# 验证下载完整 (5 个分片都齐): 实战踩过缺分片的坑导致输出乱码
ls /root/models/Qwen/Qwen2.5-32B-Instruct-AWQ/*.safetensors | wc -l   # 应该 5

# 启动 (2 卡 T4 + TP=2 + AWQ INT4)
nerdctl run -d --name vllm-qwen32b --gpus all --net=host \
    -v /root/models:/models --shm-size=16g --ipc=host \
    docker.m.daocloud.io/vllm/vllm-openai:v0.6.6.post1 \
    --model /models/Qwen/Qwen2.5-32B-Instruct-AWQ \
    --served-model-name qwen2.5-32b \
    --host 0.0.0.0 --port 8001 \
    --quantization awq \
    --dtype=half --tensor-parallel-size 2 \
    --gpu-memory-utilization 0.92 --max-model-len 8192 --enforce-eager \
    --enable-auto-tool-choice --tool-call-parser hermes
```

启动成功验证:
```bash
curl http://localhost:8001/v1/chat/completions -H "Content-Type: application/json" -d '{
  "model": "qwen2.5-32b",
  "messages": [{"role": "user", "content": "用 3 句话说明 OOMKilled"}],
  "max_tokens": 200, "temperature": 0.3
}'
```

**备选: Qwen2.5-7B (FP16, 推理更快但能力弱一些)**

```bash
# 把 --model / --served-model-name 改成 7B, 去掉 --quantization 即可
uv run --with modelscope python -c "from modelscope import snapshot_download; snapshot_download('Qwen/Qwen2.5-7B-Instruct', local_dir='/root/models/Qwen2.5-7B-Instruct')"

nerdctl run -d --name vllm-qwen --gpus all --net=host \
    -v /root/models:/models --shm-size=8g --ipc=host \
    docker.m.daocloud.io/vllm/vllm-openai:v0.6.6.post1 \
    --model /models/Qwen2.5-7B-Instruct \
    --served-model-name qwen2.5-7b \
    --host 0.0.0.0 --port 8001 \
    --dtype=half --tensor-parallel-size 2 \
    --gpu-memory-utilization 0.85 --max-model-len 8192 --enforce-eager
```

**模型对比 (实测)**:
| 模型 | 显存 (T4×2) | 单次 RCA 耗时 | 工具调用稳定性 | R3 触发率 |
|---|---|---|---|---|
| Qwen2.5-7B FP16 | ~14GB | 5-10s | 不稳, 经常输出 markdown wrap | 25%+ |
| Qwen2.5-32B AWQ INT4 | ~26GB | 15-30s | 稳, 能正确遵循 Owner 铁律 | < 5% |

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
```

**3.1 安装 uv (Python 包管理器, 一次性, 后续命令都靠它)**

```bash
# 联网环境一键安装 (官方脚本)
curl -LsSf https://astral.sh/uv/install.sh | sh

# 加进当前 shell 的 PATH (重开 shell 后自动生效)
source $HOME/.local/bin/env
# 或临时:
export PATH="$HOME/.local/bin:$PATH"

# 验证
uv --version
```

**内网环境** (服务器无外网, 离线装 uv):

```bash
# 在能上网的机器上下载:
#   https://github.com/astral-sh/uv/releases/latest
# 选 uv-x86_64-unknown-linux-gnu.tar.gz, 解压后 scp 二进制到服务器:
scp uv root@<server>:/usr/local/bin/
ssh root@<server> 'chmod +x /usr/local/bin/uv && uv --version'
```

**3.2 安装项目依赖**

```bash
uv sync
```

uv 会自动创建 `.venv` 并按 `pyproject.toml` + `uv.lock` 把依赖装齐.
后续所有命令都用 `uv run python ...` (uv 自动激活 .venv).

**3.3 (可选) 不装 uv 用 pip 也行**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
# 后续命令把 "uv run python" 改成 "python" 即可
```

**3.4 常见坑 (新环境部署可能踩到)**

跨机器部署时, `uv sync` 偶尔在某些机器上失败. 几乎都是 wheel 兼容性问题, 不是项目代码问题.

| 报错关键字 | 根因 | 解决 |
|-----|-----|-----|
| `Failed to build tiktoken==0.x.x ... error: can't find Rust compiler` | tiktoken 部分版本 wheel 限 `manylinux_2_28` (glibc 2.28+). CentOS 7 / 老内核拿不到 wheel, 源码编需要 Rust 工具链. | 见下方"路径 A/B/C" |
| `Failed to build greenlet==3.x.x ... C++11 ... constexpr does not name a type` | greenlet 3.x 用了 C++11, 老 gcc 4.8.x 编不动. | 升级 gcc 到 7+, 或锁 `greenlet<3.0` |
| `IndentationError: unexpected indent`(uv run python -c "..." 多行) | `python -c` heredoc 多行写法对缩进敏感. | 改成单行带 `;` 分号 |

**tiktoken Rust 编译问题的 3 条解决路径**:

```bash
# 路径 A: 装 Rust 工具链 (一劳永逸, 推荐) — 联网环境
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
source $HOME/.cargo/env
rustc --version    # 验证
# 然后重新 uv sync, 会编译 tiktoken (~1-2 分钟), 编完进 cache 复用

# 路径 B: scp 老机器已编好的 wheel cache (无外网最快路径)
# 在能跑通的老机器上:
tar czf /tmp/uv-cache.tar.gz -C $HOME/.cache uv
# scp 到新机器:
scp /tmp/uv-cache.tar.gz root@<新机器>:/tmp/
# 在新机器上:
mkdir -p $HOME/.cache && tar xzf /tmp/uv-cache.tar.gz -C $HOME/.cache/
cd aiops-multi-agent && rm -rf .venv && uv sync
# (前提: 两机 glibc + Python ABI 一致)

# 路径 C: 锁老版本 tiktoken (有 manylinux2014 wheel) — 不需要 Rust
# 改 pyproject.toml 加约束:
#   "tiktoken>=0.7,<0.8"
# 删 lock 重 sync:
rm -rf .venv uv.lock && uv sync
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
export SELF_HEAL_MAX_RETRIES=2    # v2.3 闭环重诊最大次数 (默认 2)
export VALIDATOR_WAIT_SEC=30      # v2.0 Validator 等待 Pod 稳定的秒数 (默认 30, sync 路径用)

# v2.12 异步验证 (默认开, 不阻塞调度; 后台 daemon 30s/2min/10min 三轮复查)
export VALIDATOR_ASYNC=true       # false 回到 v2.11 sync 30s 行为
export VERIFIER_LOOP_SEC=5        # worker 主循环扫表间隔 (默认 5s)
export AIOPS_DB_PATH=data/aiops.db   # 速率限制 + 异步任务表统一存储
export METRICS_INSPECTOR_ENABLED=true   # v2.12 指标层巡检, false 关闭
export PROM_BASE_URL=http://<prom-host>:port/select/1/prometheus   # 同 MetricsInspector 和 mock_tools.prometheus_query 共用

# v2.13 Investigator 自主执行只读 shell (默认开)
export READONLY_EXEC_ENABLED=true       # false 一键关掉 ssh / kubectl exec
export SSH_USER=root                    # ssh 登录用户 (默认 root)
export SSH_KEY_PATH=                    # 默认空, 用系统 ssh-agent 或 ~/.ssh/id_rsa
export SSH_STRICT_HOST_CHECK=no         # 兼容跳板机, 生产建议 yes
export SSH_CMD_TIMEOUT_SEC=10           # 单命令超时 (默认 10s)

# v2.14 审批命令 daemon (默认开, 让 LLM 能申请突破白名单)
export APPROVAL_EXEC_ENABLED=true       # false 关闭, 现有只读工具不受影响
export APPROVAL_EXEC_LOOP_SEC=5         # daemon 扫表间隔 (默认 5s)
export APPROVAL_EXEC_TTL_SEC=1800       # 审批 TTL, 超时自动 expired (默认 30min)

# ⚡ v2.14 自动审批模式 (测试环境用!!!)
# 设 true 后 with_approval 命令派单后立即标 approved, daemon 秒级执行,
# 不需要人手 approve. 硬黑名单/速率/reason 校验仍生效, 只是跳过人审这一步.
# 生产环境务必 false (默认).
export APPROVAL_AUTO_APPROVE=false

# IM 通知 (可选, 不设则只写本地 alerts/ 文件)
export IM_PROVIDER=infoflow         # infoflow / dingtalk / wecom / feishu
export IM_WEBHOOK_URL='http://<im-api-host>/...?access_token=xxx'
export IM_TOID='[<group-id>]'        # 仅 infoflow 需要 (JSON 数组字符串)

# v2.8 多集群 region 标识 (告警 IM 第一行会带 [region])
export REGION=prod-bj                # 可选, 默认 "default"

# v2.8 LLM 工厂 (5 个 Agent 全部走 tools/llm_factory.build_llm)
# 全局默认 (5 个 Agent 共享):
export LLM_MODEL=qwen2.5-32b
export LLM_BASE_URL=http://localhost:8001/v1
export LLM_API_KEY=dummy

# 角色专属覆盖 (优先级高于全局)
# 例: 让关键的 Investigator/Remediator 走云 API 强模型, 其他保留本地:
# export INVESTIGATOR_MODEL=deepseek-chat
# export INVESTIGATOR_BASE_URL=https://api.deepseek.com/v1
# export INVESTIGATOR_API_KEY=sk-xxx
# export REMEDIATOR_MODEL=deepseek-chat
# export REMEDIATOR_BASE_URL=https://api.deepseek.com/v1
# export REMEDIATOR_API_KEY=sk-xxx

# Function Calling 一键回退 (v2.9, 默认开)
# export USE_FUNCTION_CALLING=false   # 调试或 vLLM 不支持 tool_calls 时回退 ReAct

# v2.3 故障 Memory + 审批 SQLite (持久化路径, 可选)
export FAULT_MEMORY_DB_PATH=data/fault_memory.db    # 默认就这个
export FAULT_MEMORY_TTL_SEC=3600                     # 复用 TTL (默认 1h)
export APPROVAL_DB_PATH=data/approvals.db
export APPROVAL_TTL_SEC=1800

# 永久生效:
# echo 'export LANGFUSE_PUBLIC_KEY="..."' >> ~/.bashrc
```

**4.1 (可选) 配置忽略策略 (v2.6)**

让某些 namespace / Pod 不进巡检流水线 (比如监控团队独立维护的 ns, 压测 Pod 等).

```bash
# 复制模板成实际配置 (示例文件不会被服务读, 必须改名)
cp config/policies.yaml.example config/policies.yaml

# 编辑, 按需取消注释 + 改 namespace 名
vim config/policies.yaml
```

`config/policies.yaml` 长这样:

```yaml
ignores:
  # 整 ns 忽略
  - namespace: "monitoring"
    reason: "监控团队独立维护"

  # ns + pod glob 通配 (* 任意, ? 单字符)
  - namespace: "ci"
    pod_pattern: "*-test-*"
    reason: "CI 临时 Pod"

  # ns + 精确 pod 名
  - namespace: "default"
    pod: "my-known-broken-pod"
    reason: "已知配置问题, 等下次发版修"
```

改完不需要重启, 每轮巡检自动重新加载. 也可以用 `--policies <path>` 指定自定义文件.

**4.2 (可选) 多集群部署**

每个集群一份环境变量, 通过 `REGION` 区分告警来源:

```bash
# 北京集群
export REGION=prod-bj
export KUBECONFIG=~/.kube/config-bj
uv run python -u main_inspect.py

# 上海集群 (另一个进程 / 另一台机器)
export REGION=prod-sh
export KUBECONFIG=~/.kube/config-sh
uv run python -u main_inspect.py
```

两个进程的 IM 推送在同一个群里也能立刻区分:
```
🟠 [HIGH] [prod-bj] AIOps 告警
🔴 [CRITICAL] [prod-sh] AIOps 告警
```

### 5. 跑一轮巡检 + 诊断

```bash
# 默认: Top 50 组 + 同类去重 + 单次模式
uv run python -u main_inspect.py

# 关闭去重对比效果
uv run python -u main_inspect.py --no-dedup --top 5

# 不限制诊断组数 (大集群全量诊断, 配合同类去重 + Memory 也能 hold)
uv run python -u main_inspect.py --top 0

# 用自定义策略文件
uv run python -u main_inspect.py --policies my-policies.yaml

# Investigator 步数调高 (32B 复杂场景可能需要)
uv run python -u main_inspect.py --deep-steps 10

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

# 跑全部 pytest 单测 (v2.1 - v2.11, 共 ~220 用例, 纯函数 + monkeypatch, 不连外部)
uv run --with pytest pytest \
  tests/test_remediator_post_process.py \
  tests/test_validator_futility.py \
  tests/test_approval_target_check.py \
  tests/test_remediation_actions.py \
  tests/test_triage.py \
  tests/test_remediator_r3.py \
  tests/test_v22_actions.py \
  tests/test_approval_target_v22.py \
  tests/test_inspector_severity.py \
  tests/test_fault_memory.py \
  tests/test_graph_routing.py \
  tests/test_scheduler_grouping.py \
  tests/test_policy.py \
  tests/test_llm_factory.py \
  tests/test_function_calling.py \
  -v
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

--top INT          每轮最多诊断的异常组数 (去重后, 含 critical/high/medium),
                   默认 50, 设 0 表示不限制
--no-dedup         关闭同类去重 (默认开启)
--interval INT     循环间隔秒数, 0=单次模式 (默认 0)
--deep-steps INT   Investigator/Inspector 深入调查最大步数 (默认 8, 32B 建议 6-10)
--policies PATH    忽略策略 YAML 文件路径 (默认 config/policies.yaml)
```

## 技术栈

| 层 | 选型 |
|---|---|
| Agent 编排 | LangGraph 0.2.x + LangChain 0.3.x |
| LLM 推理 | vLLM 0.6.6 (容器化) + Tensor Parallel TP=2 |
| 模型 | Qwen2.5-32B-Instruct-AWQ (INT4 量化, T4×2) — 升级前是 7B FP16 |
| 监控 | VictoriaMetrics multi-tenant (PromQL 兼容) |
| K8s 接入 | kubernetes Python SDK + AppsV1Api (Deployment/RS 操作) |
| 可观测性 | Langfuse 2.x (本地容器化部署) |
| 持久化 | SQLite (审批 + 故障 Memory) |
| HTTP | httpx + FastAPI |
| 包管理 | uv (Python 3.11) |
| 配置驱动 | YAML (PyYAML) — v2.6 忽略策略 |

## 已知局限与 Roadmap

### 已知局限

- 重复异常通过 v2.3 故障 Memory 复用诊断结论 (1h TTL), 但 TTL 过期后还是会重新跑 LLM
- 诊断推 IM 之外没有持久化(本地有 alerts/, 缺集中检索)
- 本地 Qwen2.5-32B-AWQ 在某些复杂多容器场景仍有 fallback (可通过 LLM 工厂切到云 API 强模型混合架构)

### Roadmap (摘要)

完整 21 项前沿迭代方向请见 [ROADMAP.md](./ROADMAP.md)

**v1.1 已完成 ✅** (2026.06)
- [x] 同类去重 + 分级诊断 (覆盖率 ~100%, LLM 调用 ↓60%, 默认 --top 50)
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

**v2.1-v2.2 已完成 ✅** (2026.06)
- [x] R1/R2/R3 强制规则 (Owner 铁律 + StatefulSet 重启分流 + "重启无救"识别)
- [x] target sanity check (ApprovalGate 校验 target 必须出自原始告警, 抵御 LLM 幻觉)
- [x] Validator "重启无救"型故障升级 (RunContainerError / ImagePullBackOff 等 → escalate_human)
- [x] 修复动作库扩展: cordon_node / uncordon_node / scale_deployment / rollback_deployment (10 个动作)
- [x] Executor T-1 实存性预检 + scale_deployment 参数透传 (delta / replicas)

**v2.3 已完成 ✅** (2026.06)
- [x] 失败再诊断闭环 (Validator failed → 跳回 Investigator, 默认最多 2 次)
- [x] 故障 Memory SQLite (同指纹 1h 复用, 重复故障秒级响应)

**v2.4-v2.7 已完成 ✅** (2026.06)
- [x] 分级诊断 (critical/high 完整 / medium 轻量 / low 仅审计, 不丢任何异常)
- [x] 调度器排序"卡死状态优先" (ImagePullError 不再被高 restart 故障挤掉)
- [x] 调度器分组三维度 `(ns, type, service_prefix)` (同 ns 不同服务不再被误合并)
- [x] 多容器日志 (init containers + 屏幕证据 1500 字)
- [x] YAML 忽略策略 (config/policies.yaml, 配置驱动)
- [x] 删除冗余 Inspector LLM 阶段 (2/4), Inspector 现在 0 次 LLM 纯代码逻辑
- [x] 升级 Qwen2.5-32B-AWQ (代码兼容旧 7B, 通过 LLM 工厂切换)

**v2.8 已完成 ✅** (2026.06)
- [x] LLM 工厂 `tools/llm_factory.py` (全局 + 角色级 env, 支持混合架构)
- [x] REGION 多集群标识 (IM 推送第一行带 `[region]`)
- [x] 5 个 Agent 抽出 ChatOpenAI 实例化代码

**v2.9 已完成 ✅** (2026.06.26)
- [x] **Function Calling Native**: Investigator 升级到 OpenAI 原生 tool_calls 协议
- [x] `tools/tool_schemas.py` 4 工具严格 JSON Schema, langchain `bind_tools` 自动透传 vLLM
- [x] `agents/investigator.py` 重构 `_run_function_calling()` / `_run_react()` 双路径
- [x] 收尾解析改自然语言"根因/置信度/关键证据"三行格式 + 多行/中英文冒号兼容
- [x] 单测 `tests/test_function_calling.py` (12 用例覆盖 FC final 解析边界)
- [x] 一键回退: `USE_FUNCTION_CALLING=false`

**v2.10 已完成 ✅** (2026.06.26)
- [x] **Investigator prompt 方法论化**: 重写 `_SYSTEM_TPL` 教 4 层根因模型 + Hypothesis-Verify-Refine,
  不再"碰到 X 用 Y" 关键词匹配, LLM 能自主推理 "重启 2500 次还崩 → 排除容器进程层"
- [x] **防循环计数器**: 同一 (tool, sorted_args) 第 2 次追加 nudge 提示, 第 3 次强制中断 → 代码兜底
- [x] **参数纠错注入**: 工具返回 `TypeError unexpected keyword argument` 时自动追加 schema 提示
- [x] **`get_pod_logs` 接受 `previous` 参数**: LLM 显式传 `previous=true` 不再 TypeError
- [x] **R3 黑名单扩展 10 个关键词**: 覆盖 GPU 驱动 (NVML / DCGM / could not load) + 挂载错 (invalid mount / OCI runtime)
- [x] **`_parse_fc_final` 多行匹配**: 关键证据支持跨行 (`[\s\S]+?` 非贪婪), 不再把多条证据合成 "无"

**v2.11 已完成 ✅** (2026.06.26)
- [x] **草稿检测器 `_looks_like_draft`**: Function Calling 空 `tool_calls` 时二次校验
  - 缺"根因:"标签 + 出现"假设/第一步/```json/我打算"等草稿特征 → 判定草稿
  - step 0 零工具就 final → 100% 判定草稿
  - 草稿强制重试 1 次 + 注入"必须 (A)调工具 或 (B)按三行格式 final"纠错提示
  - 重试仍失败走代码兜底, 不无限循环
- [x] **system prompt 第五节重写**: 明确写"用文字描述工具调用 = 无效", 附 4 个反例
- [x] 实测彻底治住 Qwen-32B / DeepSeek-V3 偶发"思考过程当 final"问题

**v1.2 计划 (未来)**
- [x] ~~Function Calling Native~~ ✅ v2.9 已完成
- [ ] Critic Agent (CRITIC 论文范式, 反 LLM 幻觉) — 注: v2.10/v2.11 的草稿检测 + 防循环 + R3 强制规则已覆盖大部分场景, Critic 优先级可降低
- [ ] **SOP 知识库注入** (新增, 把沉淀的 SOP 文档接进 Investigator prompt, 按 alertname / 关键词匹配)
- [ ] **修复建议生成 Agent** (新增, action=none 的"重启无救"故障也给可粘贴的具体命令 / runbook / git PR diff 草案)
- [ ] Eval Set + LLM-as-Judge (回归测试, 把跑通的真实 case 固化成 `tests/test_real_cases.py`)
- [ ] Tool Result Caching (5min TTL)

**v3.x 自愈闭环深化 + 学术前沿**
- [ ] L2 飞书/钉钉/如流交互式按钮 (双向 webhook, 替代 CLI 审批)
- [ ] Validator 异步 30s/2min/10min 三次检查 (当前仅 30s 同步)
- [ ] 修复历史 SQLite 检索 + 频次统计仪表盘
- [ ] 微软 GraphRAG 知识库 (基于服务依赖图谱回答全局根因)
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
