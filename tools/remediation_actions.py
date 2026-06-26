"""K8s 修复操作工具集 (L3 白名单).

每个动作都有:
- 严格的输入校验 (target 格式, 资源类型确认)
- dry_run 模式 (默认开启, 不真动手)
- 详细日志 (前后状态对比)

设计原则:
1. 每个动作都是幂等的 (重复执行不出错)
2. 失败要返回结构化错误, 不抛异常
3. 成功要返回 before/after 快照
4. 不在这一层做"该不该执行"的判断 (那是 Approval Gate 的事)
"""
import time

from tools.k8s_tools import _v1, _apps_v1, _kube_ok


def _split_target(target: str):
    """target 格式: 'namespace/pod-name'"""
    if "/" not in target:
        return None, None
    parts = target.split("/", 1)
    return parts[0], parts[1]


def _capture_pod_state(namespace: str, name: str) -> dict:
    """捕获 Pod 当前状态快照 (供 before/after 对比)"""
    if not _kube_ok:
        return {"error": "k8s api not available"}
    try:
        p = _v1.read_namespaced_pod(name=name, namespace=namespace)
    except Exception as e:
        return {"error": f"pod not found: {e}"}
    snap = {
        "phase": p.status.phase,
        "node": p.spec.node_name or "",
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if p.status.container_statuses:
        snap["containers"] = []
        total_restarts = 0
        any_not_ready = False
        waiting_reasons = []  # 收集所有容器的 waiting.reason, 供 Validator 判断"重启无救"型故障
        for cs in p.status.container_statuses:
            total_restarts += cs.restart_count or 0
            if not cs.ready:
                any_not_ready = True
            wreason = ""
            wmsg = ""
            if cs.state and cs.state.waiting:
                wreason = cs.state.waiting.reason or ""
                wmsg = (cs.state.waiting.message or "")[:200]
                if wreason:
                    waiting_reasons.append(wreason)
            snap["containers"].append({
                "name": cs.name,
                "ready": cs.ready,
                "restart_count": cs.restart_count or 0,
                "waiting_reason": wreason,
                "waiting_message": wmsg,
            })
        snap["total_restarts"] = total_restarts
        snap["any_not_ready"] = any_not_ready
        snap["waiting_reasons"] = waiting_reasons
    return snap


def _check_image_pull_issue(p) -> tuple:
    """检查 Pod 是否处于 ImagePull 类异常.
    返回 (is_image_issue: bool, reason_str: str)"""
    statuses = []
    if p.status.container_statuses:
        statuses.extend(p.status.container_statuses)
    if p.status.init_container_statuses:
        statuses.extend(p.status.init_container_statuses)
    for cs in statuses:
        if cs.state and cs.state.waiting:
            wreason = (cs.state.waiting.reason or "").lower()
            if "imagepull" in wreason or "errimage" in wreason or "imagebackoff" in wreason:
                return True, cs.state.waiting.reason
    return False, ""


def _check_owner_safe(p) -> tuple:
    """检查 Pod owner 是否在 L3 安全列表.
    返回 (ok: bool, owner_kind: str, reason: str)"""
    owners = p.metadata.owner_references or []
    if not owners:
        return False, "", "pod has no owner, refuse to delete"
    owner_kinds = [o.kind for o in owners]
    SAFE_OWNERS = {"ReplicaSet", "DaemonSet"}
    if not any(k in SAFE_OWNERS for k in owner_kinds):
        return False, "", f"pod owner is {owner_kinds}, only {sorted(SAFE_OWNERS)} are L3-allowed"
    owner_kind = next(k for k in owner_kinds if k in SAFE_OWNERS)
    return True, owner_kind, "ok"


def delete_evicted_pod(target: str, dry_run: bool = True) -> dict:
    """删除 Evicted 状态的 Pod (集群清理类操作, 极低风险)"""
    ns, name = _split_target(target)
    if not ns or not name:
        return {"ok": False, "reason": f"invalid target format: {target}"}
    if not _kube_ok:
        return {"ok": False, "reason": "k8s api not available"}

    try:
        p = _v1.read_namespaced_pod(name=name, namespace=ns)
    except Exception as e:
        return {"ok": False, "reason": f"pod not found: {e}"}

    is_evicted = (
        p.status.phase == "Failed"
        and (p.status.reason == "Evicted" or "Evicted" in (p.status.reason or ""))
    )
    if not is_evicted:
        return {
            "ok": False,
            "reason": f"pod is not Evicted (phase={p.status.phase}, reason={p.status.reason})",
        }

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "message": f"[DRY-RUN] would delete evicted pod {target}",
        }

    try:
        _v1.delete_namespaced_pod(name=name, namespace=ns)
        return {"ok": True, "message": f"deleted evicted pod {target}"}
    except Exception as e:
        return {"ok": False, "reason": f"delete failed: {e}"}


def restart_pod(target: str, dry_run: bool = True) -> dict:
    """重启 Pod (实际是 delete pod 让 owner controller 重建).
    L3 白名单允许的 owner: ReplicaSet (Deployment) / DaemonSet.
    StatefulSet 重启可能影响数据一致性, 走 L2 人审."""
    ns, name = _split_target(target)
    if not ns or not name:
        return {"ok": False, "reason": f"invalid target format: {target}"}
    if not _kube_ok:
        return {"ok": False, "reason": "k8s api not available"}

    try:
        p = _v1.read_namespaced_pod(name=name, namespace=ns)
    except Exception as e:
        return {"ok": False, "reason": f"pod not found: {e}"}

    ok, owner_kind, reason = _check_owner_safe(p)
    if not ok:
        return {"ok": False, "reason": reason}

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "message": f"[DRY-RUN] would restart pod {target} (owner={owner_kind} will recreate it)",
        }

    try:
        _v1.delete_namespaced_pod(name=name, namespace=ns)
        return {
            "ok": True,
            "message": f"restart triggered for pod {target} (owner={owner_kind} will recreate)",
        }
    except Exception as e:
        return {"ok": False, "reason": f"delete failed: {e}"}


def delete_completed_job_pod(target: str, dry_run: bool = True) -> dict:
    """清理已完成 (Succeeded) 的 Pod"""
    ns, name = _split_target(target)
    if not ns or not name:
        return {"ok": False, "reason": f"invalid target format: {target}"}
    if not _kube_ok:
        return {"ok": False, "reason": "k8s api not available"}

    try:
        p = _v1.read_namespaced_pod(name=name, namespace=ns)
    except Exception as e:
        return {"ok": False, "reason": f"pod not found: {e}"}

    if p.status.phase != "Succeeded":
        return {
            "ok": False,
            "reason": f"pod is not Succeeded (phase={p.status.phase})",
        }

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "message": f"[DRY-RUN] would delete completed pod {target}",
        }

    try:
        _v1.delete_namespaced_pod(name=name, namespace=ns)
        return {"ok": True, "message": f"deleted completed pod {target}"}
    except Exception as e:
        return {"ok": False, "reason": f"delete failed: {e}"}


def restart_pod_for_image_pull(target: str, dry_run: bool = True) -> dict:
    """重启 ImagePullBackOff/ErrImagePull 状态的 Pod 让控制器重新拉镜像.

    安全性: 仅治标 (临时性网络抖动有效), 镜像名错/认证失效需要改 Deployment.
    重启 100% 安全 (坏情况是没用, 不会出事故)."""
    ns, name = _split_target(target)
    if not ns or not name:
        return {"ok": False, "reason": f"invalid target format: {target}"}
    if not _kube_ok:
        return {"ok": False, "reason": "k8s api not available"}

    try:
        p = _v1.read_namespaced_pod(name=name, namespace=ns)
    except Exception as e:
        return {"ok": False, "reason": f"pod not found: {e}"}

    is_image_issue, image_reason = _check_image_pull_issue(p)
    if not is_image_issue:
        return {
            "ok": False,
            "reason": "pod is not ImagePullBackOff/ErrImagePull (no image issue in container_statuses)",
        }

    ok, owner_kind, reason = _check_owner_safe(p)
    if not ok:
        return {"ok": False, "reason": reason}

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "message": (
                f"[DRY-RUN] would restart pod {target} "
                f"(reason={image_reason}, owner={owner_kind} will retry image pull)"
            ),
        }

    try:
        _v1.delete_namespaced_pod(name=name, namespace=ns)
        return {
            "ok": True,
            "message": (
                f"restart triggered for pod {target} "
                f"(was {image_reason}, owner={owner_kind} will retry image pull)"
            ),
        }
    except Exception as e:
        return {"ok": False, "reason": f"delete failed: {e}"}


def delete_failed_pod(target: str, dry_run: bool = True) -> dict:
    """清理 Failed phase 的 Pod (含 Evicted 之外的 Failed 情况).

    Pod phase=Failed 表示已终止且不会恢复, 删除是安全清理.
    """
    ns, name = _split_target(target)
    if not ns or not name:
        return {"ok": False, "reason": f"invalid target format: {target}"}
    if not _kube_ok:
        return {"ok": False, "reason": "k8s api not available"}

    try:
        p = _v1.read_namespaced_pod(name=name, namespace=ns)
    except Exception as e:
        return {"ok": False, "reason": f"pod not found: {e}"}

    if p.status.phase != "Failed":
        return {
            "ok": False,
            "reason": f"pod phase is {p.status.phase}, not Failed (refuse to delete)",
        }

    fail_reason = p.status.reason or "Unknown"

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "message": f"[DRY-RUN] would delete failed pod {target} (reason={fail_reason})",
        }

    try:
        _v1.delete_namespaced_pod(name=name, namespace=ns)
        return {
            "ok": True,
            "message": f"deleted failed pod {target} (reason={fail_reason})",
        }
    except Exception as e:
        return {"ok": False, "reason": f"delete failed: {e}"}


def restart_statefulset_pod(target: str, dry_run: bool = True) -> dict:
    """重启 StatefulSet 管理的 Pod (L2 人审专用).

    与 restart_pod 的区别:
    - restart_pod 限 ReplicaSet/DaemonSet, 拒绝 StatefulSet
    - restart_statefulset_pod 反过来仅限 StatefulSet

    StatefulSet Pod 重启风险点 (这就是为何走 L2 人审):
    - 单副本 + PV: 数据短暂不可用
    - 主从架构: 删主节点会触发主从切换
    - 命名固定 (xxx-0/-1/-2): 控制器按顺序重建, 用同名
    """
    ns, name = _split_target(target)
    if not ns or not name:
        return {"ok": False, "reason": f"invalid target format: {target}"}
    if not _kube_ok:
        return {"ok": False, "reason": "k8s api not available"}

    try:
        p = _v1.read_namespaced_pod(name=name, namespace=ns)
    except Exception as e:
        return {"ok": False, "reason": f"pod not found: {e}"}

    owners = p.metadata.owner_references or []
    if not owners:
        return {"ok": False, "reason": "pod has no owner, refuse to delete"}
    owner_kinds = [o.kind for o in owners]
    if "StatefulSet" not in owner_kinds:
        return {
            "ok": False,
            "reason": f"pod owner is {owner_kinds}, this action is only for StatefulSet pods",
        }

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "message": (
                f"[DRY-RUN] would restart StatefulSet pod {target} "
                f"(controller will recreate with same name)"
            ),
        }

    try:
        _v1.delete_namespaced_pod(name=name, namespace=ns)
        return {
            "ok": True,
            "message": (
                f"restart triggered for StatefulSet pod {target} "
                f"(controller will recreate with same name)"
            ),
        }
    except Exception as e:
        return {"ok": False, "reason": f"delete failed: {e}"}


# ============================================================
# L3 自动: cordon_node (标节点不可调度, 不影响存量 Pod)
# ============================================================
def cordon_node(target: str, dry_run: bool = True) -> dict:
    """标节点为 unschedulable (kubectl cordon 等价).

    适用场景:
    - 节点频繁失联 / NotReady, 阻止新 Pod 调度上去
    - 节点磁盘/内存压力, 暂停接受新负载
    - 节点准备维护, 提前 cordon

    安全性 (为何归 L3):
    - 只改 spec.unschedulable=True, 不动存量 Pod
    - 完全可逆 (uncordon_node 恢复)
    - 影响面仅限"未来调度", 没有立刻的破坏

    target 格式: 直接是 node 名 (无 namespace), e.g. "192.168.48.78"
    或者兼容 "node/<name>" 格式 (从 ApprovalGate 过来时可能带前缀).
    """
    node_name = _normalize_node_target(target)
    if not node_name:
        return {"ok": False, "reason": f"invalid node target: {target!r}"}
    if not _kube_ok:
        return {"ok": False, "reason": "k8s api not available"}

    try:
        node = _v1.read_node(name=node_name)
    except Exception as e:
        return {"ok": False, "reason": f"node not found: {e}"}

    # 已经 cordon 了就直接成功 (幂等)
    if node.spec.unschedulable:
        return {
            "ok": True,
            "message": f"node {node_name} already cordoned (no-op)",
            "already_cordoned": True,
        }

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "message": f"[DRY-RUN] would cordon node {node_name} (set unschedulable=true)",
        }

    try:
        body = {"spec": {"unschedulable": True}}
        _v1.patch_node(name=node_name, body=body)
        return {
            "ok": True,
            "message": f"cordoned node {node_name} (no new pods will be scheduled)",
        }
    except Exception as e:
        return {"ok": False, "reason": f"cordon failed: {e}"}


def uncordon_node(target: str, dry_run: bool = True) -> dict:
    """恢复节点可调度 (cordon_node 的反操作).

    用途: cordon 之后节点恢复正常, 让调度器重新使用它.
    """
    node_name = _normalize_node_target(target)
    if not node_name:
        return {"ok": False, "reason": f"invalid node target: {target!r}"}
    if not _kube_ok:
        return {"ok": False, "reason": "k8s api not available"}

    try:
        node = _v1.read_node(name=node_name)
    except Exception as e:
        return {"ok": False, "reason": f"node not found: {e}"}

    if not node.spec.unschedulable:
        return {
            "ok": True,
            "message": f"node {node_name} is already schedulable (no-op)",
            "already_schedulable": True,
        }

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "message": f"[DRY-RUN] would uncordon node {node_name}",
        }

    try:
        body = {"spec": {"unschedulable": False}}
        _v1.patch_node(name=node_name, body=body)
        return {"ok": True, "message": f"uncordoned node {node_name}"}
    except Exception as e:
        return {"ok": False, "reason": f"uncordon failed: {e}"}


def _normalize_node_target(target: str):
    """把 target 归一成 node 名. 接受三种格式:
    - "192.168.48.78"          → "192.168.48.78"
    - "node/192.168.48.78"     → "192.168.48.78"  (CLI 兼容)
    - "/192.168.48.78"         → "192.168.48.78"  (ApprovalGate 兼容)
    """
    if not target:
        return None
    target = target.strip()
    if target.startswith("node/"):
        return target[len("node/"):]
    if target.startswith("/"):
        return target[1:]
    if "/" in target:
        # 不应该到这里, 但宽容处理: 取最后一段
        return target.split("/")[-1]
    return target


# ============================================================
# L2 人审: scale_deployment (调副本数)
# ============================================================
# 安全边界: 单次扩缩最多 ±5 副本, 总副本数限制在 [0, 50]
# 防 LLM 输出 replicas=999 这种胡言, 也防一次性扩到 100 副本打崩集群.
SCALE_MAX_DELTA = 5
SCALE_REPLICAS_MAX = 50


def scale_deployment(target: str, dry_run: bool = True,
                     replicas: int = None, delta: int = None) -> dict:
    """调整 Deployment 副本数.

    target: "namespace/deployment-name" (注意是 deployment 名, 不是 pod 名)
    指定模式 (二选一):
    - replicas=N: 设置成绝对值 N
    - delta=±N: 在当前副本数基础上 ±N (推荐, 更安全)

    安全边界:
    - 单次 |delta| <= SCALE_MAX_DELTA (5)
    - 最终 replicas <= SCALE_REPLICAS_MAX (50)
    - replicas >= 0
    """
    ns, name = _split_target(target)
    if not ns or not name:
        return {"ok": False, "reason": f"invalid target format: {target}"}
    if not _kube_ok or _apps_v1 is None:
        return {"ok": False, "reason": "k8s api not available"}
    if replicas is None and delta is None:
        return {"ok": False, "reason": "must specify replicas= or delta="}
    if replicas is not None and delta is not None:
        return {"ok": False, "reason": "replicas and delta are mutually exclusive"}

    try:
        dep = _apps_v1.read_namespaced_deployment(name=name, namespace=ns)
    except Exception as e:
        return {"ok": False, "reason": f"deployment not found: {e}"}

    current = dep.spec.replicas or 0
    if delta is not None:
        if abs(delta) > SCALE_MAX_DELTA:
            return {
                "ok": False,
                "reason": f"|delta|={abs(delta)} exceeds SCALE_MAX_DELTA={SCALE_MAX_DELTA}",
            }
        target_replicas = current + delta
    else:
        target_replicas = replicas

    # 边界检查
    if target_replicas < 0:
        return {"ok": False, "reason": f"target replicas={target_replicas} cannot be negative"}
    if target_replicas > SCALE_REPLICAS_MAX:
        return {
            "ok": False,
            "reason": f"target replicas={target_replicas} exceeds SCALE_REPLICAS_MAX={SCALE_REPLICAS_MAX}",
        }
    if target_replicas == current:
        return {
            "ok": True,
            "message": f"deployment {target} already has {current} replicas (no-op)",
            "no_op": True,
        }

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "message": (
                f"[DRY-RUN] would scale deployment {target} "
                f"from {current} to {target_replicas} replicas"
            ),
            "before": current,
            "after": target_replicas,
        }

    try:
        body = {"spec": {"replicas": target_replicas}}
        _apps_v1.patch_namespaced_deployment_scale(name=name, namespace=ns, body=body)
        return {
            "ok": True,
            "message": f"scaled deployment {target}: {current} → {target_replicas}",
            "before": current,
            "after": target_replicas,
        }
    except Exception as e:
        return {"ok": False, "reason": f"scale failed: {e}"}


# ============================================================
# L2 人审: rollback_deployment (回滚到上一个 ReplicaSet)
# ============================================================
def rollback_deployment(target: str, dry_run: bool = True) -> dict:
    """回滚 Deployment 到上一个版本 (kubectl rollout undo 等价).

    target: "namespace/deployment-name"

    实现方式:
    - 找 deployment 关联的所有 ReplicaSet, 按 revision 排序
    - 取倒数第二个 (上一个版本) 的 pod template
    - 把 deployment 的 template 改回那个版本

    适用场景: 新版本上线崩了, 一键回退到上版本.

    限制:
    - deployment 必须开了 .spec.revisionHistoryLimit > 0 (默认 10, 通常都开)
    - 至少要有 2 个 revision (新版本 + 旧版本) 才能回滚
    """
    ns, name = _split_target(target)
    if not ns or not name:
        return {"ok": False, "reason": f"invalid target format: {target}"}
    if not _kube_ok or _apps_v1 is None:
        return {"ok": False, "reason": "k8s api not available"}

    try:
        dep = _apps_v1.read_namespaced_deployment(name=name, namespace=ns)
    except Exception as e:
        return {"ok": False, "reason": f"deployment not found: {e}"}

    cur_revision = (dep.metadata.annotations or {}).get(
        "deployment.kubernetes.io/revision", "")

    # 列出该 Deployment 关联的所有 ReplicaSet (通过 selector match)
    selector_labels = (dep.spec.selector.match_labels or {})
    if not selector_labels:
        return {"ok": False, "reason": "deployment has no selector match_labels"}
    selector = ",".join(f"{k}={v}" for k, v in selector_labels.items())

    try:
        rs_list = _apps_v1.list_namespaced_replica_set(
            namespace=ns, label_selector=selector).items
    except Exception as e:
        return {"ok": False, "reason": f"list rs failed: {e}"}

    # 解析每个 RS 的 revision 注解, 排序 (越大越新)
    rs_with_rev = []
    for rs in rs_list:
        ann = (rs.metadata.annotations or {})
        rev_str = ann.get("deployment.kubernetes.io/revision", "")
        if not rev_str:
            continue
        try:
            rev = int(rev_str)
        except ValueError:
            continue
        rs_with_rev.append((rev, rs))
    rs_with_rev.sort(key=lambda x: x[0], reverse=True)  # 大→小

    if len(rs_with_rev) < 2:
        return {
            "ok": False,
            "reason": (
                f"rollback needs at least 2 revisions, found {len(rs_with_rev)} "
                f"(deployment may have just been created or revisionHistoryLimit=0)"
            ),
        }

    # 取上一个版本 (倒数第二)
    prev_rev, prev_rs = rs_with_rev[1]
    cur_rev_int = rs_with_rev[0][0]

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "message": (
                f"[DRY-RUN] would rollback deployment {target} "
                f"from revision {cur_rev_int} to {prev_rev} "
                f"(prev image: {_extract_first_image(prev_rs)})"
            ),
            "from_revision": cur_rev_int,
            "to_revision": prev_rev,
        }

    try:
        # 把 prev RS 的 template 拷到 deployment 上 (模拟 kubectl rollout undo)
        new_template = prev_rs.spec.template
        # 必须保持 deployment 自己的 selector + replicas, 只换 template
        body = {"spec": {"template": _serialize_template(new_template)}}
        _apps_v1.patch_namespaced_deployment(name=name, namespace=ns, body=body)
        return {
            "ok": True,
            "message": (
                f"rolled back deployment {target}: "
                f"revision {cur_rev_int} → {prev_rev}"
            ),
            "from_revision": cur_rev_int,
            "to_revision": prev_rev,
        }
    except Exception as e:
        return {"ok": False, "reason": f"rollback failed: {e}"}


def _extract_first_image(rs) -> str:
    """从 RS 的 pod template 提第一个容器的 image (供 dry-run 提示用)"""
    try:
        return rs.spec.template.spec.containers[0].image
    except Exception:
        return "?"


def _serialize_template(template) -> dict:
    """把 V1PodTemplateSpec 转成 patch body 用的 dict.

    kubernetes client 的对象在 patch 时需要转回 dict, 这里走 ApiClient
    的内部 serializer. 同时去掉 metadata.creationTimestamp 这种 server-only 字段.
    """
    from kubernetes.client import ApiClient
    raw = ApiClient().sanitize_for_serialization(template)
    # patch_namespaced_deployment 不允许 templated.metadata.creationTimestamp 出现
    if isinstance(raw, dict):
        meta = raw.get("metadata") or {}
        meta.pop("creationTimestamp", None)
        raw["metadata"] = meta
    return raw


# ============================================================
ALLOWED_ACTIONS = {
    "delete_evicted_pod": delete_evicted_pod,
    "delete_completed_job_pod": delete_completed_job_pod,
    "restart_pod": restart_pod,
    "restart_pod_for_image_pull": restart_pod_for_image_pull,
    "delete_failed_pod": delete_failed_pod,
    # v2.2 新增 L3 (节点级低风险):
    "cordon_node": cordon_node,
    "uncordon_node": uncordon_node,
}

# ============================================================
# L2 灰名单: 人审通过后才允许执行 (CLI approve 触发)
# ============================================================
ALLOWED_L2_ACTIONS = {
    "restart_statefulset_pod": restart_statefulset_pod,
    # v2.2 新增 L2 (Deployment 级中风险, 需人审):
    "scale_deployment": scale_deployment,
    "rollback_deployment": rollback_deployment,
    # TODO 未来扩展: evict_pod / drain_node / patch_resources
}


def is_l3_allowed(action: str) -> bool:
    return action in ALLOWED_ACTIONS


def is_l2_allowed(action: str) -> bool:
    return action in ALLOWED_L2_ACTIONS


def is_action_allowed(action: str) -> bool:
    """L3 自动 或 L2 人审 后允许的全部 action"""
    return action in ALLOWED_ACTIONS or action in ALLOWED_L2_ACTIONS


def execute_action(action: str, target: str, dry_run: bool = True, **kwargs) -> dict:
    """统一执行入口. 同时查 L3 白名单和 L2 灰名单.

    kwargs 透传给具体动作函数, 用于 scale_deployment 这种需要额外参数的:
        execute_action("scale_deployment", "ns/dep", dry_run=False, delta=2)
    """
    fn = ALLOWED_ACTIONS.get(action) or ALLOWED_L2_ACTIONS.get(action)
    if fn is None:
        return {"ok": False, "reason": f"action {action} not in L3/L2 whitelist"}
    return fn(target, dry_run=dry_run, **kwargs)
