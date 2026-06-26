"""单测: agents.approval_gate._validate_target_in_alerts

target sanity check 是 v2.1 引入的抵御 LLM 幻觉的最后防线:
LLM 偶尔会编造 "default/pod-name-1234567890abcdef" 这种和告警无关的 target,
一旦放过去执行就是无差别误删. 这里强制 plan.target 必须出现在 raw_alerts 里.
"""
from agents.approval_gate import _validate_target_in_alerts


def _alert(ns, pod, **labels):
    """快捷构造一个 alert dict."""
    base = {"namespace": ns, "instance": pod}
    base.update(labels)
    return {"labels": base}


# ---------------------------------------------------------------
# 命中 (target 与告警一致)
# ---------------------------------------------------------------
def test_target_matches_alert():
    plan = {"target": "default/my-pod-abc"}
    alerts = [_alert("default", "my-pod-abc")]
    ok, reason = _validate_target_in_alerts(plan, alerts)
    assert ok is True
    assert reason == ""


def test_target_matches_one_of_many():
    plan = {"target": "kube-system/coredns-2"}
    alerts = [
        _alert("default", "my-pod"),
        _alert("kube-system", "coredns-1"),
        _alert("kube-system", "coredns-2"),
    ]
    ok, reason = _validate_target_in_alerts(plan, alerts)
    assert ok is True


# ---------------------------------------------------------------
# 不命中 (LLM 幻觉编 target)
# ---------------------------------------------------------------
def test_target_not_in_alerts_rejected():
    """LLM 编了一个完全不存在的 pod 名."""
    plan = {"target": "default/hallucinated-pod-xyz"}
    alerts = [_alert("default", "real-pod-123")]
    ok, reason = _validate_target_in_alerts(plan, alerts)
    assert ok is False
    assert "不在告警 Pod 列表" in reason


def test_target_namespace_mismatch_rejected():
    """同名 pod 但 namespace 不同, 也算不命中."""
    plan = {"target": "wrong-ns/my-pod-abc"}
    alerts = [_alert("default", "my-pod-abc")]
    ok, reason = _validate_target_in_alerts(plan, alerts)
    assert ok is False


# ---------------------------------------------------------------
# target 格式错
# ---------------------------------------------------------------
def test_empty_target_rejected():
    ok, reason = _validate_target_in_alerts({"target": ""}, [_alert("a", "b")])
    assert ok is False
    # v2.2 起空 target 提前 return, 文案是 "target 为空" (v2.1 是 "target 为空或格式错")
    assert "target 为空" in reason


def test_missing_target_key_rejected():
    """plan 没 target 字段."""
    ok, reason = _validate_target_in_alerts({}, [_alert("a", "b")])
    assert ok is False


def test_target_without_slash_rejected():
    """target 必须含 '/' 分隔 namespace 和 pod."""
    ok, reason = _validate_target_in_alerts(
        {"target": "no-slash-here"}, [_alert("a", "b")])
    assert ok is False


def test_target_whitespace_stripped():
    """前后空格被 .strip() 容忍."""
    plan = {"target": "  default/my-pod  "}
    alerts = [_alert("default", "my-pod")]
    ok, reason = _validate_target_in_alerts(plan, alerts)
    assert ok is True


# ---------------------------------------------------------------
# alerts 异常
# ---------------------------------------------------------------
def test_empty_alerts_rejected():
    """没有原始告警就无从校验, 安全起见拒绝."""
    ok, reason = _validate_target_in_alerts({"target": "ns/pod"}, [])
    assert ok is False
    assert "无法校验" in reason


def test_alerts_without_labels_rejected():
    """alert 里没有有效 ns/pod 字段."""
    ok, reason = _validate_target_in_alerts(
        {"target": "ns/pod"}, [{"labels": {}}, {"foo": "bar"}])
    assert ok is False


def test_non_dict_alerts_skipped():
    """alerts 里混进非 dict 元素时不该 crash."""
    plan = {"target": "default/my-pod"}
    alerts = ["string-not-dict", None, 123, _alert("default", "my-pod")]
    ok, reason = _validate_target_in_alerts(plan, alerts)
    assert ok is True


def test_alert_top_level_namespace_instance_supported():
    """兼容旧格式: ns/instance 在 alert 顶层而非 labels 里."""
    plan = {"target": "default/my-pod"}
    alerts = [{"namespace": "default", "instance": "my-pod"}]
    ok, reason = _validate_target_in_alerts(plan, alerts)
    assert ok is True


def test_none_target_value_rejected():
    """plan.target 显式为 None."""
    ok, reason = _validate_target_in_alerts(
        {"target": None}, [_alert("a", "b")])
    assert ok is False
