"""Function Calling Native 工具 schema 定义 (v2.9).

把 Investigator 的工具集翻译成 OpenAI Function Calling 格式,
让 vLLM/任何 OpenAI 兼容服务通过原生 tool_calls 返回结构化调用 (零解析失败).

vLLM 启动需要加:
  --enable-auto-tool-choice --tool-call-parser hermes

跟 ReAct 字符串解析的区别:
- 旧: LLM 输出 JSON 文本, 代码用正则提取 → 经常因为 markdown wrap / 多余说明 / 空字段失败
- 新: LLM 直接返回结构化 `tool_calls` 数组, langchain 自动转 ToolMessage → 零解析失败

为何这里又写一遍 schema (跟 mock_tools.TOOL_DESCRIPTIONS 重复):
- TOOL_DESCRIPTIONS 是给 LLM 看的自由文本描述 (ReAct 模式)
- TOOLS_SCHEMA 是 OpenAI Function Calling 协议要求的严格 JSON Schema
  (含 parameters / required / type 等强约束)
"""

# 与 tools.mock_tools.TOOLS 字典里的 key 一一对应
TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "get_pod_logs",
            "description": (
                "拉取 K8s Pod 容器日志, 自动包含 init containers + 上次崩溃前的日志. "
                "排查 CrashLoopBackOff / Error / OOMKilled 的首选工具. "
                "日志为空时改用 kubectl_describe."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Pod 完整名 (含 hash 后缀, 例 my-app-7466749c9f-q98kw)",
                    },
                    "namespace": {
                        "type": "string",
                        "description": "Pod 所在 namespace",
                    },
                    "lines": {
                        "type": "integer",
                        "description": "返回最后多少行日志, 默认 30",
                        "default": 30,
                    },
                    "previous": {
                        "type": "boolean",
                        "description": (
                            "是否同时拉上次崩溃前的日志 (默认 true, "
                            "CrashLoopBackOff 排查时几乎都需要)"
                        ),
                        "default": True,
                    },
                },
                "required": ["name", "namespace"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "kubectl_describe",
            "description": (
                "查 K8s Pod 详情和事件 (kubectl describe pod). "
                "ImagePullBackOff / Pending / 调度失败这类'日志为空但有问题'的故障必须用这个. "
                "关键字段: container_statuses[].waiting.message, "
                "conditions[].message, pod.status.message, events[].message."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "resource": {
                        "type": "string",
                        "enum": ["pod"],
                        "description": "资源类型, 当前只支持 pod",
                    },
                    "name": {
                        "type": "string",
                        "description": "Pod 完整名",
                    },
                    "namespace": {
                        "type": "string",
                        "description": "Pod 所在 namespace, 默认 default",
                        "default": "default",
                    },
                },
                "required": ["resource", "name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "prometheus_query",
            "description": (
                "查 VictoriaMetrics PromQL 监控指标 (历史趋势 / 资源用量). "
                "可用真实指标 (不需要 pod_name 标签, 用 pod 标签): "
                "node_memory_MemAvailable_bytes, kube_pod_info, "
                "kube_pod_container_status_restarts_total, container_memory_usage_bytes, "
                "container_cpu_usage_seconds_total, DCGM_FI_DEV_GPU_UTIL"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "PromQL 表达式",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_history_alerts",
            "description": (
                "查集群当前所有出现某种类型问题的 Pod (横向对比, 找相似故障). "
                "用于判断是单 Pod 个例还是集群层面问题."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "alertname": {
                        "type": "string",
                        "description": "异常类型, 如 CrashLoopBackOff / OOMKilled / ImagePullBackOff",
                    },
                    "days": {
                        "type": "integer",
                        "description": "回查天数 (当前实现忽略此参数, 只看当下集群状态)",
                        "default": 7,
                    },
                },
                "required": ["alertname"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recent_changes",
            "description": (
                "查询指定 namespace 最近 N 小时内的 K8s 资源变更 "
                "(Deployment / ReplicaSet / StatefulSet / ConfigMap / Secret / Event). "
                "用于判断故障是否由近期发布或配置变更触发 "
                "(业界统计: ~80% 生产故障由变更引起). "
                "返回按时间倒序的变更列表, 最多 50 条. "
                "适合诊断刚开始飙的 CrashLoop / OOM / 5xx 突涨等故障."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "namespace": {
                        "type": "string",
                        "description": "K8s namespace, 必填",
                    },
                    "hours": {
                        "type": "integer",
                        "description": "回溯小时数, 默认 2, 最大 24",
                        "default": 2,
                    },
                },
                "required": ["namespace"],
            },
        },
    },
]


# Function Calling 模式下不需要 "use_tool" / "final" 这种自定义 action,
# LLM 用工具就调 tool_calls, 不用工具就直接返回 final text + tool_calls=[]
# (无 tool_calls 视为 final)
