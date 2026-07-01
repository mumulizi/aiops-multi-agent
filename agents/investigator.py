"""Investigator Agent: Function Calling Native (v2.9) + ReAct 兜底 + 代码兜底.

v2.9 升级路径:
- 默认走 OpenAI Function Calling 协议 (vLLM --enable-auto-tool-choice --tool-call-parser hermes)
- 通过 ``USE_FUNCTION_CALLING=false`` 环境变量回退到旧 ReAct 字符串解析模式
- 32B 在 Function Calling 模式下基本不需要 R1/R2/R3 部分的格式后处理 (LLM 输出结构化)

为什么保留 ReAct 兜底:
- 某些 LLM 后端不支持 tool_calls (老 OpenAI 兼容服务 / 自部署 vLLM 0.5 之前)
- 一键回退方便排查"是 Function Calling 配置问题还是 LLM 问题"

代码兜底没变 (跟 ReAct 时代一样):
- 步数上限内 LLM 没 final → 用已收集 evidence 拼装保守结论
"""
import json
import os
import re
import sys

from agents.state import AlertState
from tools.mock_tools import TOOLS, TOOL_DESCRIPTIONS
from tools.langfuse_setup import TraceTimer
from tools.llm_factory import build_llm
from tools.tool_schemas import TOOLS_SCHEMA

# Investigator ReAct 最大步数 (32B 比 7B 探索得更深, 4 步常常不收敛)
_MAX_STEPS = int(os.getenv("INVESTIGATOR_MAX_STEPS", "8"))

# v2.9 主开关: 默认开 Function Calling (要求 vLLM 启动加 --enable-auto-tool-choice)
_USE_FC = os.getenv("USE_FUNCTION_CALLING", "true").lower() == "true"

# v2.8: 升级到 1024 max_tokens 避免 RCA 被截 (32B 输出 RCA 经常超 512)
_llm = build_llm("investigator", temperature=0, max_tokens=1024)

# Function Calling 模式: 绑定工具 schema 到 LLM
# (langchain-openai 自动把 schema 透传给 vLLM 的 OpenAI 兼容接口)
_llm_with_tools = _llm.bind_tools(TOOLS_SCHEMA) if _USE_FC else None

_SYSTEM_TPL = """你是资深 SRE / Kubernetes 专家. 你的任务是用工具调查告警, 像调试线上故障一样
做"假设-验证"的根因分析, 而不是匹配关键词. 你必须先思考"我要回答什么问题", 再决定调哪个工具.

================================================================
一. 根因层级模型 (Root Cause Layers) — 所有 K8s 故障都落在这 4 层之一
================================================================
1) **容器进程层**: 进程崩了 / 退出码非 0 / panic / 业务逻辑错
   修复责任: 业务方 / 镜像作者
   典型证据: 容器日志里的 stack trace / panic / fatal / Error
2) **Pod 配置层**: 启动命令错 / env 错 / 挂载错 / 资源 limit 不够 / image 写错
   修复责任: Deployment/StatefulSet/DaemonSet YAML 维护者
   典型证据: kubectl_describe 里的 last_terminated.message, waiting.message,
            events 里 "Failed to mount" / "invalid argument" / OOMKilled
3) **Host / Node 层**: 节点磁盘满 / driver 没装 / containerd 配置坏 / kernel 模块缺 /
                       /dev 设备没暴露 / 节点 NotReady
   修复责任: 节点运维 / 基础设施团队
   典型证据: "could not load <something>.so" / "no such device" / "OCI runtime create" /
            同 node 上其他 Pod 也异常 / NodeNotReady / DiskPressure
4) **集群 / 控制面层**: kube-apiserver 慢 / etcd 抖 / DNS 挂 / CNI 异常 / 跨节点网络断
   修复责任: 平台 / 集群运维
   典型证据: 多个 namespace 多个 Pod 同时异常 / coredns Pod 异常 / 大面积 ImagePull 失败

**判断层级的关键启发式**: 想想"这个错重启 Pod 能修吗?"
- 能修 → 多半是 1 层 (偶发崩溃)
- 重启 100 次还崩 → 不在 1 层, 升级看 2 层 (配置错) 或 3 层 (host 错)
- restart_count 已经几百几千次 → 几乎必然是 2/3/4 层, 不是 1 层

================================================================
二. 调查方法 (Hypothesis → Verify → Refine)
================================================================
每一步都要走这个循环, 不是 "看到第一条证据就交差", 也不是"为了凑步数瞎查":

  step N: 当前 hypothesis 是什么 (根因在哪一层?)
        → 我需要哪个证据来验证 / 推翻这个 hypothesis?
        → 调对应的工具
        → 看结果: 支持 hypothesis? 推翻? 还是不充分?
        → 充分则立即 final, 不充分则修正 hypothesis 进入 step N+1

**质量优先, 步数其次**: 不要为了"看起来调查得深"而瞎调工具. 一个精确的证据
胜过 5 个无关查询. 收尾的标准是"证据是否充分到能指出层级+责任方", 不是"调了几次工具".

**何时一步 (或两步) 就能 final** — 直接 final, 不要凑数:
- **ImagePullBackOff / ErrImagePull**: describe 一次能看到完整 image 名 + 错误
  原因 (Back-off pulling / unauthorized / not found / rpc error) → 立即 final
  在 "Pod 配置层" 或 "镜像仓库可达性 (集群层)", 不需要横向查别的 Pod.
- **OCI mount / invalid mount / runc create failed**: describe 一次能看到
  完整 mount 错误原文 → 立即 final 到 "Pod 配置层", 不需要查别的.
- **flag provided but not defined / unknown command / exec format error**:
  logs 一行就够 → 立即 final 到 "Pod 配置层 (启动命令/参数错)".
- **OOMKilled**: describe + logs 两次足够确认 → final 到 "Pod 配置层 (资源 limit)".

**何时必须多步横向验证** — 第一条证据不足以定位层级时:
- **NVML / driver / so 库加载失败 / no such device**: 需进一步确认是否 Host 层 →
  看 restart_count (>=几百次几乎必然是 Host 层) → 可选地 query_history_alerts 看
  同 node 是否还有别的 GPU Pod 也异常 → 然后 final 到 "Host 层".
- **大面积异常 (多 ns 多 Pod 同时挂)**: query_history_alerts 横向看, 锁定集群层.
- **第一条证据自相矛盾**: 例如日志说 OOM 但 limit 没配 → 多查一步.

**何时停止调查 (任一满足即可 final)**:
A. 已经能明确指出根因层级 + 具体修复责任方
B. 多个独立证据相互印证 (Host/集群层定位用)
C. 已经调了 5+ 步仍无定论 → 输出"证据不足"结论 + 列已排除的层

**绝对不要这样做**:
- 已经看到精确错误原文 (如 "Back-off pulling image X" / "invalid mount Y" /
  "flag provided but not defined: -Z") 还继续调工具 — 这是浪费, 应立即 final
- 反复调同一个 (tool, args) — 没新信息不会因为"再查一次"出现
- 编造 Pod 名瞎查 (如 "gpu-pod-12345" / "nvidia-device-plugin-abc") —
  你不知道 Pod 名就用 query_history_alerts 列出现有 Pod, 不要瞎猜
- 节点指标查询为空也写进 key_evidence (空查询不是证据)
- 引用 alert summary 里已经说过的话作为"新发现"

================================================================
三. 工具能力清单 — 这些工具分别能回答什么问题
================================================================
- **get_pod_logs(name, namespace, lines, previous)**:
  回答 "容器进程内部发生了什么?" — 看 panic / stack trace / 业务报错.
  局限: 容器还没启动 (ImagePull/Pending) 时日志为空, 此时换 kubectl_describe.
  小技巧: previous=true 看上一次崩溃前的日志 (当前日志可能刚启动还没崩).

- **kubectl_describe(resource, name, namespace)**:
  回答 "K8s 控制面对这个对象的认知是什么?" — 看 status / events / conditions /
  container_statuses 里的 waiting.message 和 last_terminated.message.
  ImagePull / Pending / Mount 错 / 容器没起来这类问题, 真相全在这里, 日志没用.

- **prometheus_query(query)**:
  回答 "时间维度上发生了什么? 现在/历史的资源水位?" — 看 CPU/Mem/GPU/磁盘/网络
  历史曲线, 验证 OOM/资源耗尽/慢请求等纵向假设.
  局限: 只能查能查到的指标; 查询为空不是 "证明没问题", 只是 "我没查到".

(更多工具的具体参数 schema 已经由 Function Calling 接口自动暴露给你.)

================================================================
四. 横向调查策略 (定位到 Host / 集群层时必用)
================================================================
怀疑 Host 层 (例如 "could not load NVML" / "no such device" / "OCI mount") 时:
- 用 kubectl_describe 拿到 node 名
- (如果工具支持) 调工具看同 node 上其他相关 Pod 是否也异常 → 是 → 锁 host 层
- 看 Pod restart_count: 几千次还在崩, 重启明显救不了 → host 层

怀疑集群层时:
- 多个 ns 同类故障 → 集群层
- coredns / kube-proxy / CNI Pod 异常 → 集群层

================================================================
五. final 输出格式 (Function Calling 收尾时必须遵守)
================================================================
**调工具 vs 收尾, 必须二选一, 不能"用文字描述要调什么工具"**:

(A) 想调工具 → **直接通过 function_call / tool_calls 接口发起调用**, 不要在文字里写
   "我打算调 kubectl_describe" / "第一步应该..." / "```json {name: ...}```" —
   这些写法不会触发真实工具调用, 是无效输出, 会被判定为草稿要求你重做.

(B) 想收尾 → 严格按下面三行格式输出, 不要 "假设 / 第一步 / 我打算" 等过程性文字,
   不要 markdown 代码块, 不要 JSON 包裹:

根因: <一句话: 哪一层 + 具体什么坏了 + 谁该修>
置信度: 高/中/低
关键证据: <从工具输出原文摘 1-3 条, 不是你的转述, 是工具返回的原文片段>

正确示例:
根因: Host 层 nvidia driver / NVML 库在 node 192.168.48.9 上未正确加载, 节点运维需检查 driver 安装和 /dev/nvidia* 设备
置信度: 高
关键证据: panic: could not load NVML library; restart_count=2568 (重启 N 次仍崩, 排除容器进程层)

错误示例 (会被判定草稿, 强制重做):
- "首先, 根据告警信息, Pod 在 ... 出现了 CrashLoopBackOff..." (这是分析过程, 不是 final)
- "第一步应该调用 kubectl_describe 来查看..." (这是描述, 不是真调用 — 应该走 function_call)
- "### 假设\n1. 容器进程层..." (这是思考, 不是结论)
- "```json\n{\"name\": \"kubectl_describe\", ...}\n```" (这只是文本, 不会被当工具调用)

**重要**: 第一轮 (step 0) 还没有任何工具证据时, 严禁直接给 final —
必须先调至少一个工具 (通过 function_call 接口) 看到证据后再判断.

================================================================
六. 自主执行只读 shell 命令 (v2.13)
================================================================
你新增了 2 个进入 K8s 内部跑只读命令的工具, 让你能像资深 SRE 那样**实地查证**而不是猜:
- **ssh_node_readonly(node, cmd)**: 登节点排查 Host 层 (driver/kernel/设备/systemd)
- **kubectl_exec_readonly(name, namespace, cmd)**: 进 Pod 排查容器内部 (配置/env/进程)

**何时应该用** (强烈推荐):
- 怀疑 Host 层 (NVML/driver/挂载/内核) → **必须** ssh + lsmod / dmesg / ls /dev/*
- 看到 panic: could not load NVML library → ssh + lsmod | grep nvidia (空则锁死 Host 层)
- 怀疑设备文件丢失 → ssh + ls /dev/nvidia*
- 怀疑 kubelet/containerd → ssh + journalctl --no-pager -u kubelet -n 100
- 怀疑内核版本不匹配 → ssh + uname -r 后看 dmesg 里的 NVRM 报错
- 怀疑容器内配置错 → kubectl_exec_readonly + cat /home/work/.../config.yaml
- 看到 OCI mount failed → ssh + ls 看挂载源是否存在

**关键: 不要止步于"建议运维去查"**
- 旧行为: 看到 NVML error 就 final "Host 层驱动问题, 节点运维需检查" → 是建议, 不是实锤
- 新行为: 看到 NVML error → 立刻 ssh 上去看 lsmod / dmesg / uname -r, 拿到原文证据后再 final
  → 结论变成 "节点 X 的 nvidia.ko 因内核升级到 5.10 未重装 (dmesg 原文: NVRM: nvidia.ko built with 5.4 but running 5.10)"

**安全闸 (代码强制, 不要硬刚)**:
- 只允许只读命令: ls/cat/df/free/dmesg/journalctl --no-pager/nvidia-smi (无 -r)/
  systemctl status (无 start/stop/restart)/lsmod/lspci/ip/netstat/ss/ps/uname/find/grep
- 禁用: 任何写操作 (rm/mv/cp/sed -i/重定向 >/>>/2>)/服务变更 (systemctl restart)/
  包管理 (apt/yum)/进程 kill/kubectl 写操作 (apply/delete/exec)/curl POST/docker
- 被 [Blocked] 时换更窄的只读查询, 不要尝试用 \$() / 反引号 / 拼接绕过 (都会被拦)
- 工具返回 [SSH 失败] 时该节点 ssh 没打通, 不要重试相同 node, 用其他工具

**何时不用**:
- ImagePullBackOff: 镜像名错, describe 一次就够, 不需要 ssh 节点
- 启动参数错 (flag provided but not defined): 日志一行清楚, ssh 没必要
- StatefulSet/RS Pod OOMKilled: 容器内事故, 看日志即可, ssh 节点反而绕远

================================================================
七. 申请人审命令 (v2.14)
================================================================
当只读白名单不够用时, 你可以用 ssh_node_with_approval / kubectl_exec_with_approval
**提交人审请求**. 运维在 IM 群里 approve 后, daemon 真跑命令, 结果进 fault_memory,
下次同指纹故障可秒级复用.

何时申请:
- 只读白名单挡了关键诊断 (例: crictl pull 验证镜像可达, ImagePullBackOff 诊断)
- 需要轻量状态变更才能诊断 (例: systemctl restart kubelet 后再观察)
- 必须有明确假设 + 验证理由, 不要为了"试一下"申请
- reason 必须一句话写清"为什么要跑 + 期望验证什么" (>=10 字)

调用后**立即**返回 "[已派单审批 task_id=xxx]", **本轮拿不到这条证据**.
你应该:
1. 基于现有证据先 final 一个临时结论 (置信度可以是"中", 注明缺什么证据)
2. 审批通过后, daemon 自动跑 + 结果进 Memory, 下次同故障自动复用

硬黑名单 (永远不入审批通道, 别试):
- rm / dd / mkfs / fdisk / shutdown / reboot / iptables -F
- kubectl delete --all / drop database / :(){:|:&};: (fork bomb)
- 数据销毁 / 系统断电 / 批量删 这种"出了事没法回滚"的操作
- 硬黑名单命中会直接返回 [硬黑名单拒], 不要绕

期望行为对比:
✗ 旧: 看到 ImagePullBackOff → 直接 final "镜像不存在" 不申请验证
✓ 新: 看到 ImagePullBackOff → 用现有 events 先 final (置信度 "中"),
      同时申请 ssh_node_with_approval(节点, "crictl pull <image>",
      "验证镜像仓库可达 + 拉取是否成功"), 结果异步进 Memory

✗ 旧: 看到 kubelet 卡住 → final "节点问题, 请人工排查"
✓ 新: 看到 kubelet 卡住 → 现有证据先 final, 同时申请
      ssh_node_with_approval(节点, "systemctl restart kubelet",
      "kubelet 已卡 10min, 重启是标准恢复步骤, 之后我会验证 Pod 状态")
"""

# Function Calling 模式: 不需要描述工具列表, schema 已经绑定到 LLM
# 新 prompt 第五节已经写了 final 格式, 这里不再重复 (减少冲突)
_SYSTEM_FC = _SYSTEM_TPL

# ReAct 模式: 需要描述工具列表 + 严格 JSON 输出
_SYSTEM_REACT = _SYSTEM_TPL + """
可用工具:
{tools}

每轮严格输出 JSON 二选一:
1. {{"action":"use_tool","tool":"x","args":{{}},"thought":"..."}}
2. {{"action":"final","hypothesis":"...","confidence":"高/中/低","key_evidence":["..."]}}

规则: 只输出 JSON; 拿到关键证据后立即 final; 不要循环.
"""


def _log(msg):
    print(msg, flush=True)


def _extract_json(text):
    """ReAct 兜底用的 JSON 提取 (Function Calling 模式下用不到)"""
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def _format_tools():
    lines = []
    for n, d in TOOL_DESCRIPTIONS.items():
        lines.append(f"- {n}: {d}")
    return "\n".join(lines)


def _build_fallback_hypothesis(evidence, summary):
    """代码兜底: LLM 没在步数内 final 时用已收集证据拼保守结论"""
    if not evidence:
        sm = summary[:80]
        return f"诊断未完成: 无有效证据 (基于摘要 {sm} 需人工介入)"
    findings = []
    for ev in evidence[:3]:
        tool = ev.get("tool", "?")
        result = str(ev.get("result", ""))[:200]
        findings.append(f"[{tool}] {result}")
    joined = " | ".join(findings)
    n = len(evidence)
    return f"诊断未自主完成 (LLM 未在限定步数内 final), 已收集 {n} 条证据: {joined}"


def _call_tool_with_trace(tool_name: str, args: dict):
    """统一调工具入口 + Langfuse span + 错误兜底."""
    fn = TOOLS.get(tool_name)
    if not fn:
        return f"工具 {tool_name} 不存在"
    with TraceTimer(
        agent="investigator",
        name=f"tool:{tool_name}",
        input_data={"args": args},
    ) as t:
        try:
            result = fn(**args)
        except Exception as e:
            result = f"调用失败: {type(e).__name__}: {e}"
        t.set_output({"result_preview": str(result)[:300]})
    return result


def _log_tool_result(step: int, tool_name: str, args: dict, result):
    """v2.5 屏幕显示扩展, 让运维能看到完整证据 (LLM 拿到的更长)."""
    full_str = str(result)
    full_len = len(full_str)
    preview = full_str[:1500]
    if full_len > 1500:
        _log(f"[Investigator]  step {step}: 结果 (前 1500 字, 完整 {full_len} 字):")
        _log(preview)
        _log(f"  ... (截断剩余 {full_len - 1500} 字)")
    else:
        _log(f"[Investigator]  step {step}: 结果 = {preview}")


def _looks_like_draft(content: str) -> bool:
    """v2.11: 判断 LLM 的"空 tool_calls 回答"是不是草稿/思考过程.

    DeepSeek-V3 偶尔会输出"我打算调 xxx 工具""第一步应该..." 这种半成品,
    甚至带 ```json {...} ``` 块描述自己想调的工具, 但 tool_calls 字段是空的.
    这种情况下不能当真 final, 应该让它重做.

    判定为草稿的特征:
    - 出现 "假设" / "第一步" / "我打算" / "应该调用" / "我们需要"
    - 出现 ```json``` 块 (LLM 想 "用 JSON 调工具" 但走错了通道)
    - 不包含 "根因:" 或 "根因：" (合法 final 必须有这行)
    - 全文 < 60 字 (内容太短不像 final)
    """
    if not content:
        return True
    text = content.strip()
    # 合法 final 必须有"根因"标签 (中文冒号 / 英文冒号)
    has_rca_label = bool(re.search(r"根因\s*[:：]", text))
    if has_rca_label:
        return False
    # 没有"根因:" 标签 + 出现草稿关键词 → 是草稿
    draft_signals = (
        "假设", "第一步", "应该调用", "我们需要", "我打算",
        "我将", "我会", "下一步", "```json", "```\n",
    )
    if any(sig in text for sig in draft_signals):
        return True
    # 短内容也算草稿 (合法 final 至少 60 字以上)
    if len(text) < 60:
        return True
    return False


def _parse_fc_final(content: str) -> dict:
    """从 Function Calling 模式下 LLM 的自然语言收尾里抽 RCA / 置信度 / 关键证据.

    LLM 按 _SYSTEM_FC 的提示会输出类似:
       根因: kube-external-auditor Pod 因为 -kubeConfig 标志未定义崩溃.
       置信度: 高
       关键证据: flag provided but not defined: -kubeConfig

    解析失败也不该 crash, 直接整段当 hypothesis.
    """
    if not content:
        return {"hypothesis": "(空回答)", "confidence": "?", "key_evidence": []}
    text = content.strip()

    def _grab(label_re):
        m = re.search(label_re, text)
        return m.group(1).strip() if m else ""

    hypothesis = _grab(r"根因[:：]\s*(.+?)(?=\n[置关]|\n$|$)")
    confidence = _grab(r"置信度[:：]\s*(高|中|低)")
    # v2.10: 关键证据可能跨多行 (LLM 自由发挥), 改用多行匹配, 直到下一个标签或结尾
    key_evidence_raw = _grab(r"关键证据[:：]\s*([\s\S]+?)(?=\n(?:根因|置信度)[:：]|$)")
    # 没匹配到标准格式 → 整段当 hypothesis
    if not hypothesis:
        hypothesis = text[:400]
    key_evidence = [s.strip() for s in re.split(r"[;；\n]", key_evidence_raw)
                    if s.strip() and not s.strip().startswith(("无", "(无)"))]
    return {
        "hypothesis": hypothesis,
        "confidence": confidence or "中",
        "key_evidence": key_evidence,
    }


def _run_function_calling(history: list, max_steps: int, evidence: list):
    """Function Calling 模式: LLM 直接返回 tool_calls, 解析零失败.

    history 是 [(role, content), ...] 的 langchain message tuple 列表.
    返回 final_result dict 或 None (未 final).

    v2.10 防循环 / 防资源浪费:
    - 连续 2 次调用同一个 (tool, sorted_args) → 注入"已查过, 必须换工具或收尾"提示
    - 同一 (tool, sorted_args) 累计调用 ≥3 次 → 强制中断, 进代码兜底
    - 工具返回 "调用失败: TypeError" → 注入参数错误提示, 让 LLM 用合法参数重试
    """
    from langchain_core.messages import (
        SystemMessage, HumanMessage, AIMessage, ToolMessage,
    )

    # 转 langchain messages
    msgs = []
    for role, content in history:
        if role == "system":
            msgs.append(SystemMessage(content=content))
        elif role == "user":
            msgs.append(HumanMessage(content=content))
        else:
            msgs.append(AIMessage(content=content))

    call_counter: dict = {}     # (tool, args_key) -> count
    last_call_key = None        # 上一步的 (tool, args_key)
    empty_call_retry = 0        # v2.11: 空 tool_calls 但内容明显是"草稿"的重试次数

    for step in range(max_steps):
        try:
            resp = _llm_with_tools.invoke(msgs)
        except Exception as e:
            _log(f"[Investigator] step {step}: LLM 调用失败 {e}")
            return None

        tool_calls = getattr(resp, "tool_calls", None) or []

        # 没有 tool_calls → LLM 自己收尾 (final)
        if not tool_calls:
            content = resp.content or ""
            # v2.11: 区分"真 final"和"DeepSeek 把思考过程当 final 输出"
            # 真 final 必有 "根因:" / "置信度:" 字样, 且不该出现"假设"/"第一步"/"```json"这种草稿特征
            is_draft = _looks_like_draft(content)
            has_no_evidence = step == 0 and not evidence  # 还没调过任何工具就要 final
            if is_draft or has_no_evidence:
                empty_call_retry += 1
                _log(f"[Investigator] step {step}: ⚠ LLM 空 tool_calls 但内容是草稿 "
                     f"(retry={empty_call_retry}), 强制重新输出")
                if empty_call_retry >= 2:
                    _log(f"[Investigator] step {step}: 草稿重试 2 次仍失败, 走代码兜底")
                    return None
                # 把模型刚才那段草稿当 assistant 留在历史里, 再追加纠错 user message
                msgs.append(AIMessage(content=content))
                msgs.append(HumanMessage(content=(
                    "[严重错误] 你没有调用任何工具, 也没有给出符合格式的最终结论. "
                    "你必须二选一:\n"
                    "(A) 调用工具 — 通过 function calling 接口发起 tool_calls "
                    "(kubectl_describe / get_pod_logs / prometheus_query / "
                    "query_history_alerts), 不要用文字描述你打算调什么\n"
                    "(B) 给出最终结论 — 严格按下面三行格式输出, 不要假设, 不要列举, 不要 JSON:\n"
                    "根因: <一句话: 哪一层 + 具体什么坏了 + 谁该修>\n"
                    "置信度: 高/中/低\n"
                    "关键证据: <工具输出原文 1-3 条>\n\n"
                    "现在请重新输出, 选 (A) 或 (B)."
                )))
                continue
            _log(f"[Investigator] step {step}: LLM 收尾 (Function Calling final)")
            return _parse_fc_final(content)

        # 多个 tool_calls 一并执行 (32B 偶尔同时调 2 个)
        msgs.append(resp)  # AIMessage with tool_calls
        nudge_needed = False  # 本步是否需要追加"换工具或收尾"提示
        for tc in tool_calls:
            tool_name = tc["name"]
            args = tc.get("args") or {}
            tc_id = tc.get("id") or f"call_{step}"

            # 计数键: (工具名, 排序后的 args 元组)
            try:
                args_key = tuple(sorted(args.items()))
            except Exception:
                args_key = (str(args),)
            call_key = (tool_name, args_key)
            call_counter[call_key] = call_counter.get(call_key, 0) + 1
            count = call_counter[call_key]

            _log(f"[Investigator]  step {step}: 调用 {tool_name}({args}) "
                 f"[tc_id={tc_id}]" + (f" ⚠ 第{count}次相同调用" if count > 1 else ""))

            # 同一调用第 3 次出现 → 直接终止整个 FC loop
            if count >= 3:
                _log(f"[Investigator] ⚠ 同一调用 ({tool_name}) 已第 {count} 次, "
                     f"判定 LLM 卡死, 中断进代码兜底")
                # 给最后一次 tool_call 一个空 ToolMessage 占位 (langchain 要求闭合)
                msgs.append(ToolMessage(
                    content="[终止] 检测到重复调用, 已强制退出",
                    tool_call_id=tc_id,
                ))
                return None

            result = _call_tool_with_trace(tool_name, args)
            _log_tool_result(step, tool_name, args, result)
            full_str = str(result)
            evidence.append({"tool": tool_name, "args": args,
                              "result": full_str[:2000]})

            # 参数错 (TypeError) → 在工具返回里追加提示, LLM 下一步会自动修正
            tool_content = full_str[:3000]
            if "TypeError" in full_str and "unexpected keyword argument" in full_str:
                tool_content += (
                    "\n\n[提示] 上面是参数错误. 请只使用 schema 声明的参数 "
                    "(name / namespace / lines / previous). 不要传 schema 没有的字段."
                )

            # 同一调用第 2 次 → 标记本步结束后补一条 user 提示
            if count >= 2:
                nudge_needed = True

            msgs.append(ToolMessage(
                content=tool_content,
                tool_call_id=tc_id,
            ))
            last_call_key = call_key

        if nudge_needed:
            msgs.append(HumanMessage(content=(
                "[提示] 你刚才重复调用了同一个工具+参数. 不要再调用相同的查询, "
                "要么换工具 (kubectl_describe / get_pod_logs / prometheus_query / "
                "query_history_alerts), 要么换参数 (不同 Pod / 不同 namespace), "
                "要么基于已收集的证据直接给出 final (按系统提示格式输出 "
                "根因/置信度/关键证据)."
            )))

    _log(f"[Investigator] FC 模式: {max_steps} 步内 LLM 未收尾, 走代码兜底")
    return None


def _run_react(history: list, max_steps: int, evidence: list):
    """旧版 ReAct 字符串解析模式 (兜底, 通过 USE_FUNCTION_CALLING=false 启用)."""
    for step in range(max_steps):
        try:
            resp = _llm.invoke(history)
        except Exception as e:
            _log(f"[Investigator] step {step}: LLM 调用失败 {e}")
            return None
        text = resp.content
        decision = _extract_json(text)
        if not decision:
            _log(f"[Investigator] step {step}: JSON 解析失败")
            return None
        action = decision.get("action")

        if action == "use_tool":
            tool_name = decision.get("tool")
            args = decision.get("args", {})
            thought = decision.get("thought", "")[:60]
            _log(f"[Investigator]  step {step}: 调用 {tool_name}({args}) - {thought}")
            result = _call_tool_with_trace(tool_name, args)
            _log_tool_result(step, tool_name, args, result)
            full_str = str(result)
            evidence.append({"tool": tool_name, "args": args,
                              "result": full_str[:2000]})
            history.append(("assistant", text))
            history.append(("user", f"工具 {tool_name} 返回: {full_str[:3000]}\n\n请输出下一步."))

        elif action == "final":
            return decision
        else:
            _log(f"[Investigator] step {step}: 未知 action {action}")
            return None
    return None


def investigator_node(state: AlertState) -> AlertState:
    summary = state.get("event_summary", "")
    alerts = state.get("raw_alerts", [])
    retry_count = state.get("retry_count", 0)
    # v2.4 分级诊断: medium 走 light (3 步快诊), critical/high 走 full (默认 8 步)
    mode = state.get("investigation_mode", "full")
    max_steps = 3 if mode == "light" else _MAX_STEPS

    # v2.3 故障 Memory: 同指纹高置信度 → 跳过 LLM 推理 (重诊时不查, 否则会循环命中)
    if retry_count == 0:
        from tools.fault_memory import generate_fingerprint, lookup, record_hit
        first_alert = alerts[0] if alerts else {}
        labels = first_alert.get("labels") or {}
        ns = labels.get("namespace", "") or first_alert.get("namespace", "")
        alertname = labels.get("alertname", "") or first_alert.get("alertname", "")
        # 用 summary 当 RCA 签名 (此刻还没诊断, 没 RCA), 这个签名要稳定
        rca_signature = summary
        fp = generate_fingerprint(ns, alertname, rca_signature)
        cached = lookup(fp)
        if cached:
            record_hit(fp)
            state["from_memory"] = True
            state["fingerprint"] = fp
            state["rca_hypothesis"] = cached["rca_text"]
            state["evidence"] = []
            state["remediation_plan"] = cached["plan"]
            state["retry_count"] = 1  # 标记一下, 防止重诊路径再次命中
            _log(f"[Investigator] ⚡ Memory 命中 fp={fp} hits={cached['hits']} "
                 f"age={cached['age_sec']}s, 跳过 LLM 推理")
            _log(f"[Investigator] 复用 RCA: {cached['rca_text'][:120]}")
            return state
        # 没命中, 记录 fp 备后续 record_success 用
        state["fingerprint"] = fp
        state["from_memory"] = False

    # v2.14: 拉同指纹的历史诊断命令 (曾人审执行过的命令 + 结果)
    diag_history = []
    fp_now = state.get("fingerprint", "")
    if fp_now and retry_count == 0:
        try:
            from tools.fault_memory import list_diagnostic_history
            diag_history = list_diagnostic_history(fp_now, limit=5)
            if diag_history:
                _log(f"[Investigator] 📚 同指纹历史命令 {len(diag_history)} 条 (审批执行过, 供参考)")
        except Exception as e:
            _log(f"[Investigator] 历史命令查询失败 (不影响): {e}")

    if retry_count == 0:
        if mode == "light":
            _log("[Investigator] 开始轻量诊断 (3 步快诊, medium 级异常)")
        else:
            mode_label = "Function Calling" if _USE_FC else "ReAct"
            _log(f"[Investigator] 开始诊断 ({mode_label} + 代码兜底)")
    else:
        _log(f"[Investigator] ⟳ 第 {retry_count} 次重诊 "
             f"(上次 plan 未能修复, 需换个角度)")

    # 准备 system + user
    if _USE_FC:
        sys_prompt = _SYSTEM_FC
    else:
        sys_prompt = _SYSTEM_REACT.format(tools=_format_tools())

    # v2.3 闭环重诊: 把上次 plan 失败信息塞进 user_msg
    extra_ctx = ""
    if retry_count > 0:
        last_plan = state.get("last_failed_plan") or {}
        last_reason = state.get("last_failure_reason") or "未提供失败原因"
        extra_ctx = (
            f"\n\n⚠ 这是第 {retry_count} 次重诊. 上一次的修复尝试失败了:\n"
            f"  - 上次 action: {last_plan.get('action')}\n"
            f"  - 上次 target: {last_plan.get('target')}\n"
            f"  - 失败原因: {last_reason}\n"
            f"  请重新分析根因, 给出不同的诊断思路 (例如: 不是 runtime 问题"
            f"而是依赖/配置/资源限制), 避免推荐同样会失败的方案."
        )

    # v2.14: 若有同指纹历史命令, 拼进 extra_ctx 给 LLM 参考
    if diag_history:
        lines = ["\n\n📚 此故障曾审批执行过以下诊断命令 (最近 5 条, 供参考):"]
        for h in diag_history:
            cmd_short = h["cmd"][:100]
            approved_by = h.get("approved_by") or "?"
            ec = h.get("exit_code", "?")
            reason = (h.get("reason") or "")[:60]
            stdout_head = (h.get("stdout_head") or "").strip()[:200]
            stderr_head = (h.get("stderr_head") or "").strip()[:200]
            lines.append(f"  - cmd: {cmd_short}")
            lines.append(f"    reason: {reason}  approved_by: {approved_by}  exit={ec}")
            if stdout_head:
                lines.append(f"    stdout: {stdout_head}")
            if stderr_head:
                lines.append(f"    stderr: {stderr_head}")
        lines.append(
            "\n提示: 若历史命令已经证实过某个根因, 可以直接 final. "
            "若需要新证据, 可以再申请新的 with_approval 命令."
        )
        extra_ctx += "\n".join(lines)

    # v2.14: 把当前 fingerprint 塞进 user_msg, LLM 可以在 with_approval 调用时传回
    fp_hint = ""
    if fp_now:
        fp_hint = (
            f"\n\n当前故障指纹: fingerprint={fp_now} "
            f"(调 ssh_node_with_approval/kubectl_exec_with_approval 时,"
            f"传这个 fingerprint 让结果进 Memory 供下次复用)"
        )

    user_msg = (f"事件摘要: {summary}\n\n告警明细: {alerts}{extra_ctx}{fp_hint}\n\n"
                f"请按上面的方法论调查:\n"
                f"1) 先假设根因落在哪一层 (容器进程/Pod 配置/Host/集群)\n"
                f"2) 选最能验证或推翻假设的工具\n"
                f"3) **拿到精确错误原文后立即 final**, 不要凑步数 — "
                f"ImagePull/mount 错/flag 未定义这类问题, 1-2 次工具调用就够了\n"
                f"4) 只有 NVML/driver/集群层这种需要横向验证时才多查几步\n"
                f"5) **不要瞎猜 Pod 名**, 不确定就用 query_history_alerts 列已有 Pod\n\n"
                f"第一步该调哪个工具?")

    history = [
        ("system", sys_prompt),
        ("user", user_msg),
    ]

    evidence = []
    if _USE_FC:
        final_result = _run_function_calling(history, max_steps, evidence)
    else:
        final_result = _run_react(history, max_steps, evidence)

    if final_result:
        hypothesis = final_result.get("hypothesis", "(无)")
        confidence = final_result.get("confidence", "?")
        key_evidence = final_result.get("key_evidence", [])
        joined = "; ".join(key_evidence) if key_evidence else "无"
        rca_text = f"{hypothesis} (置信度: {confidence}; 关键证据: {joined})"
    else:
        rca_text = _build_fallback_hypothesis(evidence, summary)

    state["rca_hypothesis"] = rca_text
    state["evidence"] = evidence
    # v2.3: 出 Investigator 时 retry_count++, 这样下次 Validator 看到的是新值
    state["retry_count"] = retry_count + 1
    n = len(evidence)
    short_rca = rca_text[:200]
    _log(f"[Investigator] 共调用 {n} 次工具")
    _log(f"[Investigator] 结论: {short_rca}")
    return state
