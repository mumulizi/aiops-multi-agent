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

#### 🎯 v2.0 真实集群跑通成果

一次完整巡检周期 (数十个真实异常 Pod, Top N 同类去重后) 的 5/5 决策全部正确:

| Pod 类型 (代表) | Owner | Plan | 决策路径 | 实际结果 |
|-----|-----|-----|-----|-----|
| 静态 Pod 实例 ×3 | BarePod | action=none | SKIP (LLM 自动遵循 Owner 铁律) | ✅ 不动裸 Pod |
| 某 StatefulSet 实例 | StatefulSet | restart_statefulset_pod L2 | human_review → IM 推审批 | ✅ CLI approve 后真重启 |
| 某 CSI DaemonSet 实例 | DaemonSet | restart_pod L3 | AUTO | ✅ 删→重建→ready=True |
| 某监控 DaemonSet 实例 | DaemonSet | restart_pod L3 | AUTO | ✅ 删→重建 |
| 某 debug 类裸 Pod | BarePod | action=none | SKIP | ✅ LLM 主动给 none |

**端到端验证完成的能力**:
- ✅ 5 类 owner_kind (RS/DS/SS/Job/BarePod) 严格分发, 不再误判 (静态 Pod 的 Node 引用归 BarePod)
- ✅ L2 审批走"IM 推送 + SQLite 持久化 + CLI approve" 完整闭环, 真实 StatefulSet 重启被人手 approve 后触发
- ✅ L3 自动修复在生产集群上多次成功 (DaemonSet 删→重建)
- ✅ Validator 老实标 pending, 不掩盖失败 (重启 30s 内未 ready 不报 success)
- ✅ Triage 节点保留原始 labels (含 owner_kind/alertname), 修复了 Remediator 后处理拿不到 owner 的 bug
- ✅ ApprovalGate target sanity check (target 必须出自原始告警, 抵御 LLM 幻觉编造 pod 名)
- ✅ Executor T-1 实存性预检 (调用 K8s API `read_namespaced_pod` 二次确认, 404 直接 abort)

**强制规则 R1/R2 这次没出场**: 因为 LLM 在 prompt 中正确遵循了 Owner 铁律. R1/R2 是兜底安全网, 不是日常代码路径——它没出场就是它在做对的事.

#### 🎯 v2.1 半成品: Validator "重启无救"型故障升级

实战中发现某些 Pod 重启后仍处于 `RunContainerError` (启动参数路径错 / 镜像配置错), 单纯重启无意义. 加了一条规则:

- 在 `_capture_pod_state` 中收集所有容器的 `state.waiting.reason`
- Validator + CLI approve 检查 `_NON_RESTARTABLE_REASONS` 集合: `RunContainerError` / `CreateContainerConfigError` / `CreateContainerError` / `InvalidImageName` / `ImageInspectError` / `ErrImagePull` / `ImagePullBackOff` / `ErrImageNeverPull`
- 命中 → `status=escalate_human` (区别于 success/failed/pending), IM 用 🚨 图标推送, 提示"根因不在 runtime, 请人工检查配置/镜像/启动参数"
- 用途: 告诉运维"我重启过了, 但这种状态再重启 1000 次也是同样的错, 请去看 ConfigMap / 镜像 / 启动命令"

#### 🎯 v2.1 收尾: 关键路径单测 (5 个测试文件)

为 v2.1 的强制规则 / 安全防线补 pytest 单测, 防止后续重构悄悄退化:

| 文件 | 覆盖目标 | 用例数 |
|-----|-----|-----|
| `tests/test_remediator_post_process.py` | `_post_process_plan`: action= 前缀清洗 + target 校正 + R1 (BarePod/Job → none) + R2 (StatefulSet+CrashLoop → restart_statefulset_pod L2) | 14 |
| `tests/test_validator_futility.py` | `_diagnose_restart_futility`: 8 类 "重启无救" reason 全覆盖 + 边界 (None/空 dict/未知 reason) | 17 |
| `tests/test_approval_target_check.py` | `_validate_target_in_alerts`: target 与告警一致 / LLM 幻觉编 target / 格式错 / 空告警 / 兼容旧顶层 ns 字段 | 12 |
| `tests/test_remediation_actions.py` | L2/L3 白名单注册表分离 + `is_action_allowed` 双查 + `restart_statefulset_pod` owner 校验 (用 monkeypatch 替 _v1) | 14 |
| `tests/test_triage.py` | v2.1 关键修复: triage 必须保留原始 labels (含 owner_kind), 否则 R1/R2 全部失效 | 7 |

服务器跑法: `cd aiops-multi-agent && pytest tests/test_remediator_post_process.py tests/test_validator_futility.py tests/test_approval_target_check.py tests/test_remediation_actions.py tests/test_triage.py -v`

(以上 5 个测试文件全部是纯函数 + monkeypatch, 不连 K8s / vLLM / IM / Langfuse, 服务器无依赖也能跑.)

### v2.2 完全托管 — 扩动作库 + R3 强制规则 (2026.06.25)

实战发现 5 个 Agent 都升级到 Qwen2.5-32B-AWQ 后, 诊断质量大幅提升,
但暴露出动作库太薄: 只有删/重启 Pod, 修不了 "改副本数 / 回滚 Deployment / 节点失联" 这类故障.

#### R3: "重启无救" 强制规则 (Remediator 层防误修)

实战 case (某 StatefulSet Pod):
- RCA: `exec: stat /<app-path>/config.yaml: no such file or directory`
- 旧版结果: R2 强制 → restart_statefulset_pod L2 (走人审, 但重启 1000 次也救不了)
- 新版结果: R3 拦截 → action=none, escalate_human=True, IM 用 🚨 推

R3 命中条件 (二选一):
- RCA 文本里出现 `_R3_RCA_HINTS` 关键词 (no such file / flag not defined / executable not found / errimagepull / configmap / secret / ...)
- alertname 在 `_R3_ALERT_TYPES` 集合 (RunContainerError / ImagePullBackOff / CreateContainerConfigError / ...)

R3 执行后:
- action 强制改 none (即使 R2 之前给了 restart_statefulset_pod)
- safety_level=N/A, target 清空
- 标 `escalate_human=True` (Notifier 用 🚨 而不是普通的 ⚠ / ✓)
- `_overridden` 字段记录 R3 触发原因, 供调试用

#### 新增 5 个动作 (覆盖率 30% → ~70%)

L3 自动 (新增 2):
- `cordon_node`: 标节点不可调度 (kubectl cordon). 适用节点频繁失联/磁盘满.
  完全可逆, 不动存量 Pod. target 是 node 名 (无 namespace).
- `uncordon_node`: 反操作.

L2 人审 (新增 2):
- `scale_deployment`: 调副本数. target=`namespace/deployment-name`.
  必须额外指定 `replicas=N` (绝对值) 或 `delta=±N` (相对增减).
  安全边界: |delta|<=5, 最终 replicas<=50.
- `rollback_deployment`: 回滚到上一个 ReplicaSet (kubectl rollout undo).
  实现方式: 列 deployment 关联的所有 RS, 按 revision 排序, 取倒数第二个的 template patch 回去.
  限制: 至少要有 2 个 revision.

#### Approval Gate target sanity check 升级

旧版只认 `ns/pod` 格式. 新版按 action 类型分发:
- `cordon_node` / `uncordon_node` → 校验 node 必须出自告警 `.labels.node`
- `scale_deployment` / `rollback_deployment` → 仅校验 namespace 命中告警 (deployment 名信任 LLM)
- 其他 (Pod 级) → ns + pod 严格双匹配 (与之前一致)

#### Executor T-1 预检调整

旧版对所有 action 都做 Pod 实存性预检, 新版按 `POD_LEVEL_ACTIONS` 集合判断:
- 是 Pod 级 → 调 `read_namespaced_pod` 校验存在性
- 是 node / deployment 级 → 跳过 T-1, 由各自 action 函数自检 (它们都会先 `read_node` / `read_namespaced_deployment`)

scale_deployment 透传 plan.extra: Executor 和 CLI approve 都从 `plan.extra` 拿 `replicas` / `delta`,
让 LLM 决定具体扩缩参数, 但安全边界由代码强制.

#### 单测 (3 个新文件, 共 ~60 个用例)

| 文件 | 覆盖目标 | 用例数 |
|-----|-----|-----|
| `tests/test_remediator_r3.py` | R3 强制规则: alertname 命中 / RCA 命中 / 与 R1/R2 优先级 / 边界 | 18 |
| `tests/test_v22_actions.py` | cordon/uncordon/scale/rollback 全部走 monkeypatch (FakeNode/FakeDep/FakeRS) | 30 |
| `tests/test_approval_target_v22.py` | target sanity check 三种 action 形态 + Pod 级回归 | 12 |

服务器跑法 (v2.2 部分):
```
pytest tests/test_remediator_r3.py tests/test_v22_actions.py tests/test_approval_target_v22.py -v
```

### v2.3 完全托管闭环 — 失败再诊断 + 故障 Memory (2026.06.25)

v2.0/v2.1/v2.2 之后管子已通, 但每次故障还是从零开始诊断. v2.3 解决两个真实痛点:

1. **修复失败丢给人**: Validator 标 failed 之后只能等下一轮巡检 (5-10min 后),
   人工不介入就是"故障未解决持续 N 分钟". 改成自动跳回 Investigator 重诊.
2. **重复故障重复算**: 同一个 OOM 反复出现, 每次都让 LLM 跑 4-8 次推理.
   实战经验里 80% 的故障是重复的. 加 Memory 层秒级响应.

#### 修复失败自动再诊断闭环

LangGraph 加一条条件边: `validator → investigator (failed + retry<MAX) | notifier (其他)`

```
… → Executor → Validator
                  │
        ┌─────────┴─────────┐
        │ failed (retry<2)  │ success/pending/escalate/skipped
        ▼                   ▼
   Investigator         Notifier
   (带上次 plan + 失败原因)
```

实现细节:
- `AlertState` 加 `retry_count` / `last_failed_plan` / `last_failure_reason`
- `Investigator` 入口检查 retry_count, 改 Prompt 加上"上次试过什么, 为什么失败, 避免同样方案"
- `Investigator` 出口 retry_count++, Validator 路由按新值判断
- 重试上限通过环境变量 `SELF_HEAL_MAX_RETRIES` (默认 2) 配置
- `Validator` failed 时写 last_failed_plan/last_failure_reason 到 state, 给下次 Investigator 用

#### 故障 Memory (SQLite 持久化)

新模块 `tools/fault_memory.py`, 表 `fault_memory`:

```sql
fingerprint TEXT PRIMARY KEY,  -- md5(ns + alertname + rca前100字符)[:12]
namespace, alertname, rca_text, plan_json, confidence (高/中/低),
hits INT,                       -- 命中次数, 高频故障排行
first_seen, last_used, last_success, ttl_sec  -- TTL 默认 1h
```

API:
- `generate_fingerprint(ns, alert, rca)` → 12 位指纹 (大小写不敏感, 空格归一化, 只取前 100 字符)
- `lookup(fp, only_high_confidence=True)` → 命中且未过期且置信度=高 才返回
- `record_success(fp, ns, alert, rca, plan, confidence)` → Validator 成功时写
- `record_hit(fp)` → 命中复用时 hits +1
- `list_hot(limit=10)` → 高频故障排行
- `forget(fp)` → 人工清理
- `stats()` → 总条数 / 总命中 / 平均命中

集成点:
- **Investigator 入口** (retry_count==0): 用 `summary` 当 RCA 签名生成 fp, lookup 命中 →
  直接把 cached.rca/plan 写进 state, 标 `from_memory=True`, **跳过 LLM 推理** (省 4-8 次调用)
- **Remediator 入口**: 看到 from_memory=True 直接复用 plan, 跳过 LLM
- **Validator success 时**: 提取 RCA 里的置信度, 写 `record_success(fp, ...)` (失败的 case 不入库)

#### 实战价值 (预估)

| 场景 | v2.2 | v2.3 |
|-----|------|------|
| 同 Pod 频繁 OOM | 每次 5-8 次 LLM 调用 (~30-60s) | 第二次起 0 次 LLM, 秒级 |
| 修复失败 (Validator failed) | 等下一轮巡检 (5-10min) | 立即重诊 (最多 2 次, ~1min 内) |
| 同类故障群 (5 个 Pod 同 OOM) | 5 次完整诊断 | 第一次诊断, 后 4 次 Memory 命中 |

#### 单测 (2 个新文件, ~45 个用例)

| 文件 | 覆盖 | 用例数 |
|-----|-----|-----|
| `tests/test_fault_memory.py` | generate_fingerprint / record_success / lookup / record_hit / TTL / 置信度过滤 / forget / stats / list_hot | 26 |
| `tests/test_graph_routing.py` | _route_after_validator: failed → investigator / 达上限 → notifier / 各种状态 / 环境变量覆盖 | 18 |

每个测试都用临时 SQLite 文件 (tmp_path fixture), 互不污染.

服务器跑法 (v2.3 部分):
```
pytest tests/test_fault_memory.py tests/test_graph_routing.py -v
```

### v2.4 全量诊断 — 调度器排序修复 + 分级诊断 (2026.06.25)

v2.3 跑通后实战暴露两个真实问题:

**问题 1**: 7 个 ImagePullError Pod 全部排在 Top 3 之后, 没进流水线
- 调度器 `_group_similar_issues` 按 (severity, restarts) 排, ImagePull 的 restart=0
- 同 high 的某 namespace Unhealthy (restart=666) 排在前面把它挤掉了
- 结果: 集群里 7 个真故障 (镜像拉不下来) 持续无人诊断

**问题 2**: medium / low 级异常被 critical/high 硬过滤直接丢弃
- `priority = [i for i in issues if sev in ("critical", "high")]`
- 24 个 medium/low 异常 (有些是 "重启 5 次, 即将爆") 完全没进流水线
- 这违背"无遗漏"设计原则

#### 修复 1: 调度器排序加"卡死状态优先"

`main_inspect.py:_group_similar_issues` 排序逻辑:

```
旧:  (severity, -restarts)
新:  (severity, is_stuck ? 0 : 1, -restarts)
```

`is_stuck` 判定: type/reason 含 imagepull/errimage/configerror/runcontainererror 等 8 种关键词.

效果: 同 severity 时, 卡死故障 (restart=0 但永不自愈) 优先于反复重启故障.

#### 修复 2: 分级诊断, medium 也走流水线

不再用 critical/high 硬过滤, 改成按 severity 分阶段:

| Severity | 处理 | LLM 调用 |
|---|---|---|
| critical/high | 完整 ReAct (8 步) | 5-8 次 |
| medium | 轻量诊断 (3 步快诊) | 2-3 次 |
| low | 仅写审计, 不调 LLM | 0 次 |

实现:
- `AlertState` 加 `investigation_mode: "full" | "light"`
- 调度器把 `investigation_mode=light` 写入 medium 组的 initial_state
- `Investigator` 入口读 mode, light 模式 max_steps 改成 3
- `low_issues` 全部写 `record_audit` 但不进流水线

#### 修复 3: 默认 `--top` 从 20 → 50, 且支持 `--top 0` 不限制

```
旧: --top 默认 20 (实际跑 Top 3 时把卡死故障漏掉)
新: --top 默认 50, 配合同类去重 + Memory 已能覆盖大集群
    --top 0 表示不限制
```

#### 实战预期 (再跑一次同样的 54 异常集群)

| 指标 | v2.3 | v2.4 |
|------|------|------|
| critical/high 进流水线 | 4 组 (Top 3 限制) | 全部 7 组 |
| ImagePullError 被诊断 | ❌ 漏 | ✅ 排第一 |
| medium 进流水线 | ❌ 全丢 | ✅ 轻量诊断 |
| low 可见性 | ❌ 静默丢 | ✅ 审计文件 |
| 总 LLM 调用 | 3-4 次 | ~10-15 次 (可接受, 32B 一次 RCA ~30s) |

#### 单测 (1 个新文件, 13 个用例)

| 文件 | 覆盖 | 用例数 |
|-----|-----|-----|
| `tests/test_scheduler_grouping.py` | 排序优先级 (critical>high) + 卡死状态优先 (ImagePullError 优先于 Unhealthy) + 同类去重 (ns+type 归组) + 边界 | 13 |

服务器跑法 (v2.4 部分):
```
pytest tests/test_scheduler_grouping.py -v
```

### v2.5 准确性优先 — 分组按服务前缀 + 多容器日志 + 屏幕证据 (2026.06.25)

实战暴露 3 个准确性问题:

**问题 1**: 同类去重把不同服务合并 (运维误判)
- 旧分组键: `(namespace, type)` → 同 ns 下所有 CrashLoopBackOff 归一组
- 实际 case: 某 namespace 下 3 个完全不同的服务 (service-A / service-B / service-C)
  都是 CrashLoopBackOff, 被合并到一组, 只诊断代表 service-A
- 后果: service-B/C 被错误地套上 service-A 的诊断结论
- **修复**: 加 `service_prefix` 维度, 按 owner_kind 提服务前缀:
  - ReplicaSet: 去 pod 名最后 2 段 (`<deploy>-<rs-hash>-<pod-hash>`)
  - DaemonSet/StatefulSet/Job/BarePod: 去最后 1 段
  - 新分组键: `(namespace, type, service_prefix)`
- **设计原则**: 运维准确性高于 LLM 成本.
  分错故障 → 误诊 → 运维去修错的东西; 多调 N 次 LLM → 慢点而已. 选准确性.

**问题 2**: 多容器 Pod 漏读 init container 日志
- `get_pod_logs` 旧版只遍历 `p.spec.containers`, 跳过 `init_containers`
- 后果: Pod 启动失败在 init 阶段 (e.g. config 初始化容器崩了), 故障信息看不到
- **修复**: 同时遍历 `spec.containers + spec.init_containers`,
  每个容器都打标签 (`=== init container: foo ===` / `=== container: bar ===`),
  单容器日志限制提到 3000 字 (从 2000), 多容器累计才不会爆 LLM 上下文

**问题 3**: 屏幕显示截断让运维误判
- Investigator/Inspector 的 step 输出在屏幕上只显示前 200/120 字符
- 多容器日志输出长, 屏幕上只看到第一个容器的开头, 误以为系统漏了
- **修复**: 屏幕截断从 200 提到 1500 字, 超过时显式提示完整长度
  ```
  [Investigator]  step 0: 结果 (前 1500 字, 完整 4823 字):
  <真实日志>
  ... (截断剩余 3323 字)
  ```
- LLM 拿到的上下文从 1500 字提到 3000 字 (32B 上下文够用)

#### 实战效果 (再跑同一个 54 异常集群)

| 指标 | v2.4 | v2.5 |
|------|------|------|
| 单 ns 下不同服务的 CrashLoop 组数 | 1 (混在一组) | N (按服务分别成组) |
| 不同服务的故障被独立诊断 | ❌ 套用代表 Pod 结论 | ✅ 各自拿自己的日志独立诊断 |
| init container 故障可见 | ❌ 漏 | ✅ 单独标记日志 |
| 屏幕证据可读性 | 200 字截断 | 1500 字 + 长度提示 |
| 单轮 LLM 调用 | 7 次 | ~15 次 (可接受, 准确性优先) |

#### 单测扩展

`tests/test_scheduler_grouping.py` 加 6 个用例:
- `test_same_ns_type_diff_prefix_NOT_grouped`: 实战 case 回归, 不同服务必须分别成组
- `test_replicaset_prefix_drops_two_hash_segments`: RS Pod 名去 2 段
- `test_daemonset_prefix_drops_one_segment`: DS Pod 名去 1 段
- `test_statefulset_prefix_drops_ordinal`: STS Pod ordinal 当一段
- `test_barepod_prefix_drops_one_segment`: 静态 Pod (node-ip 后缀)
- 边界: 单段 pod 名 / 短名兜底

### v2.6 忽略策略 — YAML 配置忽略 namespace / Pod (2026.06.25)

实战需求: 集群里有些 namespace / Pod 是**已知噪音**, 不该进流水线浪费 LLM:
- 监控团队独立维护的 ns
- CI 临时 / 压测 Pod (启停频繁, 不是异常)
- 已知 broken Pod, 等下次发版修

#### YAML 配置驱动 (config/policies.yaml)

```yaml
ignores:
  # 整 ns 忽略
  - namespace: "monitoring"
    reason: "监控团队独立维护"

  # ns + 精确 pod 名
  - namespace: "default"
    pod: "my-debug-pod"
    reason: "已知 debug Pod"

  # ns + glob 通配 (* 任意, ? 单字符, [abc] 字符集)
  - namespace: "ci"
    pod_pattern: "test-*"
    reason: "CI 临时 Pod"
```

#### 实现要点

- **过滤时机**: `_run_cycle_body` 入口 (Inspector 之后, 调度器之前). 最早过滤,
  下游所有逻辑 (去重 / 分级 / 诊断 / Memory) 都不会看到被忽略的 Pod
- **每轮重新加载**: 改了 YAML 不需要重启服务, 下一轮巡检自动生效
- **优雅降级**: 文件不存在 / 格式错 / PyYAML 未装 → 都不阻塞主流程,
  打印警告并当作"无策略"处理
- **可见性**: 命中策略的 Pod 在屏幕上显示"忽略 N 个 (规则: monitoring 整 ns: ...)"
  按 reason 聚合, 不刷屏
- **CLI**: `--policies path/to/your.yaml` 指定, 默认 `config/policies.yaml`,
  可通过环境变量 `POLICIES_FILE` 覆盖

#### 模板与单测

- `config/policies.yaml.example` 带详细注释的模板 (复制成 `policies.yaml` 后改)
- `tests/test_policy.py` 22 个用例:
  - load: 文件缺失 / 空 / 注释 / 格式错 / 顶层非 dict / 正常
  - match: 整 ns / ns+pod 精确 / ns+pod_pattern (含 ? 和 [abc])
  - filter: 批量 / 顺序优先 / 边界

服务器跑法:
```
pytest tests/test_policy.py -v
```

#### 实战预期

| 场景 | 之前 | v2.6 |
|---|---|---|
| monitoring 团队独立 ns | 每轮都查, 浪费 LLM | YAML 加 `namespace: monitoring` 直接跳 |
| 压测 Pod 启停反复 | 每次都进流水线 | `pod_pattern: load-*` 全跳过 |
| 已知问题 Pod | 反复推 IM | `namespace: x, pod: y` 精确跳过 |
| 改完策略 | 改代码 + 重启 | 改 YAML, 下一轮自动生效 |

### v2.7 简化 — 删除冗余 LLM 阶段 + 策略前移 (2026.06.25)

实战发现两个浪费 + 一个 bug:

**浪费 1**: Inspector 阶段 2 "Top 5 LLM 深入预览"
- 写死 Top 5, 调度器还要再完整诊断一遍 (信息冗余)
- 32B 一次 ~30-60s, 每轮巡检多花的纯浪费

**浪费 2**: Inspector 阶段 4 "LLM 整体摘要"
- 用 LLM 写一句 "集群有 N 个问题..." 中文摘要
- 调度器最后总结里有完整 reports, 这句摘要价值有限

**Bug 3**: 策略过滤位置太晚, Top 10 显示被忽略的 Pod
- 之前 v2.6 把过滤放在 `main_inspect._run_cycle_body`, 在 Inspector 已经
  打印完 Top 10 之后
- 视觉上看到 abcd 的 Pod 还在 Top 10 里 (虽然实际调度器拿到的是 23 个,
  过滤了 9 个), 容易误判为"策略没生效"

#### 改动

- Inspector 现在纯代码逻辑, 0 次 LLM 调用:
  - 阶段 1: 收集真实异常 (K8s API)
  - 阶段 2: 应用策略过滤 (前移)
  - 输出 Top 10 (代码生成, 无 LLM, 仅 namespace/pod/type/restarts/owner)
- 删 `_react_deep_dive` (107 行) + `_llm_overview` (20 行)
- 删 `DEEP_TOOLS` / `DEEP_TOOL_DESCS` / `_extract_json` 等只供阶段 2 用的死代码
- `agents/inspector.py` 从 332 行 → 175 行 (-47%)
- 策略过滤从 `main_inspect._run_cycle_body` 移到 `agents/inspector.run_inspector`,
  保证 Top 10 显示已过滤后的列表

#### 实战预期

| 阶段 | v2.6 | v2.7 |
|---|---|---|
| Inspector LLM 调用 | 1-2 次 (阶段 2 + 阶段 4) | 0 次 |
| 单轮 Inspector 耗时 | ~60-120s (含 LLM) | ~5-10s (纯 K8s API) |
| Top 10 显示 | 含被忽略 Pod (视觉 bug) | 已过滤 |
| 整体单轮巡检耗时 | ~3-6 min | ~2-5 min |

#### config/policies.yaml.example pod_pattern 写法补充

补了详细示例和"踩坑写法":

```yaml
# pod 名 "kube-external-auditor-192.168.48.78"
#   - "kube-external-auditor-*"   ✓ 命中 (服务名开头, 匹配尾部 hash/IP)
#   - "*kube-external-auditor*"   ✓ 命中 (包含即可)
#   - "*-kube-external-auditor-*" ✗ 不命中 (要求前面必须有 -)
```

### v2.8 多集群部署 — LLM 工厂 + region 标识 (2026.06.25)

接入多个集群/系统的部署需求, 加了两个东西:

#### 1. tools/llm_factory.py — 统一 LLM 客户端工厂

5 个 Agent 的 ChatOpenAI 实例化代码全部抽出, 改成 `build_llm(role, ...)`.
模型/端口/key 通过环境变量切换, 不需要改代码.

**两层环境变量**:

```
# 全局 (5 个 Agent 共享)
LLM_MODEL=qwen2.5-32b
LLM_BASE_URL=http://localhost:8001/v1
LLM_API_KEY=dummy

# 角色专属 (优先级高于全局)
INVESTIGATOR_MODEL=deepseek-chat
INVESTIGATOR_BASE_URL=https://api.deepseek.com/v1
INVESTIGATOR_API_KEY=sk-xxx
```

**典型混合架构**:

让 Investigator/Remediator (诊断 + 决策, 质量要求高) 走云 API 强模型,
其他 Agent (Classifier/Aggregator, 简单任务) 保留本地 32B:

```bash
# 关键 Agent 用 DeepSeek
export INVESTIGATOR_MODEL=deepseek-chat
export INVESTIGATOR_BASE_URL=https://api.deepseek.com/v1
export INVESTIGATOR_API_KEY=sk-xxx
export REMEDIATOR_MODEL=deepseek-chat
export REMEDIATOR_BASE_URL=https://api.deepseek.com/v1
export REMEDIATOR_API_KEY=sk-xxx
# 其余走本地 32B (默认就是)
uv run python -u main_inspect.py
```

#### 2. REGION 标识符 — 多集群告警来源区分

`tools/llm_factory.py:get_region()` 提供统一入口:

```python
# 优先级: REGION > AIOPS_REGION > "default"
```

集成点:
- `tools/im_notify.py:format_alert_message` 第一行加 `[{region}]` 标签:
  `🔴 [CRITICAL] [prod-bj] AIOps 告警`
- `agents/notifier.py:notifier_node` 终端输出标题 + 增加一行 `region: prod-bj`

**多集群部署示例**:

```bash
# 北京生产集群
export REGION=prod-bj
export KUBECONFIG=~/.kube/config-bj
uv run python -u main_inspect.py

# 上海生产集群 (另一个进程 / 另一个机器)
export REGION=prod-sh
export KUBECONFIG=~/.kube/config-sh
uv run python -u main_inspect.py
```

两个集群的告警在同一个 IM 群里也能立刻分辨来源.

#### 抽出的浪费 + 升级 max_tokens

顺手做了:
- Investigator / Remediator 的 `max_tokens` 从 512 → 1024 (避免 RCA / plan 被截)
- 删除每个 Agent 文件里的 `_callbacks = [LANGFUSE_HANDLER] if ...` 重复样板,
  统一到 build_llm 内部

#### 单测

- `tests/test_llm_factory.py` (新增, 15 个用例):
  - `_resolve`: 默认 / 全局 env / 角色 env / 优先级 / 大小写
  - `build_llm`: kwargs 透传 / max_tokens 可选 / 混合架构场景
  - `get_region`: REGION / AIOPS_REGION fallback / 优先级 / 空字符串

服务器跑法:
```
pytest tests/test_llm_factory.py -v
```

### v2.9 Function Calling Native — Investigator 改用 OpenAI 原生工具调用 (2026.06.26)

**问题**: v2.0 起 Investigator 一直走 ReAct 字符串解析:
- LLM 输出 JSON 文本 → 代码用 `_extract_json` 正则提取
- 32B 输出基本稳定, 但偶尔被 markdown wrap (` ```json ... ``` `) / 多余 "好的我来分析:" 前缀
  / 中间空字段搞翻车, 这种 case 触发的 R1/R2/R3 强制规则其实是给"坏格式"擦屁股
- v2.0 起 vLLM 启动参数已经加了 `--enable-auto-tool-choice --tool-call-parser hermes`,
  但代码层一直没用上, 长期浪费

**改动**: 升级 Investigator 到 OpenAI Function Calling 协议
- `tools/tool_schemas.py` (新增): 4 个工具的严格 JSON Schema (含 type / required / enum)
- `agents/investigator.py`: 重构成 `_run_function_calling()` + `_run_react()` 双路径
- 默认走 FC, 通过 `USE_FUNCTION_CALLING=false` 一键回退 ReAct
- `langchain-openai` 的 `bind_tools(schema)` 自动透传给 vLLM, langchain 自动把
  `tool_calls` 转 ToolMessage 回喂, 零字符串解析

#### 模式对比

| 维度 | ReAct (v2.0-v2.8) | Function Calling (v2.9) |
|---|---|---|
| LLM 输出 | 文本 + 内嵌 JSON | 结构化 tool_calls 数组 |
| 解析 | 正则 + try/except | langchain 自动转 |
| 失败模式 | JSON wrap / 多余前缀 / 字段空 | 几乎不会 |
| 多工具并发 | 不支持 (一步一调) | 支持 (一轮 tool_calls 多个) |
| 旧后端兼容 | 任何 OpenAI 兼容服务 | 要求 vLLM 0.6+ 且开 --enable-auto-tool-choice |

LLM 收尾 (final) 时:
- ReAct: 输出 `{"action":"final","hypothesis":"...","confidence":"...","key_evidence":[...]}`
- FC: 不调工具, 直接自然语言三行 (根因 / 置信度 / 关键证据), `_parse_fc_final` 抽取

#### 兼容性 / 切换

```bash
# 默认开启 (推荐, 32B 体验显著更好)
uv run python -u main_inspect.py

# 一键回退 ReAct (vLLM 不支持 tool_calls / 调试时用)
export USE_FUNCTION_CALLING=false
uv run python -u main_inspect.py
```

旧 ReAct 路径完整保留, 没删. Memory 命中 / 闭环重诊 / 分级诊断 / 代码兜底 都跟旧版一致.

#### 单测

- `tests/test_function_calling.py` (新增, 12 个用例): 覆盖 `_parse_fc_final`
  - 标准三行格式 (中英文冒号)
  - 多条证据分隔符 (`,` / `；` / `\n`)
  - 缺字段兜底 (无证据 / 无置信度)
  - 自由文本兜底 (LLM 没按格式输出整段当 hypothesis)
  - 边界 (空字符串 / None / 超长截断 / 不吸下一字段)

服务器跑法:
```
pytest tests/test_function_calling.py -v
```

#### 实战预期

| 现象 | v2.8 | v2.9 |
|---|---|---|
| Investigator 给非 JSON 输出 | 偶尔, 触发 `JSON 解析失败` | 几乎绝迹 |
| R1/R2/R3 强制规则触发率 | ~5-10% | 预期 ↓ (LLM 输出更稳, 格式不再翻车) |
| 多工具并行 | 一步一个 | 复杂诊断可一轮调 2 个 |

### v2.10 方法论提示词 + 防循环 / 防呆三道闸 (2026.06.26)

v2.9 切到 Function Calling 后实战暴露 3 类问题, 全部在工程层(非模型层)解决:

**问题 1**: prompt 写"碰到 X 用 Y"的关键词匹配, 换故障类型立刻翻车
- 旧版 prompt 列举"ImagePullBackOff → restart_pod_for_image_pull / OOM → check resource"等映射
- 实战 case: 集群里多了"GPU 驱动 NVML 加载失败"和"OCI invalid mount"两类没列举的故障 → LLM 给 restart_pod 误判
- 用户反馈: **"现在是遇到一个修复一个 case 打补丁一样, 为什么不能让他知道遇到问题后分析问题"**

**问题 2**: LLM 编造 Pod 名死循环
- 实战 case: device-plugin-patch 诊断时 LLM 想做"横向验证"
  但没数据来源, 自己编了 3 个不存在的 Pod 名 (`gpu-pod-12345` / `nvidia-device-plugin-abc` 等)
  反复调 `kubectl_describe` → 全 404 → 继续编 → 直到 max_steps 才停
- 一个故障烧掉 4-6 次 LLM 调用, 完全浪费

**问题 3**: `get_pod_logs` 参数 schema 不匹配
- LLM 受 prompt 引导主动传 `previous=true`, 但 wrapper 不接受 → 每次都 TypeError
- 实测一次巡检触发 12 次 TypeError

#### 修复 1: prompt 重写成方法论 (不是关键词表)

`_SYSTEM_TPL` 完全重写, 引入 4 层根因模型 + Hypothesis-Verify-Refine 方法:

```
一. 根因层级模型 — 所有 K8s 故障都落在这 4 层之一
1) 容器进程层: 进程崩了/退出码非0/panic/业务逻辑错 → 业务方/镜像作者修
2) Pod 配置层: 启动命令错/env错/挂载错/资源 limit 不够 → YAML 维护者修
3) Host/Node 层: 节点磁盘满/driver没装/containerd配置坏 → 节点运维修
4) 集群/控制面层: apiserver慢/etcd抖/DNS挂/CNI异常 → 平台运维修

二. 调查方法 (Hypothesis → Verify → Refine)
每步: 假设根因在哪层 → 调最能验证假设的工具 → 看结果支持/推翻 → final 或修正假设
质量优先, 步数其次. 拿到精确错误原文 (Back-off pulling / invalid mount /
flag not defined) 立即 final, 不要凑步数.

三. 判断层级的关键启发式:
"这个错重启 Pod 能修吗?"
- 能修 → 多半 1 层
- 重启 100 次还崩 → 升级 2/3 层
- restart_count 几千次 → 几乎必然 2/3/4 层, 不是 1 层
```

实战效果: 集群里出现的"GPU NVML 加载失败"prompt 没列举, 但 LLM 自主判 Host 层 + 写 RCA:
`"Host 层 nvidia driver / NVML 库未正确加载, restart=2588 (重启 N 次仍崩, 排除容器进程层)"`

#### 修复 2: 防循环 / 防呆三道闸

`agents/investigator.py:_run_function_calling()` 加 3 道闸:

**闸 1 - 参数纠错注入**: 工具返回字符串里出现 `TypeError ... unexpected keyword argument`
- 自动追加 schema 提示: `"[提示] 请只使用 schema 声明的参数 (name/namespace/lines/previous)"`
- LLM 下一步会自动修正参数 (实测 100% 生效)

**闸 2 - nudge**: 同一 `(tool, sorted_args)` 第 2 次出现
- 追加 HumanMessage: `"[提示] 你刚才重复调用了同一个工具+参数, 换工具或换参数或基于已收集证据 final"`
- 让 LLM 自己跳出来, 不强行中断

**闸 3 - 强制中断**: 同一 `(tool, sorted_args)` 第 3 次
- `_log("⚠ 同一调用已第 3 次, 判定 LLM 卡死, 中断进代码兜底")`
- 返回 None → 代码兜底拼装保守结论
- 实测一次诊断从 6-8 次 LLM 调用降到最多 4 次

#### 修复 3: `get_pod_logs` 接受 `previous` 参数

`tools/mock_tools.py` wrapper 改成 `(name, namespace, lines=30, previous=True)`,
`tools/tool_schemas.py` 同步声明 `previous` 字段. LLM 显式传不再 TypeError.

#### 修复 4: R3 黑名单扩展 10 个关键词

`agents/remediator.py:_R3_RCA_HINTS` 加:
- 挂载层: `invalid mount` / `bind mounts cannot have` / `failed to create containerd task` / `oci runtime create failed`
- GPU 驱动: `nvml` / `could not load` / `dcgm initialization` / `cuda error` / `no such device` / `failed to initialize`

实战命中: dcgm-exporter (restart=2589) / device-plugin-patch (restart=2588) 这类
"重启 1000 次都救不了" 的 Host 层故障, R3 强制 `action=none` + `escalate_human=True`,
不让 LLM 出 restart_pod 馊主意.

#### 修复 5: `_parse_fc_final` 多行匹配 + "无" 过滤

旧版正则 `关键证据[:：]\s*(.+?)` 只能匹配单行, LLM 给多行证据时全部丢失 → 输出 "关键证据: 无".

新版 `[\s\S]+?` 非贪婪跨行匹配 + 排除 `"无"` / `"(无)"` 开头的伪证据:

```python
key_evidence_raw = _grab(r"关键证据[:：]\s*([\s\S]+?)(?=\n(?:根因|置信度)[:：]|$)")
key_evidence = [s.strip() for s in re.split(r"[;；\n]", key_evidence_raw)
                if s.strip() and not s.strip().startswith(("无", "(无)"))]
```

#### 实战效果对比 (同一个 5-case 集群)

| 指标 | v2.9 | v2.10 |
|---|---|---|
| 平均诊断步数 | 4-6 | 1-3 |
| 编造 Pod 名循环 | 偶发 4-6 步 | 第 3 次强制中断 (上限 3 步) |
| TypeError 触发 | 一轮 12 次 | 0 次 |
| Host 层 / Pod 配置层正确分类 | 80% | 100% (5/5) |
| `action=restart_pod` 误判率 (重启无救型) | 10% | 0% (R3 黑名单扩展后) |

#### 模型可替换性验证

同 prompt + 同代码下分别用 Qwen2.5-32B (本地) 和 DeepSeek-V3 (API) 跑同一个 5-case 集群:
- 诊断结论: **5/5 case 层级判断完全一致**
- 关键证据: 完全一致 (都引用工具输出原文)
- 步数差异: < 1 步

结论: **代码层做对后, 模型选择影响有限**. 业界"AIOps 必须用 GPT-4 / Claude"的论调
在结构化场景下被反证. 生产可用 Qwen-32B, 调试可对照 DeepSeek 当回归基线.

### v2.11 草稿检测器 — 拦截"思考过程当 final 输出" (2026.06.26)

v2.10 解决了"LLM 编造 Pod 名循环", 但又发现新问题:

**问题**: Function Calling 模式下, Qwen / DeepSeek 偶尔会**返回空 tool_calls 但内容是草稿**
- Case A (dcgm-exporter): step 0 直接返回 `"首先, 根据告警信息, Pod 在 ... 出现了 CrashLoopBackOff... ### 假设 1. 容器进程层 ..."`
  没调任何工具, 还是思考过程, 但 tool_calls 是空的 → 旧逻辑当真 final 处理 → 下游 Remediator 拿"假设1..."当 RCA, 给出 restart_pod L3 (差点假修复一个已重启 2585 次的 Pod)
- Case B (device-plugin-patch): step 0 返回 `"第一步应该调用 kubectl_describe... \`\`\`json {name: 'kubectl_describe', arguments: {...}} \`\`\`"`
  在 markdown 里描述工具调用, 但 tool_calls 字段空 → 被当 final → 下游 Remediator 给 restart_pod

**根因**: 32B / 671B-MoE 模型在 Function Calling 协议下都不是 100% 严格遵循,
**"想调工具" 和 "真发起 tool_calls"** 是两件事. 协议层必须有兜底.

#### 修复: `_looks_like_draft()` 二次校验 + 强制重试

`agents/investigator.py`:

```python
def _looks_like_draft(content: str) -> bool:
    """判定"空 tool_calls 回答"是不是草稿"""
    text = content.strip()
    # 合法 final 必有"根因:"标签
    if re.search(r"根因\s*[:：]", text): return False
    # 缺标签 + 出现草稿关键词 → 草稿
    draft_signals = ("假设", "第一步", "应该调用", "我们需要", "我打算",
                     "我将", "我会", "下一步", "```json", "```\n")
    if any(sig in text for sig in draft_signals): return True
    # 太短也算草稿
    if len(text) < 60: return True
    return False

# _run_function_calling 主循环里
if not tool_calls:
    is_draft = _looks_like_draft(resp.content)
    has_no_evidence = step == 0 and not evidence  # 还没调工具就 final → 100% 幻觉
    if is_draft or has_no_evidence:
        empty_call_retry += 1
        if empty_call_retry >= 2:
            return None  # 重试 1 次仍失败 → 代码兜底
        # 注入"必须二选一"纠错提示
        msgs.append(HumanMessage(
            "[严重错误] 你没调用工具也没给合法 final, 必须二选一:\n"
            "(A) 通过 function_call 接口发起 tool_calls, 不要用文字描述\n"
            "(B) 严格按三行格式 final: 根因:/置信度:/关键证据:"
        ))
        continue
    return _parse_fc_final(resp.content)  # 真 final
```

#### system prompt 第五节重写

明确写**"用文字描述工具调用 = 无效输出, 会被判定草稿要求重做"**, 附 4 个反例:

```
错误示例 (会被判定草稿, 强制重做):
- "首先, 根据告警信息, Pod 在 ... 出现了 CrashLoopBackOff..." (思考过程, 不是 final)
- "第一步应该调用 kubectl_describe 来查看..." (描述, 不是真调用)
- "### 假设\n1. 容器进程层..." (思考, 不是结论)
- "```json\n{"name": "kubectl_describe", ...}\n```" (只是文本, 不会被当工具调用)

重要: step 0 还没有任何工具证据时, 严禁直接给 final
```

#### 实战效果 (同一 5-case 集群)

| 指标 | v2.10 | v2.11 |
|---|---|---|
| 草稿当 final 通过率 | 2/5 (dcgm + device-plugin-patch) | 0/5 |
| Remediator 拿到草稿 RCA 出 restart_pod | 2 次 | 0 次 |
| 草稿检测触发率 | - | 偶发 (实测 device-plugin-patch step 0 触发 1 次, retry 后正确给 NVML 诊断) |

实测日志:
```
[Investigator] step 0: ⚠ LLM 空 tool_calls 但内容是草稿 (retry=1), 强制重新输出
[Investigator]  step 1: 调用 get_pod_logs(...)
[Investigator] step 2: LLM 收尾 (Function Calling final)
[Investigator] 结论: Host 层 nvidia driver / NVML 库... 关键证据: panic: could not load NVML library; restart_count=2584
```

#### 跨模型验证

| 模型 | 草稿出现率 | 草稿检测触发率 | 最终诊断正确率 |
|---|---|---|---|
| Qwen2.5-32B-AWQ (本地) | ~20% | 100% 命中 | 5/5 |
| DeepSeek-V3 API | ~10% | 100% 命中 | 5/5 |

**结论**: 协议层防呆比换更强的模型更重要. v2.11 之后, **本地 Qwen-32B 在 5-case 真实集群上诊断质量与 DeepSeek-V3 几乎一致**.

---

### v2.12 P0 平台化改善 — 走出"K8s Pod 自愈"上 AIOps 平台 (2026.06.29)

#### 背景

v2.11 之后做了一次全面评估, 结论:

> **这是一个工程实践质量很高的"K8s Pod 层自愈系统", 但还不是真正意义上的 AIOps 平台.**
> 它在防 LLM 幻觉 / 安全分级 / 代码兜底等**生产工程化**层面做得比 90% 的同类项目都扎实, 但要往 AIOps 平台演进, 还差三块业界标准能力:
> 1. **检测前移**: 从"Pod 崩了再修"到"SLO 异常→主动诊断"
> 2. **业务翻译**: 从"运维严重度"到"业务影响度"
> 3. **变更关联**: 从"看 Pod 内部"到"关联近期变更"

v2.12 选了 4 项 P0 (影响正确性 + 扩展性 + AIOps 含金量) 落地. P1/P2 留到后续版本.

#### 4 项 P0 改善

| # | 项目 | 价值 | 状态 |
|---|---|---|---|
| §1 | 速率限制 SQLite 持久化 | 重启不丢状态 + 多副本部署可共享 | ✅ |
| §2 | 变更感知工具 `get_recent_changes` | 业界 80% 故障由变更引起, 这是 RCA 关键线索 | ✅ |
| §3 | MetricsInspector + 6 条内置 PromQL | 把"Pod 崩了再修"升级为"SLO 异常→主动诊断" | ✅ |
| §4 | Validator 异步化 | 调度不阻塞 + 30s/2min/10min 三轮覆盖快慢恢复 | ✅ |

#### §1 速率限制迁 SQLite

**问题**: 旧实现是 `defaultdict(deque)` 内存表 — 进程重启丢状态, 多副本各自计数失效.

**改动**: `tools/safety_guards.py` 新增 SQLite 表 `rate_limit_records` 在 `data/aiops.db`:
- `allow(target, action, max_per_hour)` 改 SQL: `DELETE` 过期 → `COUNT` 窗口 → `INSERT` 当前
- 接口签名零变化, 调用方零改动
- 审计日志保留内存 deque (本期不迁, 学习/单机够用)

**验证**: 同 target 三次 allow 后第四次 → 拒绝; kill -9 进程重启后 1h 内同 target 再触发仍被限流.

#### §2 变更感知工具

**问题**: Investigator 现有工具集都在看"Pod 现在怎么了", 完全不知道"刚才有人改了什么". 业界 AIOps 最常用的 RCA 线索是"近期变更", 这是关键缺口.

**改动**: 新建 `tools/change_tracker.py`, 注册到 Investigator 工具集 (`mock_tools.TOOLS` + `tool_schemas.TOOLS_SCHEMA`):
```python
get_recent_changes(namespace: str, hours: int = 2) → str
```
数据源 (实时查 K8s API):
- Deployment 的 revision + lastUpdateTime
- 新创建的 ReplicaSet (= 新版本部署)
- StatefulSet condition
- ConfigMap/Secret 的 managedFields 最新 update time (跳过 SA token 自动生成的)
- Events: ScalingReplicaSet / SuccessfulCreate / Killing / BackOff

按时间倒序, 最多 50 条. K8s API 失败 → 返回友好错误字符串不抛异常.

**为什么不接 CI/CD webhook**: 需要额外服务端 + 你的环境 CI 平台不确定. 后续真有需要再补.

#### §3 MetricsInspector — 指标层异常前置巡检

**问题**: 旧 Inspector 只看 Pod `phase / waiting / ready`, 漏掉 80% 真实生产故障 — Pod Running 但慢/错的 (P99 飙升 / 5xx 涨 / 内存缓慢泄漏 / 节点磁盘 IO 饱和 / 证书过期...).

**改动**:
- 新建 `agents/metrics_inspector.py`: 跟 Inspector 并行的代码型 Agent (无 LLM)
- 新建 `tools/metrics_rules.py`: 6 条内置 K8s 通用 PromQL 规则

| Rule | PromQL | 阈值 | severity |
|---|---|---|---|
| `pod_cpu_throttling` | `rate(container_cpu_cfs_throttled_seconds_total[5m])` | >0.5 | high |
| `pod_memory_near_limit` | `container_memory_working_set_bytes / spec.memory.limit` | >0.9 | high |
| `node_disk_pressure` | `1 - node_filesystem_avail / size` | >0.85 | high |
| `node_load_high` | `node_load5 / cpu_count` | >2 | medium |
| `apiserver_5xx_high` | `sum(rate(apiserver_request_total{code=~"5.."}[5m]))` | >1 req/s | critical |
| `kubelet_down` | `up{job="kubelet"} == 0` | == 0 | critical |

输出 issue 跟 Pod issue 同结构, 新加 `source=metrics` 字段区分. 调度器接入: `run_inspector + run_metrics_inspector` 结果合并到同一 issues 列表, 共用同类去重逻辑.

**`_issue_to_alert` 对 metric 来源 issue** 用 PromQL 摘要构造 description, 让 Investigator 拿到时优先用 `prometheus_query` 工具深入.

**错误兜底**: 单条规则失败 → 跳过 + 日志; 整体 crash → 调度器 catch, 不阻塞 Inspector; `METRICS_INSPECTOR_ENABLED=false` 一键关闭.

**为什么不上 YAML 配置**: YAGNI, 6 条通用规则覆盖 80% 场景, 后续真有人定制再加 YAML 覆盖层.

#### §4 Validator 异步化

**问题**: 旧 Validator 主流程内 `time.sleep(30)` — 调度被 Pod 数量线性阻塞 (20 个 Pod 都到 executor = 最坏 10min); 且 30s 太短, Pod 重启常需 60-120s 才 ready, 大量产生 `pending` 状态且没有后续异步验证补刀.

**新架构**:
```
[同步] Executor → Validator (T+0 立即返回 pending_async) → Notifier 派单通知 → END
                              ↓ 写 SQLite verification_tasks
[异步] verifier_worker daemon 线程每 5s 扫表 → 跑 30s/2min/10min 三轮验证 → 终态推第二条 IM
```

**改动**:
- 新建 `tools/verifier_store.py`: SQLite 任务表 (`enqueue` / `claim_due` / `update_status` / `list_recent`)
- 新建 `agents/verifier_worker.py`: daemon 线程, 复用 validator 的 `_diagnose_restart_futility` / `_check_pod_recreated_by_owner` / `_capture_pod_state` 逻辑 (零代码重复)
- `agents/validator.py` 拆 `_validator_async_path` / `_validator_sync_path`:
  - **异步路径** (默认): T+0 看 futile 命中 → 否则入队 → 立即返回 `pending_async`
  - **同步路径**: 保留原 30s sleep, `VALIDATOR_ASYNC=false` 一键回退
- `graph.py`: `_route_after_validator` 加 `pending_async → notifier` 分支 (不走重诊)
- `agents/notifier.py`: `_VALIDATION_ICON` 加 `pending_async=⏳` / `escalate_human=🚨`
- `main_inspect.py`: 启动时拉起 `verifier_worker` (`VALIDATOR_ASYNC=true` 时)
- 新建 `scripts/aiops_verify_status.py`: CLI 工具看任务表

**三轮验证节奏** (按 `created_at` 偏移):
- round 0 → 30s (大部分 restart_pod 的 success 在这一轮命中)
- round 1 → 2min (慢启动的 Pod 在这一轮)
- round 2 → 10min (兜底)
- round 3 仍 pending → 终态 `timeout`

**SQLite WAL** 模式保证跨线程读写安全 (跟 fault_memory 一致).

**进程 kill 重启后**: status=pending 的任务保留, worker 启动自动 pick up, `check_at` 过期就立即重试一次.

**不打通 (留 ROADMAP)**: 异步 `failed` 自动跳回 Investigator 重诊 (异步触发主图工程量大 + 失败模式多样, 容易死循环). 本期 `failed` 终态推 IM 告诉运维, 写一条 audit `should_re_diagnose`, 人工 rerun.

**v2.3 失败再诊断闭环** 仅在 sync 路径保留, 异步路径 retry_count 暂不在主图增长.

#### 跨版本对比

| 维度 | v2.11 | v2.12 |
|---|---|---|
| 速率限制 | 内存 deque, 进程级 | SQLite, 重启不丢 |
| 异常检测维度 | Pod phase/waiting/ready | + 6 条 PromQL 指标层规则 |
| RCA 上下文 | logs / describe / prom / history | + 变更追踪 (Deployment/RS/CM/Secret/Event) |
| Validator 阻塞 | 30s sleep × N 个 Pod 串行 | T+0 派单不阻塞 + 后台 30s/2min/10min |
| 验证覆盖 | 1 次 30s | 3 轮 (覆盖快慢恢复) |

#### 新环境变量

- `AIOPS_DB_PATH` (默认 `data/aiops.db`) — 速率限制 + 异步任务表统一存储
- `METRICS_INSPECTOR_ENABLED` (默认 `true`)
- `VALIDATOR_ASYNC` (默认 `true`; `false` 回到 v2.11 同步行为)
- `VERIFIER_LOOP_SEC` (默认 5, worker 主循环扫表间隔)
- `PROM_BASE_URL` (默认值跟 `mock_tools.VMSELECT_URL` 一致, 显式配置可覆盖)

#### 兼容性

允许小幅 breaking change (`VALIDATOR_ASYNC` 默认 `true`), 但提供 `VALIDATOR_ASYNC=false` 一键回退, 现有逻辑保留.

#### CLI 工具

```bash
# 看异步验证任务表
python scripts/aiops_verify_status.py                    # 全部 (pending 优先)
python scripts/aiops_verify_status.py --pending          # 只看 pending
python scripts/aiops_verify_status.py --limit 100        # 多看几条
python scripts/aiops_verify_status.py --json             # 机器可读
```

#### 未做 (留下个版本)

- **异步失败自动跳回 Investigator 重诊** — 异步路径下 retry_count 不在主图增长
- **业务影响 Agent** (P1) — 关联 SLO/QPS/受影响用户, 把"运维严重度"翻译成"业务影响度"
- **服务拓扑感知** (P1) — 区分"受影响 vs 引发", 解决"上游 A 慢 → 下游 B 重启"误诊
- **故障 Memory 换语义检索** (P1) — 跨 namespace 同类故障复用
- **诊断质量评估闭环** (P1) — 采样人工标注 + 失败 case 看板
- **变更感知接 CI/CD webhook** — 当前只看 K8s 层, 不看应用发布平台

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
1. 抽取查询中的实体 (e.g. "image-registry")
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
例如 镜像仓库挂了 → 所有依赖 镜像仓库拉镜像的 Pod 都受影响。

**做法**:
```python
# 拉 K8s 拓扑 (NetworkPolicy / Service / Ingress / 应用调用关系)
graph = build_dependency_graph()

# 当 image-registry 挂了
downstream = graph.descendants("image-registry")  # 所有下游
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
- [x] ~~S5 Function Calling Native~~ ✅ v2.9 已完成
- [x] ~~S4 历史故障 Memory~~ ✅ v2.3 已完成
- [ ] **S3 Critic Agent** (1d) — 注: v2.10/v2.11 的草稿检测 + 防循环 + R3 强制规则
  已覆盖大部分 LLM 幻觉拦截场景, Critic 优先级可往后挪 (实战 5/5 case 全对, 没出现需要 Critic 的场景)

## 第 3-4 周 (v1.2 收尾)
- [ ] **A1 Eval Set + LLM-as-Judge** (1d) — 把 v2.11 跑通的 5 个真实 case
  固化成 `tests/test_real_cases.py`, prompt 改动跑回归 (是后续所有迭代的前置条件)
- [ ] **A3 Tool Result Caching** (0.5d) — 单次巡检 PromQL ↓70%

## 第 5-7 周 (v1.3)
- [ ] **SOP 知识库注入** (1.5d, 新增) — 把沉淀的 SOP 文档接进 Investigator prompt
  - frontmatter (alertname / keywords / pod_pattern) 标触发条件
  - 匹配命中后整篇 SOP 注入 user message
  - LLM 按 SOP "诊断步骤" 走, 比方法论更精准 (方法论是 fallback)
- [ ] **修复建议生成 Agent** (1d, 新增) — action=none 的"重启无救"故障也给可粘贴的
  具体命令 / runbook / git PR diff 草案 (让 IM 通知从"需人工"变成"需人工, 建议执行 xxx")
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
- v2.1-v2.8 已完成 (2026.06):
  v2.1: R1/R2 强制规则 + target sanity check + Validator 重启无救升级 + 关键路径单测
  v2.2: R3 强制规则 + 扩动作库 (cordon/scale/rollback) + 60 用例新单测
  v2.3: 失败再诊断闭环 + 故障 Memory (SQLite, 同指纹复用)
  v2.4: 分级诊断 (critical/high/medium/low) + 卡死状态排序
  v2.5: 服务前缀三维分组 + 多容器日志 + 屏幕证据扩展
  v2.6: YAML 忽略策略
  v2.7: 删除冗余 LLM 阶段 (Inspector 0 次 LLM 纯代码)
  v2.8: LLM 工厂 (全局 + 角色级 env) + REGION 多集群标识
- v2.9-v2.11 已完成 (2026.06.26):
  v2.9: Function Calling Native (Investigator 升级 OpenAI 原生 tool_calls,
        + 自然语言三行 final 解析, vLLM bind_tools 自动透传)
  v2.10: prompt 方法论化 (4 层根因 + Hypothesis-Verify-Refine)
         + 防循环三道闸 (参数纠错/nudge/强制中断)
         + R3 黑名单扩展 10 关键词 (GPU 驱动 / OCI 挂载)
  v2.11: 草稿检测器 (拦"思考过程当 final"), 跨模型验证 Qwen / DeepSeek 诊断质量一致
- v1.2: SOP 知识库注入 (用户沉淀文档 → prompt) + 修复建议生成 Agent
        + Eval Set + LLM-as-Judge (回归测试) + Tool Result Caching
- v1.3: Topology-Aware 故障传播分析 + TimesFM 时序异常预测
- v2.x: 飞书 webhook 人审回调 + Validator 异步三次检查
  + 微软 GraphRAG 知识库 (基于服务依赖图谱回答全局根因)
  + Critic Agent (优先级降低, 现有代码层防呆已覆盖大部分场景)
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

---

### v2.13 Investigator 自主执行 (Readonly Tier A) — 实地查证升级 (2026.06.30)

#### 背景

v2.12 三次生产实跑暴露一个共性短板:

```
[Investigator] 结论: Host 层 NVIDIA 驱动 / NVML 库在 node 192.168.48.9 上未正确加载,
              节点运维需检查驱动安装和 /dev/nvidia* 设备
```

**结论是个建议, 不是实锤**. 运维拿到还得 ssh 上去查 lsmod / dmesg / nvidia-smi,
走一遍 LLM 已经"推测"过的步骤. 闭环没合上.

Claude Code 之所以好用, 是因为它能自主跑只读命令 (`cat / grep / ls / ps`) 看实际数据
再给结论. 我们的 Investigator 现在只能 `kubectl_describe / get_pod_logs / prometheus_query`
这种间接观测, 拿不到节点本地真相 (内核日志/驱动模块/设备文件/systemd 状态).

#### 改动

新增 2 个 Investigator 工具 (注册到 `mock_tools.TOOLS` + `tool_schemas.TOOLS_SCHEMA`):

| 工具 | 适用场景 |
|---|---|
| `ssh_node_readonly(node, cmd)` | Host 层: driver/kernel/设备文件/systemd 服务/dmesg 日志 |
| `kubectl_exec_readonly(name, namespace, cmd)` | 容器内部: 配置文件实际内容/env/进程/网络 |

#### 安全设计 (代码硬规则, 不依赖 LLM 自觉)

**4 道命令闸**:

1. **命令前缀白名单** (`READONLY_PREFIXES`): ls/cat/head/tail/grep/find/df/free/dmesg/
   journalctl/nvidia-smi/lsmod/lspci/systemctl/kubectl/ip/netstat/ss/ps/uname/date/env 共 40+
2. **子命令二级白名单** (`SUBCMD_WHITELIST`): kubectl 只允许 get/describe/logs/top/version/
   explain/api-resources, systemctl 只允许 status/show/list-units/list-unit-files/is-active
3. **dangerous token 黑名单** (`DANGEROUS_TOKENS`): rm/mv/cp/sed -i/重定向 >/>>/2>/&>/
   systemctl restart/kubectl apply|delete|exec|drain/curl POST/docker/podman/modprobe/
   journalctl --rotate/mount/iptables/reboot 共 60+ token
4. **节点白名单**: ssh 目标 node 必须出现在 `kubectl get nodes` (60s 缓存防编造)

**匹配技术**:
- 单词边界匹配 (`re.escape + \b`), 避免 `cat` 被 `at` 误命中
- 字符级模式拦截重定向 / 反引号 / `$()` (防 LLM 通过命令替换绕过)
- 拼接 (`;` / `&&` / `||` / `|`) 每段都走白名单, 限 5 段

**3 道资源闸**:
- 单命令 10s 超时 (`subprocess.run timeout`)
- 输出截断 4KB (stdout/stderr 各)
- 速率 `(node, cmd_prefix)` 1h 5 次, 复用 `safety_guards.allow`

#### Investigator prompt 升级

第六节"自主执行只读 shell 命令"加入 prompt, 列出 5 类强烈推荐场景:
- 怀疑驱动加载失败 → `lsmod | grep nvidia`
- 怀疑设备文件丢失 → `ls /dev/nvidia*`
- 怀疑内核报错 → `dmesg | grep -i nvidia | tail -50`
- 怀疑 kubelet/containerd 异常 → `journalctl --no-pager -u kubelet -n 100`
- 怀疑内核版本不匹配 → `uname -r` + dmesg NVRM 报错对照

反例对比 (旧 vs 新):
- 旧: 看到 NVML error → final "Host 层 driver 问题, 节点运维需检查"
- 新: 看到 NVML error → 立刻 ssh + lsmod (空!) + ls /dev/nvidia* + dmesg + uname -r
      → final "节点 X nvidia.ko 因内核 5.4→5.10 未重装 (dmesg 原文: ...)"

#### 跟现有 L3/L2/L4 修复路径的分工

```
诊断阶段 (Investigator) — v2.13 新加只读工具
  └─ 只读自主执行 → 4 道命令闸 + 3 道资源闸

修复阶段 (Remediator → ApprovalGate → Executor) — 保持现状不动
  └─ L3 自动 / L2 人审 / L4 拒绝 / R3 黑名单 — 全部保留
```

**关键设计原则**: 现有那套针对"状态变更"的安全防线 (审批 / 速率 / 业务时段 / R3)
已经够用. 不重新造一个 ssh 通道绕过它. 只读的事新加, 写的事走老路.

未来如果要做"重启节点 kubelet"这类 Host 级写操作, 走新增 L2 action 进 Approval Gate,
不会在 `ssh_node_readonly` 里偷偷开口子.

#### 新环境变量

- `READONLY_EXEC_ENABLED` (默认 `true`, false 一键关掉 ssh + kubectl exec 两个工具)
- `SSH_USER` (默认 `root`)
- `SSH_KEY_PATH` (默认空, 用系统 ssh-agent 或 `~/.ssh/id_rsa`)
- `SSH_STRICT_HOST_CHECK` (默认 `no`, 兼容跳板机; 生产建议 `yes`)
- `SSH_CMD_TIMEOUT_SEC` (默认 10)

#### 验证

ssh_tools 校验逻辑跑通:
- 22 条只读命令 (ls/cat/df/dmesg/journalctl/nvidia-smi/...) **全通过**
- 31 条危险命令 (rm/systemctl restart/kubectl delete/apt/curl POST/docker/reboot/...) **全拒绝**
- 5 个绕过尝试 (`$()` 命令替换 / 反引号 / `2>` 重定向 / `&>` / `&&` 拼接) **全拒绝**

#### 不做 (明确 YAGNI)

- 不做 ssh 写操作 (任何): rm/mv/sed -i/重定向 全部拒
- 不做 `systemctl restart` 类 (未来走 L2 Approval Gate 新增 action)
- 不做 GPU 写操作 (`nvidia-smi -r` 等)
- 不做 FixSuggester (诊断深入到拿原文证据后, 不需要再单独生成"人工命令")
- 不持久化 ssh session (每次新连接, 简化设计)


---

### v2.14 人审突破白名单 (审批命令通道) — 让 LLM 能"申请高危操作", 运维 approve 后异步执行 (2026.06.30)

#### 背景

v2.13 生产实跑后用户反馈:

> "为什么需要我设置各种命令的操作方式, 而不是直接接入模型后, 交给模型自己判断?
> DeepSeek-V3 已经比本地 Qwen 更强大, 为什么还做不到像 Opus 4.7 那样完全由模型
> 自己去理解到底该怎么做能不能执行?"

**核心答案**: Claude Code 的"自由"是错觉 — 每次 Bash 调用前有 permission prompt
让你按 yes. 生产 AIOps 无人值守, 没人按 yes. 模型强解决的是"诊断准不准", 不是
"该不该执行". 你在 Claude Code 里让 Opus 跑 `rm -rf /` 它会先问你; 它没问的,
是因为你已经按了 "Always allow Bash" 授权 — 那是你的责任, 不是模型的判断力.

**正确路径**: 让模型敢于提出高危操作, 但每次都过人审. v2.14 就是这个能力.

#### 改动

新增 2 个 Investigator 工具:

| 工具 | 用途 |
|------|------|
| `ssh_node_with_approval(node, cmd, reason, ...)` | 提交需人审的节点命令 |
| `kubectl_exec_with_approval(name, ns, cmd, reason, ...)` | 提交需人审的 Pod 内命令 |

**关键: 立即返回, 不阻塞诊断**. LLM 调用后拿到 `[已派单审批 task_id=xxx]`
提示, 本轮拿不到这条证据, 应基于现有证据 final 临时结论. 结果异步进 Memory,
下次同指纹故障可秒级复用.

#### 数据库扩展

- `approval_pending` 表加 `kind` (remediation/diagnostic_cmd) / `cmd_payload` /
  `execution_result` 3 列 (兼容旧 remediation 审批)
- 新表 `diagnostic_cmd_history` (fingerprint + cmd + exit + stdout/stderr +
  approved_by + timestamp), 供 Investigator 下次同指纹故障读取

#### 硬黑名单 (永远不入审批通道)

```
rm / dd / mkfs / fdisk / shutdown / reboot / iptables -F /
kubectl delete --all / drop database / :(){:|:&};: / docker system prune /
> /dev/sd... / modprobe / halt / poweroff
```

理由: 这类不可逆/影响面太大的操作就算运维 approve 也不该 AIOps 代跑, 必须
人工 ssh 跑, 留完整审计. 命中 → 立即返回 `[硬黑名单拒]`, 不写 SQLite, 不推 IM.

#### 分工

```
诊断阶段 (Investigator):
  ├─ 只读工具 (v2.13, 白名单硬闸)
  │   ssh_node_readonly / kubectl_exec_readonly
  └─ 需人审工具 (v2.14, 突破白名单)
      ssh_node_with_approval / kubectl_exec_with_approval
      ↓
      派单 → IM 推运维 → approve → daemon 异步执行 → 结果进 Memory

修复阶段 (Remediator → ApprovalGate → Executor) — 保持不动
  └─ L3 自动 / L2 人审 / L4 拒绝 / R3 黑名单
```

**新加的通道跟现有 L2 Approval Gate 是同一张表** (`approvals`), 只是 kind 字段区分:
- `kind=remediation`: 原有 plan 审批 (restart_pod 等修复动作)
- `kind=diagnostic_cmd`: 新增诊断命令审批

CLI `scripts/aiops_review.py` 自动区分显示 (🔍 diagnostic_cmd vs 🔧 remediation).

#### daemon worker

新建 `agents/approval_exec_worker.py` (跟 verifier_worker 同款设计):
- 每 5s 扫 `kind='diagnostic_cmd' AND status='approved' AND execution_result IS NULL`
- ssh 通道: `subprocess.run(["ssh", "-o", "BatchMode=yes", ..., cmd])`
- kubectl_exec 通道: `subprocess.run(["kubectl", "exec", "-n", ns, pod, "--", "sh", "-c", cmd])`
- 10s 超时强杀 + stdout/stderr 各 4KB 截断
- 写结果 → 推第二条 IM → 调 `fault_memory.record_diagnostic_cmd`

#### 学习闭环 (关键设计)

Investigator 命中 `fault_memory.lookup` 时, 额外查 `list_diagnostic_history(fp, limit=5)`.
拿到历史命令 + 结果拼进 user_msg:

```
📚 此故障曾审批执行过以下诊断命令 (最近 5 条, 供参考):
  - cmd: crictl pull registry.baidubce.com/foo/bar:v1
    reason: 验证镜像仓库可达 + 拉取是否成功  approved_by: opsuser  exit=1
    stderr: rpc error: code = NotFound
  - cmd: kubectl exec dcgm-exporter -- ldconfig -p | grep nvml
    reason: 查容器内动态库路径  approved_by: opsuser  exit=0
    stdout: libnvidia-ml.so.1 => /usr/lib/x86_64-linux-gnu/libnvidia-ml.so.1

提示: 若历史命令已经证实过某个根因, 可以直接 final. 若需要新证据,
     可以再申请新的 with_approval 命令.
```

**这才是真正的学习闭环** — 人审过的命令沉淀成"这个故障的排查 SOP", LLM 下次
自动复用. Anthropic prompt caching 让重复上下文几乎零成本.

#### 期望行为对比

| 场景 | v2.13 (仅只读) | v2.14 (可申请人审) |
|------|--------------|---------------------|
| ImagePullBackOff | final "镜像不存在" 建议 | final 临时结论 + 申请 `crictl pull` 验证 |
| kubelet 卡住 | final "节点问题, 请人工排查" | final 临时结论 + 申请 `systemctl restart kubelet` |
| NVML 加载失败 | ssh readonly + lsmod (若能 ssh) | + 申请 `nvidia-smi -q` 拿详细版本信息 |
| 复现故障 | 每次都跑完整流水线 | 命中 Memory + 复用历史 diagnostic_cmd 结果, 秒级 final |

#### 防滥用

- **reason 校验**: 必填 + ≥10 字, 拦 "试一下"/"看看"/"just checking" 等空话
- **速率限制**: 单 (trace_id, target) 1h 内最多 3 条审批, 防 LLM 刷屏
- **TTL**: 30min 未审批自动 expired
- **硬黑名单**: 永远不入审批通道

#### 新环境变量

- `APPROVAL_EXEC_ENABLED` (默认 `true`, false 关闭 daemon + 工具直接返回 `[已禁用]`)
- `APPROVAL_EXEC_LOOP_SEC` (默认 5, daemon 扫表间隔)
- `APPROVAL_EXEC_TTL_SEC` (默认 1800, 审批 TTL)
- 复用 `SSH_USER` / `SSH_KEY_PATH` / `SSH_STRICT_HOST_CHECK` / `SSH_CMD_TIMEOUT_SEC`

#### 兼容性

- 允许小幅 breaking change (`APPROVAL_EXEC_ENABLED` 默认 true)
- false 一键关掉 (工具仍存在, 但调用返回 `[已禁用]`)
- 现有 L2 Approval Gate (remediation 审批) 完全不动
- 现有 v2.13 只读工具 (ssh_node_readonly 等) 不动
- CLI aiops_review 向后兼容: 老 remediation 审批照原流程

#### 验证

- 硬黑名单 9 类命令全拒 + 大小写不敏感
- reason 校验拦空/短/'试一下' 空话
- 端到端: 提交 → SQLite → IM 派单 → approve → daemon 执行 → 结果 → fault_memory
- kubectl_exec 通道等效
- 速率限制: 同 trace+target 3 次后拒
- 未知 kind 走失败路径 status=failed
- TTL 过期 pending 自动标 expired
- 已执行任务不重复 pick up
