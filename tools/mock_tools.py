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


from tools.change_tracker import get_recent_changes as _real_get_recent_changes


def get_recent_changes(namespace: str, hours: int = 2) -> str:
    """变更追踪: 查 namespace 最近 N 小时的 Deployment/RS/ConfigMap/Secret/Event 变更.

    用于 Investigator 判断故障是否由近期变更引起 (业界 80% 故障由变更触发).
    """
    return _real_get_recent_changes(namespace=namespace, hours=hours)


# === v2.13: 只读 shell 执行工具 (自主诊断 Host / 容器内部) ===
from tools.ssh_tools import (
    ssh_run as _real_ssh_run,
    kubectl_exec_readonly as _real_kubectl_exec_readonly,
    ssh_node_with_approval as _real_ssh_node_with_approval,
    kubectl_exec_with_approval as _real_kubectl_exec_with_approval,
)


def ssh_node_readonly(node: str, cmd: str) -> str:
    """登节点跑只读命令排查 Host 层故障.

    适用场景: 怀疑 driver/kernel/设备文件/系统服务 类问题, 想直接看节点本地状态.
    白名单: ls/cat/df/free/dmesg/journalctl --no-pager/nvidia-smi/lspci/lsmod/
            systemctl status/ip/netstat 等. 任何写操作/服务重启/包管理全部拒绝.
    """
    return _real_ssh_run(node=node, cmd=cmd)


def kubectl_exec_readonly(name: str, namespace: str, cmd: str) -> str:
    """在 Pod 里跑只读命令查容器内部状态.

    适用场景: 看容器内的 /etc/config 实际内容 / env / 进程 / 网络.
    白名单跟 ssh_node_readonly 一致.
    """
    return _real_kubectl_exec_readonly(name=name, namespace=namespace, cmd=cmd)


# === v2.14: 需人审的命令 (突破白名单, 走 IM 审批异步执行) ===

def ssh_node_with_approval(node: str, cmd: str, reason: str,
                            trace_id: str = "",
                            fingerprint: str = "") -> str:
    """提交需人审的节点 shell 命令.

    用途: 只读白名单挡了关键诊断 (crictl pull 验证镜像可达), 或需要轻量状态变更
    (systemctl restart kubelet) 才能诊断时使用.

    reason 必须一句话写清'为什么要跑 + 期望验证什么' (>=10 字).
    调用后立即返回 [已派单审批 task_id=xxx], LLM 不阻塞, 结果异步进 fault_memory.
    硬黑名单 (rm/dd/mkfs/shutdown/kubectl delete --all) 永远不入审批通道.
    """
    return _real_ssh_node_with_approval(
        node=node, cmd=cmd, reason=reason,
        trace_id=trace_id, fingerprint=fingerprint,
    )


def kubectl_exec_with_approval(name: str, namespace: str, cmd: str,
                                reason: str,
                                trace_id: str = "",
                                fingerprint: str = "") -> str:
    """同上, 但在 Pod 内执行. 适用需要 kubectl exec 进容器跑诊断."""
    return _real_kubectl_exec_with_approval(
        name=name, namespace=namespace, cmd=cmd, reason=reason,
        trace_id=trace_id, fingerprint=fingerprint,
    )


TOOLS = {
    "prometheus_query": prometheus_query,
    "kubectl_describe": kubectl_describe,
    "query_history_alerts": query_history_alerts,
    "get_recent_changes": get_recent_changes,
    "ssh_node_readonly": ssh_node_readonly,
    "kubectl_exec_readonly": kubectl_exec_readonly,
    "ssh_node_with_approval": ssh_node_with_approval,
    "kubectl_exec_with_approval": kubectl_exec_with_approval,
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
    "get_recent_changes": (
        "查 namespace 最近 N 小时内的 K8s 资源变更 (Deployment/ReplicaSet/"
        "StatefulSet/ConfigMap/Secret/Event). 用于判断故障是否由近期发布或"
        "配置变更引起. 参数 namespace(必填), hours(默认 2, 最大 24)."
    ),
    "ssh_node_readonly": (
        "登节点跑只读命令排查 Host 层故障 (driver/kernel/设备文件/systemd). "
        "参数 node (K8s 节点名/IP, 必须真实存在), cmd (只读 shell 命令). "
        "强烈推荐场景: 怀疑 Host 层 → ssh+lsmod/dmesg/ls /dev/*/journalctl --no-pager/nvidia-smi. "
        "白名单: ls/cat/df/free/dmesg/journalctl/nvidia-smi/lsmod/lspci/systemctl status/"
        "ip/netstat/ss/ps. 写操作/服务重启/包管理全部拒绝."
    ),
    "kubectl_exec_readonly": (
        "在 Pod 里跑只读命令查容器内部 (/etc/config 实际内容/env/进程/网络). "
        "参数 name (pod 名), namespace, cmd (只读 shell 命令). "
        "白名单跟 ssh_node_readonly 一致."
    ),
    "ssh_node_with_approval": (
        "提交需人审的节点 shell 命令. 只读白名单挡了关键诊断时使用. "
        "参数 node, cmd, reason (给运维看的一句话理由, >=10 字). "
        "运维 IM approve 后 daemon 异步跑, 结果进 fault_memory 供下次复用. "
        "本轮拿不到证据, LLM 应基于现有证据先 final."
    ),
    "kubectl_exec_with_approval": (
        "同 ssh_node_with_approval, 但在 Pod 内执行. "
        "参数 name, namespace, cmd, reason."
    ),
}


from tools.k8s_tools import get_pod_logs as _real_get_pod_logs








def get_pod_logs(name: str, namespace: str, lines: int = 30,
                 previous: bool = True) -> str:


    """获取 Pod 日志 (含上次崩溃前的日志).

    previous 参数兼容 LLM 显式传入 (v2.10):
    - 默认 True 时同时拉当前 + 上次崩溃日志 (k8s_tools 原行为)
    - 显式 False 时仅拉当前日志
    LLM 受 prompt 引导有时会主动传 previous=true, 之前的版本没声明这参数会 TypeError.
    """


    return _real_get_pod_logs(name=name, namespace=namespace, lines=lines, previous=previous)








TOOLS["get_pod_logs"] = get_pod_logs


TOOL_DESCRIPTIONS["get_pod_logs"] = "拉取 Pod 容器日志(含上次崩溃前的日志). 参数 name(str), namespace(str), lines(int, 默认 30). 排查 CrashLoopBackOff 的关键工具."
