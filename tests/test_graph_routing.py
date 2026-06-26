"""单测: graph.py 的 v2.3 路由逻辑.

只测纯路由函数 _route_after_validator, 不构造完整 Graph (那需要连 LLM/K8s).
"""
import pytest


# ===============================================================
# _route_after_validator: failed → investigator (有重试预算)
# ===============================================================
def test_failed_first_retry_routes_to_investigator():
    """validation_result=failed + retry_count=0 → 回 investigator (开闭环)."""
    from graph import _route_after_validator
    state = {
        "validation_result": {"status": "failed", "reason": "restarts +10"},
        "retry_count": 0,
    }
    assert _route_after_validator(state) == "investigator"


def test_failed_second_retry_routes_to_investigator():
    """validation_result=failed + retry_count=1 → 还有 1 次预算, 继续重诊."""
    from graph import _route_after_validator
    state = {
        "validation_result": {"status": "failed", "reason": "still failing"},
        "retry_count": 1,
    }
    assert _route_after_validator(state) == "investigator"


def test_failed_max_retries_routes_to_notifier():
    """retry_count 达到上限 (默认 2) → 不再重诊, 走 notifier 让人介入."""
    from graph import _route_after_validator
    state = {
        "validation_result": {"status": "failed", "reason": "still failing"},
        "retry_count": 2,
    }
    assert _route_after_validator(state) == "notifier"


def test_failed_above_max_retries_routes_to_notifier():
    """超过上限直接走 notifier (理论上不该发生, 但要兜底)."""
    from graph import _route_after_validator
    state = {
        "validation_result": {"status": "failed"},
        "retry_count": 5,
    }
    assert _route_after_validator(state) == "notifier"


# ===============================================================
# 其他状态 → notifier (不进闭环)
# ===============================================================
def test_success_routes_to_notifier():
    from graph import _route_after_validator
    state = {
        "validation_result": {"status": "success"},
        "retry_count": 0,
    }
    assert _route_after_validator(state) == "notifier"


def test_pending_routes_to_notifier():
    """pending 状态不该进入闭环 (可能还在恢复中, 重诊会浪费 LLM 调用)."""
    from graph import _route_after_validator
    state = {
        "validation_result": {"status": "pending"},
        "retry_count": 0,
    }
    assert _route_after_validator(state) == "notifier"


def test_skipped_routes_to_notifier():
    from graph import _route_after_validator
    state = {
        "validation_result": {"status": "skipped"},
        "retry_count": 0,
    }
    assert _route_after_validator(state) == "notifier"


def test_escalate_human_routes_to_notifier():
    """escalate_human (重启无救型) 永不重诊, 直接通知."""
    from graph import _route_after_validator
    state = {
        "validation_result": {"status": "escalate_human"},
        "retry_count": 0,
    }
    assert _route_after_validator(state) == "notifier"


def test_partial_routes_to_notifier():
    """partial (Ready 但 restart 涨) 不进闭环, 让运维看下."""
    from graph import _route_after_validator
    state = {
        "validation_result": {"status": "partial"},
        "retry_count": 0,
    }
    assert _route_after_validator(state) == "notifier"


# ===============================================================
# 边界
# ===============================================================
def test_no_validation_result_routes_to_notifier():
    """状态字段缺失不该 crash, 兜底走 notifier."""
    from graph import _route_after_validator
    state = {"retry_count": 0}
    assert _route_after_validator(state) == "notifier"


def test_retry_count_default_zero():
    """state 没 retry_count 当 0 处理."""
    from graph import _route_after_validator
    state = {"validation_result": {"status": "failed"}}
    # retry_count 默认 0, 应该重诊
    assert _route_after_validator(state) == "investigator"


def test_max_retries_env_override(monkeypatch):
    """SELF_HEAL_MAX_RETRIES 环境变量能调整上限."""
    monkeypatch.setenv("SELF_HEAL_MAX_RETRIES", "5")
    from graph import _route_after_validator
    state = {
        "validation_result": {"status": "failed"},
        "retry_count": 3,
    }
    # 上限是 5, retry_count=3 < 5, 应继续重诊
    assert _route_after_validator(state) == "investigator"


def test_max_retries_env_invalid_falls_back_to_default(monkeypatch):
    """无效环境变量值不该 crash, 用默认 2."""
    monkeypatch.setenv("SELF_HEAL_MAX_RETRIES", "not-a-number")
    from graph import _route_after_validator
    state = {
        "validation_result": {"status": "failed"},
        "retry_count": 2,
    }
    # 默认 2, retry_count=2 已达上限, 走 notifier
    assert _route_after_validator(state) == "notifier"


# ===============================================================
# _route_by_severity (回归测试, 没改但确认没退化)
# ===============================================================
def test_critical_routes_to_investigator():
    from graph import _route_by_severity
    assert _route_by_severity({"severity": "critical"}) == "investigator"


def test_high_routes_to_investigator():
    from graph import _route_by_severity
    assert _route_by_severity({"severity": "high"}) == "investigator"


def test_medium_routes_to_notifier():
    from graph import _route_by_severity
    assert _route_by_severity({"severity": "medium"}) == "notifier"


def test_low_routes_to_notifier():
    from graph import _route_by_severity
    assert _route_by_severity({"severity": "low"}) == "notifier"


def test_severity_default_medium_routes_to_notifier():
    """severity 字段缺失默认 medium, 走 notifier."""
    from graph import _route_by_severity
    assert _route_by_severity({}) == "notifier"
