"""单测: tools.remediation_actions 的 v2.2 新动作.

覆盖:
- cordon_node / uncordon_node (L3): node target 解析 + 幂等 + dry_run + 真执行
- scale_deployment (L2): replicas/delta 模式 + 安全边界 + dry_run + 真 patch
- rollback_deployment (L2): 至少 2 个 revision + 取上版本 + dry_run

全部用 monkeypatch 替 _v1 / _apps_v1, 不连真集群.
"""
from types import SimpleNamespace

import pytest

from tools import remediation_actions as ra


# ===============================================================
# Fake K8s client 工具
# ===============================================================
class _FakeNode:
    def __init__(self, unschedulable=False):
        self.spec = SimpleNamespace(unschedulable=unschedulable)


class _FakeDep:
    def __init__(self, name="my-dep", ns="default", replicas=3,
                 revision="5", selector=None):
        ann = {"deployment.kubernetes.io/revision": revision}
        self.metadata = SimpleNamespace(annotations=ann, name=name, namespace=ns)
        match = selector or {"app": name}
        self.spec = SimpleNamespace(
            replicas=replicas,
            selector=SimpleNamespace(match_labels=match),
            template=SimpleNamespace(
                spec=SimpleNamespace(containers=[SimpleNamespace(image=f"img:v{revision}")])
            ),
        )


class _FakeRS:
    def __init__(self, revision, image):
        ann = {"deployment.kubernetes.io/revision": str(revision)}
        self.metadata = SimpleNamespace(annotations=ann)
        self.spec = SimpleNamespace(template=SimpleNamespace(
            spec=SimpleNamespace(containers=[SimpleNamespace(image=image)]),
            metadata=SimpleNamespace(),  # patch_template 需要
        ))


def _install_core(monkeypatch, node=None, raise_on_node=False, record_patch=None):
    """把 ra._v1 替换成 fake (CoreV1Api)."""
    class _FakeV1:
        def read_node(self, name):
            if raise_on_node:
                raise RuntimeError("node not found")
            return node or _FakeNode()
        def patch_node(self, name, body):
            if record_patch is not None:
                record_patch.append(("node", name, body))
            return None
    monkeypatch.setattr(ra, "_v1", _FakeV1())
    monkeypatch.setattr(ra, "_kube_ok", True)


def _install_apps(monkeypatch, dep=None, raise_on_dep=False,
                  rs_list=None, record_patch=None):
    """把 ra._apps_v1 替换成 fake (AppsV1Api)."""
    class _FakeAppsV1:
        def read_namespaced_deployment(self, name, namespace):
            if raise_on_dep:
                raise RuntimeError("deployment not found")
            return dep or _FakeDep(name=name, ns=namespace)
        def list_namespaced_replica_set(self, namespace, label_selector=None):
            return SimpleNamespace(items=rs_list or [])
        def patch_namespaced_deployment_scale(self, name, namespace, body):
            if record_patch is not None:
                record_patch.append(("scale", namespace, name, body))
            return None
        def patch_namespaced_deployment(self, name, namespace, body):
            if record_patch is not None:
                record_patch.append(("template", namespace, name, body))
            return None
    monkeypatch.setattr(ra, "_apps_v1", _FakeAppsV1())
    monkeypatch.setattr(ra, "_kube_ok", True)


# ===============================================================
# _normalize_node_target
# ===============================================================
def test_normalize_node_target_plain():
    assert ra._normalize_node_target("192.168.48.78") == "192.168.48.78"


def test_normalize_node_target_node_prefix():
    assert ra._normalize_node_target("node/192.168.48.78") == "192.168.48.78"


def test_normalize_node_target_leading_slash():
    assert ra._normalize_node_target("/192.168.48.78") == "192.168.48.78"


def test_normalize_node_target_empty():
    assert ra._normalize_node_target("") is None
    assert ra._normalize_node_target(None) is None


# ===============================================================
# cordon_node
# ===============================================================
def test_cordon_node_dry_run(monkeypatch):
    _install_core(monkeypatch, node=_FakeNode(unschedulable=False))
    result = ra.cordon_node("192.168.48.78", dry_run=True)
    assert result["ok"] is True
    assert result.get("dry_run") is True
    assert "DRY-RUN" in result["message"]


def test_cordon_node_real(monkeypatch):
    patches = []
    _install_core(monkeypatch, node=_FakeNode(unschedulable=False),
                  record_patch=patches)
    result = ra.cordon_node("192.168.48.78", dry_run=False)
    assert result["ok"] is True
    assert patches == [("node", "192.168.48.78", {"spec": {"unschedulable": True}})]


def test_cordon_node_idempotent(monkeypatch):
    """已经 cordon 过的节点再 cordon 不该报错, 也不该重复 patch."""
    patches = []
    _install_core(monkeypatch, node=_FakeNode(unschedulable=True),
                  record_patch=patches)
    result = ra.cordon_node("192.168.48.78", dry_run=False)
    assert result["ok"] is True
    assert result.get("already_cordoned") is True
    assert patches == []  # 没有真的 patch


def test_cordon_node_not_found(monkeypatch):
    _install_core(monkeypatch, raise_on_node=True)
    result = ra.cordon_node("missing-node", dry_run=True)
    assert result["ok"] is False
    assert "node not found" in result["reason"]


def test_cordon_node_invalid_target(monkeypatch):
    _install_core(monkeypatch)
    result = ra.cordon_node("", dry_run=True)
    assert result["ok"] is False
    assert "invalid node target" in result["reason"]


def test_cordon_node_kube_unavailable(monkeypatch):
    monkeypatch.setattr(ra, "_kube_ok", False)
    result = ra.cordon_node("192.168.48.78", dry_run=True)
    assert result["ok"] is False
    assert "k8s api not available" in result["reason"]


# ===============================================================
# uncordon_node
# ===============================================================
def test_uncordon_node_dry_run(monkeypatch):
    _install_core(monkeypatch, node=_FakeNode(unschedulable=True))
    result = ra.uncordon_node("192.168.48.78", dry_run=True)
    assert result["ok"] is True
    assert result.get("dry_run") is True


def test_uncordon_node_real(monkeypatch):
    patches = []
    _install_core(monkeypatch, node=_FakeNode(unschedulable=True),
                  record_patch=patches)
    result = ra.uncordon_node("192.168.48.78", dry_run=False)
    assert result["ok"] is True
    assert patches == [("node", "192.168.48.78", {"spec": {"unschedulable": False}})]


def test_uncordon_already_schedulable_idempotent(monkeypatch):
    """节点本来就可调度, uncordon 是 no-op."""
    patches = []
    _install_core(monkeypatch, node=_FakeNode(unschedulable=False),
                  record_patch=patches)
    result = ra.uncordon_node("192.168.48.78", dry_run=False)
    assert result["ok"] is True
    assert result.get("already_schedulable") is True
    assert patches == []


# ===============================================================
# scale_deployment
# ===============================================================
def test_scale_deployment_must_specify_replicas_or_delta(monkeypatch):
    _install_apps(monkeypatch)
    result = ra.scale_deployment("default/my-dep", dry_run=True)
    assert result["ok"] is False
    assert "must specify" in result["reason"]


def test_scale_deployment_replicas_and_delta_mutually_exclusive(monkeypatch):
    _install_apps(monkeypatch)
    result = ra.scale_deployment("default/my-dep", dry_run=True,
                                  replicas=5, delta=2)
    assert result["ok"] is False
    assert "mutually exclusive" in result["reason"]


def test_scale_deployment_delta_dry_run(monkeypatch):
    _install_apps(monkeypatch, dep=_FakeDep(replicas=3))
    result = ra.scale_deployment("default/my-dep", dry_run=True, delta=2)
    assert result["ok"] is True
    assert result["before"] == 3
    assert result["after"] == 5
    assert "DRY-RUN" in result["message"]


def test_scale_deployment_replicas_dry_run(monkeypatch):
    _install_apps(monkeypatch, dep=_FakeDep(replicas=3))
    result = ra.scale_deployment("default/my-dep", dry_run=True, replicas=10)
    assert result["ok"] is True
    assert result["before"] == 3
    assert result["after"] == 10


def test_scale_deployment_real_patch(monkeypatch):
    patches = []
    _install_apps(monkeypatch, dep=_FakeDep(replicas=3), record_patch=patches)
    result = ra.scale_deployment("default/my-dep", dry_run=False, delta=2)
    assert result["ok"] is True
    assert result["after"] == 5
    assert patches == [("scale", "default", "my-dep", {"spec": {"replicas": 5}})]


def test_scale_deployment_delta_exceeds_max(monkeypatch):
    """SCALE_MAX_DELTA=5, ±6 应被拒绝."""
    _install_apps(monkeypatch, dep=_FakeDep(replicas=3))
    result = ra.scale_deployment("default/my-dep", dry_run=True, delta=10)
    assert result["ok"] is False
    assert "SCALE_MAX_DELTA" in result["reason"]


def test_scale_deployment_replicas_exceeds_max(monkeypatch):
    """绝对值 replicas=999 应被拒绝."""
    _install_apps(monkeypatch, dep=_FakeDep(replicas=3))
    result = ra.scale_deployment("default/my-dep", dry_run=True, replicas=999)
    assert result["ok"] is False
    assert "SCALE_REPLICAS_MAX" in result["reason"]


def test_scale_deployment_negative_replicas(monkeypatch):
    _install_apps(monkeypatch, dep=_FakeDep(replicas=3))
    result = ra.scale_deployment("default/my-dep", dry_run=True, delta=-5)
    assert result["ok"] is False
    assert "negative" in result["reason"]


def test_scale_deployment_no_change_is_noop(monkeypatch):
    """delta=0 当前 replicas 已经满足, no-op."""
    patches = []
    _install_apps(monkeypatch, dep=_FakeDep(replicas=5), record_patch=patches)
    result = ra.scale_deployment("default/my-dep", dry_run=False, replicas=5)
    assert result["ok"] is True
    assert result.get("no_op") is True
    assert patches == []


def test_scale_deployment_dep_not_found(monkeypatch):
    _install_apps(monkeypatch, raise_on_dep=True)
    result = ra.scale_deployment("default/missing", dry_run=True, delta=1)
    assert result["ok"] is False
    assert "deployment not found" in result["reason"]


def test_scale_deployment_invalid_target(monkeypatch):
    _install_apps(monkeypatch)
    result = ra.scale_deployment("no-slash", dry_run=True, delta=1)
    assert result["ok"] is False
    assert "invalid target format" in result["reason"]


# ===============================================================
# rollback_deployment
# ===============================================================
def test_rollback_dry_run(monkeypatch):
    """deployment 有 2 个 revision, dry-run 显示要回到上一个."""
    rs_list = [
        _FakeRS(revision=5, image="img:v5"),
        _FakeRS(revision=4, image="img:v4"),
    ]
    _install_apps(monkeypatch, dep=_FakeDep(revision="5"), rs_list=rs_list)
    result = ra.rollback_deployment("default/my-dep", dry_run=True)
    assert result["ok"] is True
    assert result["from_revision"] == 5
    assert result["to_revision"] == 4
    assert "DRY-RUN" in result["message"]


def test_rollback_picks_latest_two(monkeypatch):
    """3 个 revision, 应该从 7 → 6 (倒数第二), 而不是 → 5."""
    rs_list = [
        _FakeRS(revision=5, image="img:v5"),
        _FakeRS(revision=7, image="img:v7"),  # 故意乱序
        _FakeRS(revision=6, image="img:v6"),
    ]
    _install_apps(monkeypatch, dep=_FakeDep(revision="7"), rs_list=rs_list)
    result = ra.rollback_deployment("default/my-dep", dry_run=True)
    assert result["from_revision"] == 7
    assert result["to_revision"] == 6


def test_rollback_only_one_revision_fails(monkeypatch):
    """只有 1 个 revision (新创建) 不能回滚."""
    rs_list = [_FakeRS(revision=1, image="img:v1")]
    _install_apps(monkeypatch, dep=_FakeDep(revision="1"), rs_list=rs_list)
    result = ra.rollback_deployment("default/my-dep", dry_run=True)
    assert result["ok"] is False
    assert "at least 2 revisions" in result["reason"]


def test_rollback_no_revisions_fails(monkeypatch):
    """没拿到任何 revision 注解."""
    _install_apps(monkeypatch, dep=_FakeDep(revision="3"), rs_list=[])
    result = ra.rollback_deployment("default/my-dep", dry_run=True)
    assert result["ok"] is False


def test_rollback_dep_not_found(monkeypatch):
    _install_apps(monkeypatch, raise_on_dep=True)
    result = ra.rollback_deployment("default/missing", dry_run=True)
    assert result["ok"] is False
    assert "deployment not found" in result["reason"]


def test_rollback_invalid_target(monkeypatch):
    _install_apps(monkeypatch)
    result = ra.rollback_deployment("no-slash", dry_run=True)
    assert result["ok"] is False
    assert "invalid target format" in result["reason"]


# ===============================================================
# 注册表 / 双查 / dispatch
# ===============================================================
def test_new_actions_in_registries():
    """v2.2 新增的 5 个 action 必须出现在注册表里."""
    # L3
    assert "cordon_node" in ra.ALLOWED_ACTIONS
    assert "uncordon_node" in ra.ALLOWED_ACTIONS
    # L2
    assert "scale_deployment" in ra.ALLOWED_L2_ACTIONS
    assert "rollback_deployment" in ra.ALLOWED_L2_ACTIONS


def test_l2_l3_no_overlap_after_v22():
    overlap = set(ra.ALLOWED_ACTIONS) & set(ra.ALLOWED_L2_ACTIONS)
    assert overlap == set()


def test_execute_action_routes_cordon(monkeypatch):
    _install_core(monkeypatch, node=_FakeNode(unschedulable=False))
    result = ra.execute_action("cordon_node", "192.168.48.78", dry_run=True)
    assert result["ok"] is True
    assert result.get("dry_run") is True


def test_execute_action_routes_scale_with_kwargs(monkeypatch):
    """execute_action 透传 kwargs 给 scale_deployment."""
    _install_apps(monkeypatch, dep=_FakeDep(replicas=3))
    result = ra.execute_action(
        "scale_deployment", "default/my-dep", dry_run=True, delta=2)
    assert result["ok"] is True
    assert result["after"] == 5


def test_execute_action_routes_rollback(monkeypatch):
    rs_list = [
        _FakeRS(revision=2, image="img:v2"),
        _FakeRS(revision=1, image="img:v1"),
    ]
    _install_apps(monkeypatch, dep=_FakeDep(revision="2"), rs_list=rs_list)
    result = ra.execute_action(
        "rollback_deployment", "default/my-dep", dry_run=True)
    assert result["ok"] is True
    assert result["to_revision"] == 1
