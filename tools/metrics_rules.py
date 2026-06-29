"""MetricsInspector 内置 PromQL 规则集.

为什么内置而不是 YAML:
- 6 条 K8s 通用规则覆盖 80% 场景, YAML 配置门槛高 + 容易写错
- 后续真有人要定制再加 YAML 覆盖层

规则字段:
- id: 唯一 ID (英文蛇形), 也是 issue.type 的来源 (PascalCase)
- query: PromQL 表达式
- threshold: 数值阈值, 超过即告警
- comparator: ">" 或 "<" (默认 ">")
- severity: critical / high / medium / low
- description: 一句话说明, 用于告警 summary
- label_for_pod: 从指标 labels 取哪个字段当 pod 名 (没有就是集群级)
- label_for_ns: 从指标 labels 取哪个字段当 namespace
- label_for_node: 节点级规则填这个
"""

# 规则 ID → 配置
_RULES = [
    {
        "id": "pod_cpu_throttling",
        "type": "PodCpuThrottling",
        "query": 'sum by (pod, namespace) (rate(container_cpu_cfs_throttled_seconds_total{container!="",container!="POD"}[5m])) > 0.5',
        "threshold": 0.5,
        "comparator": ">",
        "severity": "high",
        "description": "Pod CPU 被节流, 表明资源 limit 不够 / 节点负载高",
        "label_for_pod": "pod",
        "label_for_ns": "namespace",
        "unit": "throttle ratio",
    },
    {
        "id": "pod_memory_near_limit",
        "type": "PodMemoryNearLimit",
        "query": 'max by (pod, namespace) (container_memory_working_set_bytes{container!="",container!="POD"} / on(pod, namespace, container) group_left container_spec_memory_limit_bytes{container!="",container!="POD"}) > 0.9',
        "threshold": 0.9,
        "comparator": ">",
        "severity": "high",
        "description": "Pod 内存使用 >90% limit, 即将触发 OOMKilled",
        "label_for_pod": "pod",
        "label_for_ns": "namespace",
        "unit": "ratio",
    },
    {
        "id": "node_disk_pressure",
        "type": "NodeDiskPressure",
        "query": '(node_filesystem_size_bytes{mountpoint="/"} - node_filesystem_avail_bytes{mountpoint="/"}) / node_filesystem_size_bytes{mountpoint="/"} > 0.85',
        "threshold": 0.85,
        "comparator": ">",
        "severity": "high",
        "description": "节点根分区使用率 >85%, 会触发 Pod Evicted",
        "label_for_node": "instance",
        "unit": "ratio",
    },
    {
        "id": "node_load_high",
        "type": "NodeLoadHigh",
        "query": 'node_load5 / count by (instance) (node_cpu_seconds_total{mode="idle"}) > 2',
        "threshold": 2,
        "comparator": ">",
        "severity": "medium",
        "description": "节点 5min load 超过核数 2 倍, 业务请求会被节流",
        "label_for_node": "instance",
        "unit": "load/core",
    },
    {
        "id": "apiserver_5xx_high",
        "type": "ApiServer5xxHigh",
        "query": 'sum(rate(apiserver_request_total{code=~"5.."}[5m])) > 1',
        "threshold": 1,
        "comparator": ">",
        "severity": "critical",
        "description": "API Server 5xx 错误率超过 1 req/s, 集群控制面异常",
        "unit": "req/s",
    },
    {
        "id": "kubelet_down",
        "type": "KubeletDown",
        "query": 'up{job="kubelet"} == 0',
        "threshold": 0,
        "comparator": "==",
        "severity": "critical",
        "description": "kubelet 失联, 该节点上 Pod 可能不再受控",
        "label_for_node": "instance",
        "unit": "up",
    },
]


def get_rules() -> list:
    """返回所有内置规则的副本 (避免外部改原始定义)."""
    return [dict(r) for r in _RULES]
