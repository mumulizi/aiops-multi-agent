"""K8s 真实工具 + 结构化数据缓存"""
from kubernetes import client, config
from collections import Counter

try:
    config.load_kube_config()
    _v1 = client.CoreV1Api()
    _kube_ok = True
except Exception as e:
    print(f"[k8s_tools] kubeconfig 加载失败: {e}")
    _kube_ok = False

# 全局缓存: Inspector 阶段 3 直接拿真实结构化数据
_DISCOVERED = {
    "unhealthy_pods": [],
    "high_restart_pods": [],
}


def _query_unhealthy_pods_raw():
    """内部: 返回完整异常 Pod 结构化列表 (生产用主接口)"""
    if not _kube_ok:
        return []
    pods = _v1.list_pod_for_all_namespaces(timeout_seconds=20).items
    unhealthy = []
    for p in pods:
        phase = p.status.phase
        ns = p.metadata.namespace
        name = p.metadata.name
        restart_total = 0
        reasons = []
        if p.status.container_statuses:
            for cs in p.status.container_statuses:
                restart_total += cs.restart_count or 0
                if cs.state and cs.state.waiting:
                    r = cs.state.waiting.reason
                    if r:
                        reasons.append(r)
                if cs.last_state and cs.last_state.terminated:
                    r = cs.last_state.terminated.reason
                    if r:
                        reasons.append(f"last:{r}")
        is_unhealthy = phase not in ("Running", "Succeeded") or restart_total >= 3
        if is_unhealthy:
            unhealthy.append({
                "namespace": ns,
                "pod": name,
                "phase": phase,
                "restarts": restart_total,
                "reason": ";".join(reasons) if reasons else "(no reason)",
                "node": p.spec.node_name or "",
            })
    unhealthy.sort(key=lambda x: x["restarts"], reverse=True)
    return unhealthy


def list_unhealthy_pods(limit: int = 20) -> str:
    """LLM 工具: 异常 Pod 文本输出 + 结构化缓存"""
    if not _kube_ok:
        return "[错误] K8s API 不可用"
    pods = _query_unhealthy_pods_raw()
    _DISCOVERED["unhealthy_pods"] = pods
    if not pods:
        return "[巡检结果] 所有 Pod 健康"
    head = f"[巡检结果] 共 {len(pods)} 个异常 Pod, 显示 Top {min(limit, len(pods))}:"
    lines = [head]
    for u in pods[:limit]:
        ns_name = u["namespace"]
        pod_name = u["pod"]
        phase = u["phase"]
        rs = u["restarts"]
        rsn = u["reason"][:60]
        lines.append(f"  - {ns_name}/{pod_name} phase={phase} restarts={rs} reason={rsn}")
    return "\n".join(lines)


def list_high_restart_pods(threshold: int = 100) -> str:
    """LLM 工具: 高重启 Pod"""
    if not _kube_ok:
        return "[错误] K8s API 不可用"
    pods = _v1.list_pod_for_all_namespaces(timeout_seconds=20).items
    high_restart = []
    for p in pods:
        if not p.status.container_statuses:
            continue
        total = sum((cs.restart_count or 0) for cs in p.status.container_statuses)
        if total >= threshold:
            high_restart.append({
                "namespace": p.metadata.namespace,
                "pod": p.metadata.name,
                "phase": p.status.phase,
                "restarts": total,
                "reason": "high_restart",
            })
    high_restart.sort(key=lambda x: x["restarts"], reverse=True)
    _DISCOVERED["high_restart_pods"] = high_restart
    if not high_restart:
        return f"[巡检结果] 无重启 >= {threshold} 的 Pod"
    head = f"[巡检结果] 重启 >= {threshold} 的 Pod 共 {len(high_restart)} 个, Top 10:"
    lines = [head]
    for u in high_restart[:10]:
        ns_name = u["namespace"]
        pod_name = u["pod"]
        rs = u["restarts"]
        lines.append(f"  - {ns_name}/{pod_name} restarts={rs}")
    return "\n".join(lines)


def get_cluster_overview() -> str:
    if not _kube_ok:
        return "[错误] K8s API 不可用"
    nodes = _v1.list_node(timeout_seconds=10).items
    pods = _v1.list_pod_for_all_namespaces(timeout_seconds=20).items
    phase_count = Counter(p.status.phase for p in pods)
    ns_set = set(p.metadata.namespace for p in pods)
    node_ready = sum(
        1 for n in nodes for c in (n.status.conditions or [])
        if c.type == "Ready" and c.status == "True"
    )
    total_nodes = len(nodes)
    total_pods = len(pods)
    ns_count = len(ns_set)
    lines = [
        "[集群总览]",
        f"  节点总数: {total_nodes} (Ready: {node_ready})",
        f"  Pod 总数: {total_pods}",
        f"  Pod 状态分布: {dict(phase_count)}",
        f"  命名空间数: {ns_count}",
    ]
    return "\n".join(lines)


def describe_pod_real(name: str, namespace: str) -> str:
    if not _kube_ok:
        return "[错误] K8s API 不可用"
    try:
        p = _v1.read_namespaced_pod(name=name, namespace=namespace)
    except Exception as e:
        return f"[错误] Pod {namespace}/{name} 查询失败: {e}"
    lines = [f"[Pod {namespace}/{name}]"]
    lines.append(f"  phase: {p.status.phase}")
    lines.append(f"  node: {p.spec.node_name}")
    if p.status.container_statuses:
        for cs in p.status.container_statuses:
            cname = cs.name
            ready = cs.ready
            rs = cs.restart_count
            lines.append(f"  container[{cname}] ready={ready} restarts={rs}")
            if cs.state and cs.state.waiting:
                wreason = cs.state.waiting.reason
                wmsg = (cs.state.waiting.message or "")[:120]
                lines.append(f"    waiting: {wreason} - {wmsg}")
            if cs.last_state and cs.last_state.terminated:
                t = cs.last_state.terminated
                treason = t.reason
                texit = t.exit_code
                lines.append(f"    last_terminated: {treason} exit={texit}")
    try:
        events = _v1.list_namespaced_event(
            namespace=namespace,
            field_selector=f"involvedObject.name={name}",
            limit=5,
        )
        if events.items:
            lines.append("  最近事件:")
            for e in events.items[-5:]:
                etype = e.type
                ereason = e.reason
                emsg = (e.message or "")[:120]
                lines.append(f"    [{etype}] {ereason}: {emsg}")
    except Exception:
        pass
    return "\n".join(lines)


def collect_all_real_issues():
    """非 LLM 工具: 直接从 K8s API 拿全部异常 Pod (排序好的结构化列表)"""
    return _query_unhealthy_pods_raw()


def get_discovered_cache():
    """非 LLM 工具: 获取全局缓存"""
    return {
        "unhealthy_pods": list(_DISCOVERED["unhealthy_pods"]),
        "high_restart_pods": list(_DISCOVERED["high_restart_pods"]),
    }


def get_pod_logs(name: str, namespace: str, lines: int = 50, previous: bool = True) -> str:


    """读取 Pod 容器日志 (含上次崩溃前的)"""


    if not _kube_ok:


        return "[错误] K8s API 不可用"


    try:


        p = _v1.read_namespaced_pod(name=name, namespace=namespace)


    except Exception as e:


        return f"[错误] Pod {namespace}/{name} 不存在: {e}"





    if not p.spec.containers:


        return "[错误] Pod 无 container 定义"





    out_lines = []


    for c in p.spec.containers:


        cname = c.name


        out_lines.append(f"=== container: {cname} ===")


        # 当前日志


        try:


            current = _v1.read_namespaced_pod_log(


                name=name,


                namespace=namespace,


                container=cname,


                tail_lines=lines,


                _request_timeout=10,


            )


            if current:


                out_lines.append("--- 当前日志(最后 {0} 行) ---".format(lines))


                out_lines.append(current.strip()[:2000])


            else:


                out_lines.append("(当前日志为空)")


        except Exception as e:


            out_lines.append(f"(当前日志读取失败: {e})")





        # 上次崩溃前的日志(关键!)


        if previous:


            try:


                prev = _v1.read_namespaced_pod_log(


                    name=name,


                    namespace=namespace,


                    container=cname,


                    previous=True,


                    tail_lines=lines,


                    _request_timeout=10,


                )


                if prev:


                    out_lines.append("--- 上次崩溃前日志(最后 {0} 行) ---".format(lines))


                    out_lines.append(prev.strip()[:2000])


            except Exception as e:


                msg = str(e)


                if "previous terminated container" in msg or "not found" in msg:


                    pass  # 没有 previous 日志属正常


                else:


                    out_lines.append(f"(上次日志读取失败: {msg[:80]})")


    return "\n".join(out_lines)
