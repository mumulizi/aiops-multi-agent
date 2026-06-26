"""单测: agents.approval_gate._validate_target_in_alerts 的 v2.2 扩展.

v2.2 新动作的 target 形态:
- cordon_node / uncordon_node: target 是 node 名 (无 namespace)
  → 校验 node 必须在告警 .labels.node 列表
- scale_deployment / rollback_deployment: target 是 "ns/deployment-name"
  → 仅校验 namespace 出自告警 (deployment 名告警里没有, 只能信任 LLM)

Pod 级 action 的校验逻辑保持不变 (test_approval_target_check.py 已覆盖).
"""
from agents.approval_gate import _validate_target_in_alerts


def _alert(ns="default", pod=None, node=None, **labels):
    base = {"namespace": ns}
    if pod:
        base["instance"] = pod
    if node:
        base["node"] = node
    base.update(labels)
    return {"labels": base}


# ============================================================
# cordon_node / uncordon_node: node 维度校验
# ============================================================
def test_cordon_node_target_in_alerts():
    plan = {"action": "cordon_node", "target": "192.168.48.78"}
    alerts = [_alert(node="192.168.48.78", pod="some-pod")]
    ok, reason = _validate_target_in_alerts(plan, alerts)
    assert ok is True


def test_cordon_node_target_with_node_prefix():
    """兼容 'node/192.168.48.78' 格式."""
    plan = {"action": "cordon_node", "target": "node/192.168.48.78"}
    alerts = [_alert(node="192.168.48.78", pod="some-pod")]
    ok, reason = _validate_target_in_alerts(plan, alerts)
    assert ok is True


def test_cordon_node_target_not_in_alerts_rejected():
    """LLM 编了一个完全不存在的 node."""
    plan = {"action": "cordon_node", "target": "wrong-node"}
    alerts = [_alert(node="192.168.48.78", pod="some-pod")]
    ok, reason = _validate_target_in_alerts(plan, alerts)
    assert ok is False
    assert "不在告警节点列表" in reason


def test_cordon_node_no_node_in_alerts_rejected():
    """告警里压根没 node 字段, 拒绝."""
    plan = {"action": "cordon_node", "target": "192.168.48.78"}
    alerts = [_alert(pod="some-pod")]  # 没 node
    ok, reason = _validate_target_in_alerts(plan, alerts)
    assert ok is False
    assert "无法校验 cordon target" in reason


def test_uncordon_node_uses_same_path():
    """uncordon_node 走同一套校验逻辑."""
    plan = {"action": "uncordon_node", "target": "192.168.48.78"}
    alerts = [_alert(node="192.168.48.78", pod="some-pod")]
    ok, reason = _validate_target_in_alerts(plan, alerts)
    assert ok is True


# ============================================================
# scale_deployment / rollback_deployment: namespace 校验
# ============================================================
def test_scale_deployment_target_ns_in_alerts():
    """ns 在告警里出现过, deployment 名不强制校验."""
    plan = {"action": "scale_deployment",
            "target": "production/my-app",
            "extra": {"delta": 2}}
    alerts = [_alert(ns="production", pod="my-app-abc-123")]
    ok, reason = _validate_target_in_alerts(plan, alerts)
    assert ok is True


def test_scale_deployment_ns_not_in_alerts_rejected():
    plan = {"action": "scale_deployment", "target": "wrong-ns/my-app"}
    alerts = [_alert(ns="default", pod="x")]
    ok, reason = _validate_target_in_alerts(plan, alerts)
    assert ok is False
    assert "namespace" in reason


def test_scale_deployment_target_format_error():
    """target 没 '/' 直接拒."""
    plan = {"action": "scale_deployment", "target": "no-slash"}
    alerts = [_alert(ns="default", pod="x")]
    ok, reason = _validate_target_in_alerts(plan, alerts)
    assert ok is False
    assert "namespace/deployment-name" in reason


def test_scale_deployment_empty_dep_name():
    plan = {"action": "scale_deployment", "target": "default/"}
    alerts = [_alert(ns="default", pod="x")]
    ok, reason = _validate_target_in_alerts(plan, alerts)
    assert ok is False


def test_rollback_deployment_uses_same_path():
    plan = {"action": "rollback_deployment", "target": "default/my-app"}
    alerts = [_alert(ns="default", pod="my-app-abc")]
    ok, reason = _validate_target_in_alerts(plan, alerts)
    assert ok is True


# ============================================================
# Pod 级 action 仍走老路径 (回归测试)
# ============================================================
def test_restart_pod_still_validates_pod_level():
    """restart_pod 必须 ns/pod 都命中, 不是 ns 级粗校验."""
    plan = {"action": "restart_pod", "target": "default/my-app-abc"}
    alerts = [_alert(ns="default", pod="my-app-abc")]
    ok, reason = _validate_target_in_alerts(plan, alerts)
    assert ok is True


def test_restart_pod_pod_name_mismatch_rejected():
    """ns 对但 pod 名不对, 拒绝 (Pod 级严格校验)."""
    plan = {"action": "restart_pod", "target": "default/wrong-pod"}
    alerts = [_alert(ns="default", pod="my-app-abc")]
    ok, reason = _validate_target_in_alerts(plan, alerts)
    assert ok is False
    assert "不在告警 Pod 列表" in reason


def test_empty_target_rejected():
    """所有 action 类型, 空 target 都拒绝."""
    for action in ["restart_pod", "cordon_node", "scale_deployment"]:
        plan = {"action": action, "target": ""}
        ok, reason = _validate_target_in_alerts(plan, [_alert(ns="x", pod="y")])
        assert ok is False
        assert "target 为空" in reason
