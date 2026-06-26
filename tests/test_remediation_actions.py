"""单测: tools.remediation_actions 的 L2/L3 白名单分发 + restart_statefulset_pod.

覆盖 v2.1 引入的:
- L2 灰名单注册表 ALLOWED_L2_ACTIONS (新增 restart_statefulset_pod)
- is_l2_allowed / is_action_allowed 双查
- execute_action 同时查 L3 + L2
- restart_statefulset_pod owner 校验 (仅限 StatefulSet) + dry_run

restart_statefulset_pod 涉及 K8s API, 用 monkeypatch 把 _v1 替换成 Fake.
"""
from types import SimpleNamespace

import pytest

from tools import remediation_actions as ra


# ---------------------------------------------------------------
# 白名单注册表 + dispatch
# ---------------------------------------------------------------
def test_l3_whitelist_contains_known_actions():
    """v2.1 之前的 L3 白名单不应缩水."""
    assert "restart_pod" in ra.ALLOWED_ACTIONS
    assert "delete_evicted_pod" in ra.ALLOWED_ACTIONS
    assert "delete_completed_job_pod" in ra.ALLOWED_ACTIONS
    assert "restart_pod_for_image_pull" in ra.ALLOWED_ACTIONS
    assert "delete_failed_pod" in ra.ALLOWED_ACTIONS


def test_l2_whitelist_has_restart_statefulset_pod():
    """v2.1 新增: StatefulSet 重启走 L2 人审."""
    assert "restart_statefulset_pod" in ra.ALLOWED_L2_ACTIONS


def test_l2_whitelist_does_not_overlap_l3():
    """L2 / L3 不能注册同一个 action (语义冲突)."""
    overlap = set(ra.ALLOWED_ACTIONS) & set(ra.ALLOWED_L2_ACTIONS)
    assert overlap == set()


def test_is_l3_allowed():
    assert ra.is_l3_allowed("restart_pod") is True
    assert ra.is_l3_allowed("restart_statefulset_pod") is False  # 不是 L3
    assert ra.is_l3_allowed("nonexistent_action") is False


def test_is_l2_allowed():
    assert ra.is_l2_allowed("restart_statefulset_pod") is True
    assert ra.is_l2_allowed("restart_pod") is False  # 不是 L2
    assert ra.is_l2_allowed("nonexistent_action") is False


def test_is_action_allowed_covers_both():
    assert ra.is_action_allowed("restart_pod") is True            # L3
    assert ra.is_action_allowed("restart_statefulset_pod") is True  # L2
    assert ra.is_action_allowed("delete_pvc") is False              # L4 黑名单
    assert ra.is_action_allowed("nonexistent_action") is False


def test_execute_action_unknown_action_returns_error():
    result = ra.execute_action("delete_pvc", "ns/pod", dry_run=True)
    assert result["ok"] is False
    assert "not in L3/L2 whitelist" in result["reason"]


# ---------------------------------------------------------------
# restart_statefulset_pod: target 格式 + owner 校验
# ---------------------------------------------------------------
def test_restart_statefulset_pod_invalid_target_format():
    """target 没 '/' 直接拒."""
    result = ra.restart_statefulset_pod("no-slash", dry_run=True)
    assert result["ok"] is False
    assert "invalid target format" in result["reason"]


def test_restart_statefulset_pod_kube_unavailable(monkeypatch):
    """k8s 客户端没起来时优雅失败."""
    monkeypatch.setattr(ra, "_kube_ok", False)
    result = ra.restart_statefulset_pod("ns/pod", dry_run=True)
    assert result["ok"] is False
    assert "k8s api not available" in result["reason"]


class _FakeV1Pod:
    """模拟 kubernetes V1Pod (只保留 owner_references 字段)."""
    def __init__(self, owner_kinds):
        self.metadata = SimpleNamespace(
            owner_references=[SimpleNamespace(kind=k) for k in owner_kinds]
        )


def _install_fake_k8s(monkeypatch, pod_owner_kinds, raise_on_read=False,
                      record_delete=None):
    """临时把 ra._v1 / ra._kube_ok 换成 fake 实现."""
    class _Fake:
        def read_namespaced_pod(self, name, namespace):
            if raise_on_read:
                raise RuntimeError("pod not found in fake cluster")
            return _FakeV1Pod(pod_owner_kinds)

        def delete_namespaced_pod(self, name, namespace):
            if record_delete is not None:
                record_delete.append((namespace, name))
            return None

    monkeypatch.setattr(ra, "_v1", _Fake())
    monkeypatch.setattr(ra, "_kube_ok", True)


def test_restart_statefulset_pod_pod_not_found(monkeypatch):
    _install_fake_k8s(monkeypatch, [], raise_on_read=True)
    result = ra.restart_statefulset_pod("ns/missing", dry_run=True)
    assert result["ok"] is False
    assert "pod not found" in result["reason"]


def test_restart_statefulset_pod_no_owner_refused(monkeypatch):
    """裸 Pod 没有 owner, 不让走这条路径 (与 R1 一致)."""
    _install_fake_k8s(monkeypatch, [])
    result = ra.restart_statefulset_pod("ns/bare", dry_run=True)
    assert result["ok"] is False
    assert "no owner" in result["reason"]


def test_restart_statefulset_pod_replicaset_refused(monkeypatch):
    """ReplicaSet 应走 restart_pod (L3), 这里要明确拒绝."""
    _install_fake_k8s(monkeypatch, ["ReplicaSet"])
    result = ra.restart_statefulset_pod("ns/rs-pod", dry_run=True)
    assert result["ok"] is False
    assert "only for StatefulSet pods" in result["reason"]


def test_restart_statefulset_pod_daemonset_refused(monkeypatch):
    _install_fake_k8s(monkeypatch, ["DaemonSet"])
    result = ra.restart_statefulset_pod("ns/ds-pod", dry_run=True)
    assert result["ok"] is False
    assert "only for StatefulSet pods" in result["reason"]


def test_restart_statefulset_pod_dry_run_does_not_delete(monkeypatch):
    """dry_run=True 不调用 delete_namespaced_pod."""
    deletes = []
    _install_fake_k8s(monkeypatch, ["StatefulSet"], record_delete=deletes)
    result = ra.restart_statefulset_pod("prod/redis-0", dry_run=True)
    assert result["ok"] is True
    assert result.get("dry_run") is True
    assert "DRY-RUN" in result["message"]
    assert deletes == []  # 关键: 没真删


def test_restart_statefulset_pod_real_delete(monkeypatch):
    """dry_run=False 真删, 记录 ns/name."""
    deletes = []
    _install_fake_k8s(monkeypatch, ["StatefulSet"], record_delete=deletes)
    result = ra.restart_statefulset_pod("prod/redis-0", dry_run=False)
    assert result["ok"] is True
    assert "dry_run" not in result
    assert deletes == [("prod", "redis-0")]


# ---------------------------------------------------------------
# execute_action: 同一入口同时调度 L3 + L2
# ---------------------------------------------------------------
def test_execute_action_routes_to_l2_handler(monkeypatch):
    """execute_action 调 restart_statefulset_pod (L2) 应能走通 dry_run 路径."""
    _install_fake_k8s(monkeypatch, ["StatefulSet"])
    result = ra.execute_action(
        "restart_statefulset_pod", "prod/redis-0", dry_run=True)
    assert result["ok"] is True
    assert result.get("dry_run") is True


def test_execute_action_routes_to_l3_handler(monkeypatch):
    """execute_action 调 restart_pod (L3) 应能走通 dry_run 路径."""
    _install_fake_k8s(monkeypatch, ["ReplicaSet"])
    result = ra.execute_action(
        "restart_pod", "default/my-app-rs-abc", dry_run=True)
    assert result["ok"] is True
    assert result.get("dry_run") is True
