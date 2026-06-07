"""Investigator 真实工具集 (接生产 K8s + VictoriaMetrics)"""
import httpx
from tools.k8s_tools import describe_pod_real, _v1, _kube_ok

VMSELECT_URL = "http://10.16.120.255:8481/select/1/prometheus"
TIMEOUT = 8


def prometheus_query(query: str) -> str:
    """查询 VictoriaMetrics PromQL"""
    try:
        resp = httpx.get(
            f"{VMSELECT_URL}/api/v1/query",
            params={"query": query},
            timeout=TIMEOUT,
        )
        data = resp.json()
    except Exception as e:
        etype = type(e).__name__
        return f"[查询失败] {etype}: {e}"
    if data.get("status") != "success":
        err = data.get("error", "unknown")
        return f"[查询失败] {err}"
    result = data.get("data", {}).get("result", [])
    if not result:
        return f"[空结果] {query}"
    n = len(result)
    lines = [f"[查询成功] {n} 条时序, 取前 5 条:"]
    keep_keys = ("__name__", "instance", "pod", "namespace", "node_name", "container", "Hostname", "gpu")
    for series in result[:5]:
        labels = series.get("metric", {})
        value_pair = series.get("value", ["?", "?"])
        value = value_pair[1] if len(value_pair) > 1 else "?"
        key_labels = {k: v for k, v in labels.items() if k in keep_keys}
        lines.append(f"  - {key_labels} = {value}")
    return "\n".join(lines)


def kubectl_describe(resource: str, name: str, namespace: str = "default") -> str:
    """真实 kubectl describe (复用 k8s_tools)"""
    if resource != "pod":
        return f"[未支持] resource={resource}, 只支持 pod"
    return describe_pod_real(name, namespace)


def query_history_alerts(alertname: str, days: int = 7) -> str:
    """简化版历史告警: 用 K8s 事件统计某 Pod 的重启次数"""
    if not _kube_ok:
        return "[错误] K8s API 不可用"
    try:
        pods = _v1.list_pod_for_all_namespaces(timeout_seconds=10).items
    except Exception as e:
        return f"[查询失败] {e}"
    same_type = []
    for p in pods:
        if not p.status.container_statuses:
            continue
        for cs in p.status.container_statuses:
            reason = ""
            if cs.state and cs.state.waiting:
                reason = cs.state.waiting.reason or ""
            if alertname.lower() in reason.lower() or alertname.lower() in (cs.name or "").lower():
                same_type.append({
                    "ns": p.metadata.namespace,
                    "pod": p.metadata.name,
                    "restarts": cs.restart_count or 0,
                    "reason": reason,
                })
    if not same_type:
        return f"[历史] 未找到 {alertname} 相关的 Pod"
    same_type.sort(key=lambda x: x["restarts"], reverse=True)
    n = len(same_type)
    lines = [f"[历史告警] 集群当前 {n} 个 Pod 出现 {alertname} 相关问题:"]
    for s in same_type[:5]:
        ns = s["ns"]
        pod = s["pod"]
        rs = s["restarts"]
        rsn = s["reason"]
        lines.append(f"  - {ns}/{pod} restarts={rs} reason={rsn}")
    return "\n".join(lines)


TOOLS = {
    "prometheus_query": prometheus_query,
    "kubectl_describe": kubectl_describe,
    "query_history_alerts": query_history_alerts,
}

_PROM_HINT = (
    "查 VictoriaMetrics 监控. 参数 query(PromQL). "
    "可用真实指标(都不需要 pod_name 标签, 用 pod 标签): "
    "node_memory_MemAvailable_bytes, kube_pod_info, kube_pod_container_status_restarts_total, "
    "container_memory_usage_bytes, container_cpu_usage_seconds_total, DCGM_FI_DEV_GPU_UTIL"
)

TOOL_DESCRIPTIONS = {
    "prometheus_query": _PROM_HINT,
    "kubectl_describe": "真实查 K8s Pod 详情和事件. 参数 resource(必须是 pod), name, namespace.",
    "query_history_alerts": "查集群当前所有出现某种类型问题的 Pod. 参数 alertname(如 CrashLoopBackOff/OOMKilled), days.",
}


from tools.k8s_tools import get_pod_logs as _real_get_pod_logs








def get_pod_logs(name: str, namespace: str, lines: int = 30) -> str:


    """获取 Pod 日志 (含上次崩溃前的日志)"""


    return _real_get_pod_logs(name=name, namespace=namespace, lines=lines, previous=True)








TOOLS["get_pod_logs"] = get_pod_logs


TOOL_DESCRIPTIONS["get_pod_logs"] = "拉取 Pod 容器日志(含上次崩溃前的日志). 参数 name(str), namespace(str), lines(int, 默认 30). 排查 CrashLoopBackOff 的关键工具."
