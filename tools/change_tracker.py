"""变更追踪工具 (v2.12): 查询指定 namespace 最近 N 小时的 K8s 资源变更.

为什么需要:
- 业界 AIOps 80% 的故障由变更引起 (新版本/配置/扩缩容)
- 当前 Investigator 只看 "Pod 现在怎么了", 无法定位 "刚才有人改了什么"
- 这个工具补上 "变更-故障" 关联线, 提升 RCA 准确率

输出尽量小而有信号: 按 changed_at 倒序, 上限 50 条.
"""
import sys
from datetime import datetime, timedelta, timezone

try:
    from tools.k8s_tools import _v1, _apps_v1, _kube_ok
except Exception:
    _v1 = None
    _apps_v1 = None
    _kube_ok = False


def _log(msg):
    print(msg, flush=True)
    sys.stdout.flush()


def _to_iso(ts) -> str:
    """统一格式化为 ISO8601 UTC. 接受 datetime / None, 失败返回空串."""
    if not ts:
        return ""
    try:
        # kubernetes-python 返回的是 timezone-aware datetime
        if hasattr(ts, "isoformat"):
            return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        pass
    return str(ts)


def _within_window(ts, cutoff) -> bool:
    """判断 ts 是否在 cutoff 之后 (即落在窗口内)."""
    if not ts or not cutoff:
        return False
    try:
        if hasattr(ts, "tzinfo") and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts >= cutoff
    except Exception:
        return False


def _collect_deployments(ns: str, cutoff) -> list:
    """收集 Deployment 的 revision 变更."""
    out = []
    if not _apps_v1:
        return out
    try:
        deps = _apps_v1.list_namespaced_deployment(
            namespace=ns, timeout_seconds=10).items
    except Exception:
        return out
    for d in deps:
        anns = (d.metadata.annotations or {})
        rev = anns.get("deployment.kubernetes.io/revision", "")
        # 看最新一条 condition 的 lastUpdateTime 判定是否近期变更
        conds = (d.status.conditions or [])
        latest_ts = None
        for c in conds:
            if c.last_update_time and (not latest_ts or c.last_update_time > latest_ts):
                latest_ts = c.last_update_time
        if not _within_window(latest_ts, cutoff):
            continue
        # 看 image 推断 change_type
        change_type = "rolling_update"
        detail_parts = []
        if rev:
            detail_parts.append(f"revision={rev}")
        try:
            containers = d.spec.template.spec.containers or []
            images = [c.image for c in containers if c.image]
            if images:
                detail_parts.append(f"image={','.join(images[:2])}")
        except Exception:
            pass
        replicas = d.spec.replicas
        if replicas is not None:
            detail_parts.append(f"replicas={replicas}")
        out.append({
            "kind": "Deployment",
            "name": d.metadata.name,
            "change_type": change_type,
            "changed_at": _to_iso(latest_ts),
            "_ts": latest_ts,
            "detail": "; ".join(detail_parts),
        })
    return out


def _collect_replicasets(ns: str, cutoff) -> list:
    """收集近期创建的 ReplicaSet (= 新版本上线)."""
    out = []
    if not _apps_v1:
        return out
    try:
        rss = _apps_v1.list_namespaced_replica_set(
            namespace=ns, timeout_seconds=10).items
    except Exception:
        return out
    for rs in rss:
        ts = rs.metadata.creation_timestamp
        if not _within_window(ts, cutoff):
            continue
        # 跳过空 RS (副本数=0 的旧版本)
        replicas = rs.spec.replicas or 0
        owner = ""
        for o in (rs.metadata.owner_references or []):
            if o.kind == "Deployment":
                owner = o.name
                break
        detail = f"replicas={replicas}"
        if owner:
            detail += f"; owner=Deployment/{owner}"
        out.append({
            "kind": "ReplicaSet",
            "name": rs.metadata.name,
            "change_type": "created",
            "changed_at": _to_iso(ts),
            "_ts": ts,
            "detail": detail,
        })
    return out


def _collect_statefulsets(ns: str, cutoff) -> list:
    out = []
    if not _apps_v1:
        return out
    try:
        sts = _apps_v1.list_namespaced_stateful_set(
            namespace=ns, timeout_seconds=10).items
    except Exception:
        return out
    for s in sts:
        conds = (s.status.conditions or [])
        latest_ts = None
        for c in conds:
            if c.last_transition_time and (
                    not latest_ts or c.last_transition_time > latest_ts):
                latest_ts = c.last_transition_time
        # StatefulSet 通常没什么 condition, 退化看 creationTimestamp
        if not latest_ts:
            latest_ts = s.metadata.creation_timestamp
        if not _within_window(latest_ts, cutoff):
            continue
        replicas = s.spec.replicas
        out.append({
            "kind": "StatefulSet",
            "name": s.metadata.name,
            "change_type": "updated",
            "changed_at": _to_iso(latest_ts),
            "_ts": latest_ts,
            "detail": f"replicas={replicas}",
        })
    return out


def _collect_configmaps_secrets(ns: str, cutoff) -> list:
    """收集近期创建/更新的 ConfigMap/Secret.

    K8s API 不直接给 "last modified" 字段, 用 managedFields 里最新 update time 兜底.
    没有 managedFields 时回退到 creationTimestamp.
    """
    out = []
    if not _v1:
        return out
    try:
        cms = _v1.list_namespaced_config_map(
            namespace=ns, timeout_seconds=10).items
    except Exception:
        cms = []
    try:
        secrets = _v1.list_namespaced_secret(
            namespace=ns, timeout_seconds=10).items
    except Exception:
        secrets = []

    for resources, kind in [(cms, "ConfigMap"), (secrets, "Secret")]:
        for r in resources:
            mf = r.metadata.managed_fields or []
            latest_ts = None
            for f in mf:
                ft = getattr(f, "time", None)
                if ft and (not latest_ts or ft > latest_ts):
                    latest_ts = ft
            if not latest_ts:
                latest_ts = r.metadata.creation_timestamp
            if not _within_window(latest_ts, cutoff):
                continue
            # 跳过 service account token / dockercfg 类自动生成的 Secret
            if kind == "Secret":
                stype = r.type or ""
                if stype.startswith("kubernetes.io/"):
                    continue
            ct = "created" if latest_ts == r.metadata.creation_timestamp else "modified"
            out.append({
                "kind": kind,
                "name": r.metadata.name,
                "change_type": ct,
                "changed_at": _to_iso(latest_ts),
                "_ts": latest_ts,
                "detail": f"resourceVersion={r.metadata.resource_version}",
            })
    return out


def _collect_events(ns: str, cutoff) -> list:
    """收集近期相关 Events (ScalingReplicaSet / SuccessfulCreate / Killing)."""
    out = []
    if not _v1:
        return out
    try:
        evs = _v1.list_namespaced_event(
            namespace=ns, timeout_seconds=10).items
    except Exception:
        return out
    INTERESTING = {
        "ScalingReplicaSet", "SuccessfulCreate", "Killing",
        "FailedCreate", "BackOff",
    }
    for e in evs:
        if (e.reason or "") not in INTERESTING:
            continue
        ts = e.last_timestamp or e.event_time or e.metadata.creation_timestamp
        if not _within_window(ts, cutoff):
            continue
        involved = e.involved_object
        target = ""
        if involved:
            target = f"{involved.kind}/{involved.name}"
        msg = (e.message or "")[:120]
        out.append({
            "kind": "Event",
            "name": target or e.metadata.name,
            "change_type": e.reason or "event",
            "changed_at": _to_iso(ts),
            "_ts": ts,
            "detail": msg,
        })
    return out


def get_recent_changes(namespace: str, hours: int = 2) -> str:
    """查询指定 namespace 最近 N 小时的变更, 用于 Investigator 关联诊断.

    返回格式化的字符串 (跟其他工具返回风格一致, 便于 LLM 阅读).
    错误情况下也返回字符串而非抛异常 (Investigator 不能崩).
    """
    if not _kube_ok:
        return "[变更追踪] K8s API 不可用"
    if not namespace:
        return "[变更追踪] namespace 必填"
    try:
        hours = int(hours)
    except Exception:
        hours = 2
    hours = max(1, min(24, hours))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    all_changes = []
    for collector in (_collect_deployments, _collect_replicasets,
                      _collect_statefulsets, _collect_configmaps_secrets,
                      _collect_events):
        try:
            all_changes.extend(collector(namespace, cutoff))
        except Exception as e:
            _log(f"[change_tracker] {collector.__name__} 失败: {e}")
            continue

    if not all_changes:
        return f"[变更追踪] {namespace} 最近 {hours}h 无 Deployment/ReplicaSet/" \
               f"ConfigMap/Secret 变更"

    # 倒序 + 截断
    all_changes.sort(key=lambda x: x.get("_ts") or datetime.min.replace(
        tzinfo=timezone.utc), reverse=True)
    all_changes = all_changes[:50]

    lines = [f"[变更追踪] {namespace} 最近 {hours}h 共 {len(all_changes)} 项变更 "
             f"(按时间倒序, 最多 50 条):"]
    for c in all_changes:
        lines.append(
            f"  - [{c['changed_at']}] {c['kind']}/{c['name']} "
            f"{c['change_type']} | {c['detail']}"
        )
    return "\n".join(lines)
