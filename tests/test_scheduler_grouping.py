"""单测: v2.4 main_inspect 调度器排序 + 分级诊断.

覆盖:
- _group_similar_issues 排序: 卡死状态 (ImagePullError 等) 排在同 severity 前面
- 关键回归: 7 个 ImagePullError 不该被 fluid-system Unhealthy 挤掉
"""
import sys
from pathlib import Path

# 把项目根目录加入 sys.path, 因为 main_inspect.py 在根目录而不是包里
sys.path.insert(0, str(Path(__file__).parent.parent))

from main_inspect import _group_similar_issues


def _issue(ns, pod, type_, severity, restarts=0, reason=None, owner_kind="ReplicaSet"):
    return {
        "namespace": ns,
        "pod": pod,
        "type": type_,
        "severity": severity,
        "restarts": restarts,
        "reason": reason or type_,
        "owner_kind": owner_kind,
    }


# ===============================================================
# 排序: critical 优先
# ===============================================================
def test_critical_before_high():
    """critical 始终排在 high 前面, 不受 restart 数影响."""
    issues = [
        _issue("a", "p1", "Unhealthy", "high", restarts=1000),
        _issue("b", "p2", "CrashLoopBackOff", "critical", restarts=10),
    ]
    out = _group_similar_issues(issues)
    assert out[0][0]["severity"] == "critical"
    assert out[1][0]["severity"] == "high"


# ===============================================================
# 核心: 同 severity 时, 卡死状态优先 (修复用户反馈的 bug)
# ===============================================================
def test_imagepull_priority_over_high_restart_unhealthy():
    """实战 case: abcd/ImagePullError (restart=0, high) 应排在
    fluid-system/Unhealthy (restart=666, high) 前面.
    """
    issues = (
        # fluid-system 的 5 个 Unhealthy Pod, 重启 666 次
        [_issue("fluid-system", f"test-pvc-{i}", "Unhealthy", "high",
                restarts=666, reason="Completed") for i in range(5)]
        # abcd 的 7 个 ImagePullError Pod, 重启 0
        + [_issue("abcd", f"probe-agent-{i}", "ImagePullError", "high",
                  restarts=0, reason="ImagePullBackOff") for i in range(7)]
    )
    out = _group_similar_issues(issues)
    # 第一组应该是 ImagePullError (卡死状态优先)
    assert out[0][0]["type"] == "ImagePullError"
    assert out[0][0]["namespace"] == "abcd"
    # 第二组才是 Unhealthy
    assert out[1][0]["type"] == "Unhealthy"


def test_image_pull_keyword_in_reason_also_stuck():
    """reason 字段 (而不是 type) 含 imagepull 关键词也算卡死."""
    issues = [
        _issue("a", "p1", "Unhealthy", "high", restarts=500,
               reason="ContainersNotReady"),
        _issue("b", "p2", "Pending", "high", restarts=0,
               reason="ImagePullBackOff: registry timeout"),
    ]
    out = _group_similar_issues(issues)
    # 第二个 (含 ImagePullBackOff reason) 排前
    assert out[0][0]["pod"] == "p2"


def test_run_container_error_is_stuck():
    issues = [
        _issue("a", "p1", "CrashLoopBackOff", "high", restarts=100),
        _issue("b", "p2", "RunContainerError", "high", restarts=5,
               reason="RunContainerError"),
    ]
    out = _group_similar_issues(issues)
    # RunContainerError 排前 (虽然 restart 少)
    assert out[0][0]["type"] == "RunContainerError"


def test_create_container_config_error_is_stuck():
    issues = [
        _issue("a", "p1", "CrashLoopBackOff", "high", restarts=100),
        _issue("b", "p2", "Pending", "high", restarts=0,
               reason="CreateContainerConfigError"),
    ]
    out = _group_similar_issues(issues)
    assert out[0][0]["pod"] == "p2"


# ===============================================================
# 同 severity + 都不卡死时, 按 restart 数排序
# ===============================================================
def test_high_restart_first_when_neither_stuck():
    issues = [
        _issue("a", "p1", "OOMKilled", "high", restarts=10),
        _issue("b", "p2", "OOMKilled", "high", restarts=1000),
    ]
    out = _group_similar_issues(issues)
    assert out[0][0]["restarts"] == 1000


def test_same_severity_both_stuck_high_restart_first():
    """两个都卡死时, 仍按 restart 数排."""
    issues = [
        _issue("a", "p1", "ImagePullError", "high", restarts=0),
        _issue("b", "p2", "ImagePullError", "high", restarts=10),
    ]
    out = _group_similar_issues(issues)
    # 注意这两个 ns 不同, 不会归一组. restart 多的排前
    assert out[0][0]["restarts"] == 10


# ===============================================================
# 同类去重: 同 (ns, type, service_prefix) 归一组 (v2.5)
# ===============================================================
def test_same_ns_type_and_prefix_grouped():
    """同 ns + 同 type + 同 service_prefix → 归一组."""
    issues = [
        _issue("ns-a", "my-app-rs-aaa", "OOM", "high", restarts=100,
               owner_kind="ReplicaSet"),
        _issue("ns-a", "my-app-rs-bbb", "OOM", "high", restarts=200,
               owner_kind="ReplicaSet"),
        _issue("ns-a", "my-app-rs-ccc", "OOM", "high", restarts=300,
               owner_kind="ReplicaSet"),
    ]
    out = _group_similar_issues(issues)
    assert len(out) == 1
    rep, members = out[0]
    # 代表是 restart 最多的
    assert rep["pod"] == "my-app-rs-ccc"
    assert set(members) == {"my-app-rs-aaa", "my-app-rs-bbb", "my-app-rs-ccc"}


def test_same_ns_type_diff_prefix_NOT_grouped():
    """v2.5 关键修复: 同 ns + 同 type 但不同服务 → 分到不同组.

    实战 case: kube-system 下 kube-external-auditor / dcgm-exporter /
    device-plugin-patch 都是 CrashLoopBackOff, 但是 3 个不同的服务,
    不能合并 (否则 dcgm-exporter 会被错误地套上 -kubeConfig 的诊断).
    """
    issues = [
        _issue("kube-system", "kube-external-auditor-192.168.48.78",
               "CrashLoopBackOff", "critical", restarts=32000,
               owner_kind="BarePod"),
        _issue("kube-system", "kube-external-auditor-192.168.48.51",
               "CrashLoopBackOff", "critical", restarts=32000,
               owner_kind="BarePod"),
        _issue("kube-system", "dcgm-exporter-75h9v",
               "CrashLoopBackOff", "critical", restarts=2254,
               owner_kind="DaemonSet"),
        _issue("kube-system", "device-plugin-patch-5vw58",
               "CrashLoopBackOff", "critical", restarts=2253,
               owner_kind="DaemonSet"),
    ]
    out = _group_similar_issues(issues)
    # 3 个不同服务 → 3 组
    assert len(out) == 3
    # 每组的代表 service_prefix 应该独立
    prefixes = sorted(set(g[0]["pod"].rsplit("-", 1)[0]
                          if g[0].get("owner_kind") != "ReplicaSet"
                          else g[0]["pod"].rsplit("-", 2)[0]
                          for g in out))
    assert "kube-external-auditor" in str(prefixes) or any(
        "kube-external-auditor" in g[0]["pod"] for g in out)
    # dcgm-exporter 自己一组
    dcgm_group = [g for g in out if "dcgm" in g[0]["pod"]]
    assert len(dcgm_group) == 1
    assert dcgm_group[0][0]["pod"] == "dcgm-exporter-75h9v"
    # device-plugin-patch 自己一组
    dpp_group = [g for g in out if "device-plugin" in g[0]["pod"]]
    assert len(dpp_group) == 1


def test_replicaset_prefix_drops_two_hash_segments():
    """ReplicaSet Pod: <deployment>-<rs-hash>-<pod-hash>, 去最后 2 段."""
    from main_inspect import _service_prefix
    assert _service_prefix(
        "baremetal-operator-controller-manager-7466749c9f-q98kw",
        "ReplicaSet"
    ) == "baremetal-operator-controller-manager"


def test_daemonset_prefix_drops_one_segment():
    """DaemonSet Pod: <ds-name>-<hash>, 去最后 1 段."""
    from main_inspect import _service_prefix
    assert _service_prefix("dcgm-exporter-75h9v", "DaemonSet") == "dcgm-exporter"
    assert _service_prefix("abcd-abcd-probe-agent-77szl",
                            "DaemonSet") == "abcd-abcd-probe-agent"


def test_statefulset_prefix_drops_ordinal():
    """StatefulSet Pod: <sts-name>-<ordinal>, 去最后 1 段."""
    from main_inspect import _service_prefix
    assert _service_prefix("abcd-abcd-task-manager-0",
                            "StatefulSet") == "abcd-abcd-task-manager"


def test_barepod_prefix_drops_one_segment():
    """BarePod (含静态 Pod): 去最后 1 段."""
    from main_inspect import _service_prefix
    # 静态 Pod 命名: <service>-<node-ip>, IP 整体当一段 (因为不含 dash)
    assert _service_prefix("kube-external-auditor-192.168.48.78",
                            "BarePod") == "kube-external-auditor"


def test_replicaset_two_segments_only_returns_full_name():
    """边界: ReplicaSet pod 名只有 2 段时返回完整名 (避免空字符串)."""
    from main_inspect import _service_prefix
    assert _service_prefix("foo-bar", "ReplicaSet") == "foo-bar"


def test_single_segment_pod_name():
    """单段 pod 名兜底."""
    from main_inspect import _service_prefix
    assert _service_prefix("standalone", "BarePod") == "standalone"


def test_diff_ns_not_grouped():
    issues = [
        _issue("ns-a", "p1", "OOM", "high", restarts=100),
        _issue("ns-b", "p1", "OOM", "high", restarts=100),
    ]
    out = _group_similar_issues(issues)
    assert len(out) == 2


def test_diff_type_not_grouped():
    issues = [
        _issue("ns-a", "p1", "OOM", "high", restarts=100),
        _issue("ns-a", "p2", "CrashLoopBackOff", "high", restarts=100),
    ]
    out = _group_similar_issues(issues)
    assert len(out) == 2


# ===============================================================
# 边界
# ===============================================================
def test_empty_input():
    assert _group_similar_issues([]) == []


def test_unknown_severity_treated_as_lowest():
    issues = [
        _issue("a", "p1", "X", "weird-level", restarts=100),
        _issue("b", "p2", "X", "high", restarts=0),
    ]
    out = _group_similar_issues(issues)
    # 'weird-level' rank=9, 排在 high 后
    assert out[0][0]["severity"] == "high"
    assert out[1][0]["severity"] == "weird-level"


def test_missing_reason_field():
    """没 reason 字段不该 crash."""
    issues = [{"namespace": "a", "pod": "p1", "type": "X",
               "severity": "high", "restarts": 5}]
    out = _group_similar_issues(issues)
    assert len(out) == 1
