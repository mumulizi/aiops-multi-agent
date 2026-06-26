"""K8s 真实工具 + 结构化数据缓存"""
from kubernetes import client, config
from collections import Counter

try:
    config.load_kube_config()
    _v1 = client.CoreV1Api()
    _apps_v1 = client.AppsV1Api()  # Deployment / StatefulSet / DaemonSet 操作
    _kube_ok = True
except Exception as e:
    print(f"[k8s_tools] kubeconfig 加载失败: {e}")
    _v1 = None
    _apps_v1 = None
    _kube_ok = False

# 全局缓存: Inspector 阶段 3 直接拿真实结构化数据
_DISCOVERED = {
    "unhealthy_pods": [],
    "high_restart_pods": [],
}


def _query_unhealthy_pods_raw():
    """内部: 返回完整异常 Pod 结构化列表 (生产用主接口).

    "异常" 判定 (满足任一):
    1. phase 不是 Running/Succeeded (Failed/Pending/Unknown)
    2. 当前有容器处于 waiting 状态 (CrashLoopBackOff/ImagePullBackOff/Error/...)
    3. 当前有容器 not ready

    注意: 单纯历史 restart 数高 + 当前正常运行的 Pod 不算异常.
    这种 Pod 可能是早期不稳定但现在已稳定, 不需要打扰.
    """
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
        has_waiting = False        # 当前是否有容器卡在 waiting (真实持续故障)
        any_not_ready = False      # 当前是否有容器 not ready
        if p.status.container_statuses:
            for cs in p.status.container_statuses:
                restart_total += cs.restart_count or 0
                if not cs.ready:
                    any_not_ready = True
                if cs.state and cs.state.waiting:
                    has_waiting = True
                    r = cs.state.waiting.reason
                    if r:
                        reasons.append(r)
                if cs.last_state and cs.last_state.terminated:
                    r = cs.last_state.terminated.reason
                    if r:
                        reasons.append(f"last:{r}")
        # v2.5: 异常判定收紧.
        # 旧版: phase 异常 或 restart>=3 都算异常 → 过于灵敏, Running+restart 高的"
        #   稳定 Pod"会反复进流水线.
        # 新版: 只看"当前是否真的在故障":
        #   - phase Failed/Pending/Unknown (不是 Running)
        #   - 或当前有容器 waiting (CrashLoop/ImagePull 等持续故障状态)
        #   - 或容器 not ready (健康检查失败等)
        # restart 数仅用作 severity 分级, 不再作为"是否异常"的判据.
        is_unhealthy = (
            phase not in ("Running", "Succeeded")
            or has_waiting
            or any_not_ready
        )
        if is_unhealthy:
            # 提取 owner_kind, 用于 Remediator 判断该走哪种修复路径
            # 只承认 K8s 真正的控制器 kind, 其他 (含 Node 静态 Pod) 都当 BarePod
            VALID_CONTROLLERS = {"ReplicaSet", "DaemonSet", "StatefulSet", "Job"}
            owners = p.metadata.owner_references or []
            owner_kind = "BarePod"  # 默认: 无 owner / Node 静态 Pod / 不识别的 kind
            for o in owners:
                # 必须是 controller=true 且 kind 在白名单
                is_controller = getattr(o, "controller", False)
                if is_controller and o.kind in VALID_CONTROLLERS:
                    owner_kind = o.kind
                    break
            unhealthy.append({
                "namespace": ns,
                "pod": name,
                "phase": phase,
                "restarts": restart_total,
                "reason": ";".join(reasons) if reasons else "(no reason)",
                "node": p.spec.node_name or "",
                "owner_kind": owner_kind,
            })
    # 排序: 先按"是否卡死状态" (ImagePullBackOff/ConfigError 等永不自愈),
    # 再按 restart 数. 这样 restart=0 但卡死的 Pod 不会被埋在末尾.
    _NON_HEALING_KEYWORDS = (
        "imagepull", "errimage", "imagebackoff", "invalidimage",
        "createcontainerconfigerror", "createcontainererror",
        "runcontainererror", "configerror",
    )

    def _is_stuck(item):
        r = (item.get("reason") or "").lower()
        return 1 if any(kw in r for kw in _NON_HEALING_KEYWORDS) else 0

    unhealthy.sort(key=lambda x: (_is_stuck(x), x["restarts"]), reverse=True)
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
    """获取 Pod 完整描述, 含 message/conditions/last_terminated/events.

    设计要点 (面向 LLM 排障):
    - 完整保留 message 字段 (不截断 / 不截短) - 这是 ImagePullBackOff/Pending 的黄金信息
    - 采集 last_terminated.message - 含崩溃前最后的关键输出
    - 采集 conditions.message - Pending Pod 调度失败原因
    - 采集 pod.status.message - Pod 整体状态信息
    - 采集 image 列表 - 调试镜像问题必需
    - 输出整体限制在 3000 字内, 优先保留 message 内容
    """
    if not _kube_ok:
        return "[错误] K8s API 不可用"
    try:
        p = _v1.read_namespaced_pod(name=name, namespace=namespace)
    except Exception as e:
        return f"[错误] Pod {namespace}/{name} 查询失败: {e}"

    lines = [f"[Pod {namespace}/{name}]"]
    lines.append(f"  phase    : {p.status.phase}")
    lines.append(f"  node     : {p.spec.node_name or '(unscheduled)'}")

    # Pod 整体 message / reason (常被忽视的黄金字段)
    if p.status.message:
        lines.append(f"  message  : {p.status.message}")
    if p.status.reason:
        lines.append(f"  reason   : {p.status.reason}")

    # Pod conditions (Pending 调度失败原因在这)
    if p.status.conditions:
        abnormal = [c for c in p.status.conditions if c.status != "True" or c.message]
        if abnormal:
            lines.append("  conditions:")
            for c in abnormal:
                msg = c.message or ""
                lines.append(
                    f"    [{c.type}] {c.status} reason={c.reason or '-'}"
                    + (f" msg={msg}" if msg else "")
                )

    # 镜像列表 (ImagePullBackOff 必备)
    if p.spec.containers:
        lines.append("  containers:")
        for c in p.spec.containers:
            lines.append(f"    - name={c.name} image={c.image}")
    if p.spec.init_containers:
        lines.append("  init_containers:")
        for c in p.spec.init_containers:
            lines.append(f"    - name={c.name} image={c.image}")

    # 容器状态 (含完整 message + last_terminated)
    def _add_statuses(statuses, label):
        if not statuses:
            return
        lines.append(f"  {label}_statuses:")
        for cs in statuses:
            lines.append(f"    [{cs.name}] ready={cs.ready} restarts={cs.restart_count}")
            if cs.state:
                if cs.state.waiting:
                    w = cs.state.waiting
                    lines.append(f"      waiting: reason={w.reason or '-'}")
                    if w.message:
                        lines.append(f"        message: {w.message}")
                elif cs.state.terminated:
                    t = cs.state.terminated
                    lines.append(
                        f"      terminated: reason={t.reason or '-'} "
                        f"exit_code={t.exit_code} signal={t.signal or '-'}"
                    )
                    if t.message:
                        lines.append(f"        message: {t.message}")
                elif cs.state.running:
                    lines.append(f"      running: started_at={cs.state.running.started_at}")
            if cs.last_state and cs.last_state.terminated:
                t = cs.last_state.terminated
                lines.append(
                    f"      last_terminated: reason={t.reason or '-'} "
                    f"exit_code={t.exit_code} signal={t.signal or '-'}"
                )
                if t.message:
                    lines.append(f"        message: {t.message}")

    _add_statuses(p.status.init_container_statuses, "init_container")
    _add_statuses(p.status.container_statuses, "container")

    # 事件 (完整 message)
    try:
        events = _v1.list_namespaced_event(
            namespace=namespace,
            field_selector=f"involvedObject.name={name}",
            limit=10,
        )
        if events.items:
            lines.append("  events (recent 10):")
            for e in events.items[-10:]:
                etype = e.type or "-"
                ereason = e.reason or "-"
                emsg = e.message or ""
                lines.append(f"    [{etype}] {ereason}: {emsg}")
    except Exception:
        pass

    full = "\n".join(lines)
    # 整体限制 3000 字, 超出时截断并提示
    if len(full) > 3000:
        full = full[:2950] + "\n  ...(output truncated, original length=" + str(len(full)) + ")"
    return full


def collect_all_real_issues():
    """非 LLM 工具: 直接从 K8s API 拿全部异常 Pod (排序好的结构化列表)"""
    return _query_unhealthy_pods_raw()


def get_discovered_cache():
    """非 LLM 工具: 获取全局缓存"""
    return {
        "unhealthy_pods": list(_DISCOVERED["unhealthy_pods"]),
        "high_restart_pods": list(_DISCOVERED["high_restart_pods"]),
    }


def get_pod_logs(name: str, namespace: str, lines: int = 50,
                 previous: bool = True) -> str:
    """读取 Pod 所有容器的日志 (含 init containers + 上次崩溃前日志).

    v2.5 升级:
    - 同时遍历 p.spec.containers 和 p.spec.init_containers
    - 任何容器读取失败都显式提示 (NotFound / 权限 / 网络)
    - 单容器日志限制 3000 字, 多容器累计才不会爆 LLM 上下文
    """
    if not _kube_ok:
        return "[错误] K8s API 不可用"
    try:
        p = _v1.read_namespaced_pod(name=name, namespace=namespace)
    except Exception as e:
        return f"[错误] Pod {namespace}/{name} 不存在: {e}"

    main_containers = list(p.spec.containers or [])
    init_containers = list(p.spec.init_containers or [])
    if not main_containers and not init_containers:
        return "[错误] Pod 无 container 定义"

    out_lines = []
    out_lines.append(
        f"[Pod {namespace}/{name}] containers={len(main_containers)} "
        f"init_containers={len(init_containers)}"
    )

    def _read_container_logs(cname, label):
        out_lines.append(f"\n=== {label}: {cname} ===")
        try:
            current = _v1.read_namespaced_pod_log(
                name=name, namespace=namespace, container=cname,
                tail_lines=lines, _request_timeout=10,
            )
            if current:
                out_lines.append(f"--- 当前日志(最后 {lines} 行) ---")
                out_lines.append(current.strip()[:3000])
            else:
                out_lines.append("(当前日志为空)")
        except Exception as e:
            out_lines.append(f"(当前日志读取失败: {str(e)[:120]})")

        if previous:
            try:
                prev = _v1.read_namespaced_pod_log(
                    name=name, namespace=namespace, container=cname,
                    previous=True, tail_lines=lines, _request_timeout=10,
                )
                if prev:
                    out_lines.append(f"--- 上次崩溃前日志(最后 {lines} 行) ---")
                    out_lines.append(prev.strip()[:3000])
            except Exception as e:
                msg = str(e)
                if "previous terminated container" in msg or "not found" in msg:
                    pass
                else:
                    out_lines.append(f"(上次日志读取失败: {msg[:120]})")

    for c in init_containers:
        _read_container_logs(c.name, "init container")
    for c in main_containers:
        _read_container_logs(c.name, "container")

    return "\n".join(out_lines)
