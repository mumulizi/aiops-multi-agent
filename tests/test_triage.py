"""单测: agents.triage.triage_node

v2.1 修复的关键 bug: 之前 triage 把原始 labels 丢了, 导致 Remediator
拿不到 owner_kind, R1/R2 强制规则全部失效. 现在必须保留 labels.
"""
from agents.triage import triage_node


def _alertmanager_payload(**kwargs):
    """模拟 Alertmanager webhook 的告警结构 (labels + annotations)."""
    base = {
        "labels": {
            "alertname": "CrashLoopBackOff",
            "severity": "critical",
            "namespace": "default",
            "instance": "my-app-abc-123",
            "owner_kind": "ReplicaSet",
        },
        "annotations": {
            "summary": "Pod CrashLoopBackOff",
            "description": "container died 5 times in 10min",
        },
        "startsAt": "2026-06-24T08:00:00Z",
    }
    base.update(kwargs)
    return base


def test_basic_field_extraction():
    state = {"raw_alerts": [_alertmanager_payload()]}
    out = triage_node(state)
    cleaned = out["raw_alerts"][0]
    assert cleaned["alertname"] == "CrashLoopBackOff"
    assert cleaned["severity_label"] == "critical"
    assert cleaned["instance"] == "my-app-abc-123"
    assert cleaned["namespace"] == "default"
    assert cleaned["summary"] == "Pod CrashLoopBackOff"
    assert cleaned["starts_at"] == "2026-06-24T08:00:00Z"


def test_alert_count_is_set():
    state = {"raw_alerts": [_alertmanager_payload(), _alertmanager_payload()]}
    out = triage_node(state)
    assert out["alert_count"] == 2


def test_labels_are_preserved():
    """v2.1 关键修复: 原始 labels (含 owner_kind) 必须透传给下游."""
    state = {"raw_alerts": [_alertmanager_payload()]}
    out = triage_node(state)
    cleaned = out["raw_alerts"][0]
    assert "labels" in cleaned, "triage 必须保留原始 labels (Remediator 后处理依赖 owner_kind)"
    assert cleaned["labels"]["owner_kind"] == "ReplicaSet"
    assert cleaned["labels"]["alertname"] == "CrashLoopBackOff"


def test_labels_is_a_copy_not_reference():
    """labels 应该是 dict 拷贝, 改 cleaned 不影响 raw."""
    raw = _alertmanager_payload()
    state = {"raw_alerts": [raw]}
    out = triage_node(state)
    cleaned = out["raw_alerts"][0]
    cleaned["labels"]["owner_kind"] = "MUTATED"
    # 原 raw 不被污染
    assert raw["labels"]["owner_kind"] == "ReplicaSet"


def test_handles_missing_labels_dict():
    """labels=None 时不该 crash (生产环境偶有畸形 payload)."""
    state = {"raw_alerts": [{"labels": None, "annotations": None,
                             "startsAt": "2026-06-24T08:00:00Z"}]}
    out = triage_node(state)
    cleaned = out["raw_alerts"][0]
    assert cleaned["alertname"] == "unknown"
    assert cleaned["instance"] == ""
    assert cleaned["labels"] == {}


def test_handles_completely_missing_keys():
    state = {"raw_alerts": [{}]}
    out = triage_node(state)
    cleaned = out["raw_alerts"][0]
    assert cleaned["alertname"] == "unknown"
    assert cleaned["severity_label"] == "unknown"
    assert cleaned["labels"] == {}


def test_empty_raw_alerts():
    state = {"raw_alerts": []}
    out = triage_node(state)
    assert out["raw_alerts"] == []
    assert out["alert_count"] == 0
