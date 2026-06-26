"""单测: agents.remediator._post_process_plan

覆盖 v2.1 引入的"代码强制规则"(LLM 输出做最后清洗+覆盖):
- 清洗:  'action=xxx' 前缀清掉; target 不等于真实 ns/pod 时强制改回
- R1: BarePod / Job (无控制器) → 强制 action=none
- R2: StatefulSet + CrashLoopBackOff/OOMKilled → 强制 restart_statefulset_pod L2

注意: _post_process_plan 是纯函数, 不涉及任何外部依赖, 直接调用即可.
不需要 mock LLM / K8s.
"""
from agents.remediator import _post_process_plan


def _alert(ns="default", pod="my-app-rs-abc-123", owner="ReplicaSet",
           alertname="CrashLoopBackOff"):
    return [{
        "labels": {
            "namespace": ns,
            "instance": pod,
            "owner_kind": owner,
            "alertname": alertname,
        }
    }]


# ---------------------------------------------------------------
# 清洗 1: 'action=' 前缀
# ---------------------------------------------------------------
def test_strip_action_prefix():
    plan = {"action": "action=none", "safety_level": "N/A", "target": ""}
    out = _post_process_plan(plan, _alert(owner="BarePod"))
    assert out["action"] == "none"


def test_strip_action_prefix_with_value():
    plan = {"action": "action=restart_pod", "safety_level": "L3",
            "target": "default/my-app-rs-abc-123"}
    out = _post_process_plan(plan, _alert(owner="ReplicaSet"))
    # 前缀清掉后剩 'restart_pod'
    assert out["action"] == "restart_pod"


def test_empty_after_strip_falls_back_to_none():
    plan = {"action": "action=", "safety_level": "L3"}
    out = _post_process_plan(plan, _alert(owner="ReplicaSet"))
    assert out["action"] == "none"


# ---------------------------------------------------------------
# 清洗 2: target 校正
# ---------------------------------------------------------------
def test_target_corrected_when_llm_simplifies():
    """LLM 偷懒把 pod-full 简化成 service 名, 必须改回真实 instance."""
    plan = {
        "action": "restart_pod",
        "safety_level": "L3",
        # LLM 输出的简化版 (没有 hash 后缀)
        "target": "default/my-app",
    }
    out = _post_process_plan(plan, _alert(ns="default", pod="my-app-rs-abc-123",
                                          owner="ReplicaSet"))
    assert out["target"] == "default/my-app-rs-abc-123"
    assert out.get("_target_corrected") is True


def test_target_not_corrected_when_action_is_none():
    """action=none 时 target 不参与校正 (下游 skip 掉, 不会用 target).

    注意: R1 的 target 清空只在"LLM 给了非 none 的 action 被强制改成 none"时触发;
    LLM 主动给 none 时, target 保留原样, 不影响安全 (ApprovalGate 直接 skip).
    """
    plan = {"action": "none", "safety_level": "N/A", "target": "wrong/target"}
    out = _post_process_plan(plan, _alert(owner="BarePod"))
    assert out["action"] == "none"
    # action=none → 跳过 target 校正分支, 也不进 R1 清空分支
    assert "_target_corrected" not in out
    assert "_overridden" not in out


def test_target_unchanged_when_already_correct():
    plan = {
        "action": "restart_pod",
        "safety_level": "L3",
        "target": "default/my-app-rs-abc-123",
    }
    out = _post_process_plan(plan, _alert(ns="default",
                                          pod="my-app-rs-abc-123",
                                          owner="ReplicaSet"))
    assert out["target"] == "default/my-app-rs-abc-123"
    assert "_target_corrected" not in out


# ---------------------------------------------------------------
# R1: BarePod / Job → action=none
# ---------------------------------------------------------------
def test_R1_barepod_forced_to_none():
    """BarePod (静态 Pod / debug Pod / 手动 kubectl run) — 没有控制器,
    删了不会重建, 必须 action=none."""
    plan = {
        "action": "restart_pod",         # LLM 错误地给了 restart
        "safety_level": "L3",
        "target": "default/static-pod-xxx",
        "rationale": "...",
    }
    out = _post_process_plan(plan, _alert(owner="BarePod"))
    assert out["action"] == "none"
    assert out["safety_level"] == "N/A"
    assert out["target"] == ""
    assert "R1" in out["_overridden"]


def test_R1_job_forced_to_none():
    """Job 状态机不应被外部干预 (它自己有 backoffLimit / completions)."""
    plan = {
        "action": "delete_failed_pod",
        "safety_level": "L3",
        "target": "default/my-cronjob-xxx",
    }
    out = _post_process_plan(plan, _alert(owner="Job"))
    assert out["action"] == "none"
    assert "R1" in out["_overridden"]


def test_R1_skips_when_already_none():
    """LLM 已经给 none, 不需要再覆盖, 也不该有 _overridden 标记."""
    plan = {"action": "none", "safety_level": "N/A", "target": ""}
    out = _post_process_plan(plan, _alert(owner="BarePod"))
    assert out["action"] == "none"
    assert "_overridden" not in out


# ---------------------------------------------------------------
# R2: StatefulSet + 可恢复异常类型 → restart_statefulset_pod L2
# ---------------------------------------------------------------
def test_R2_statefulset_crashloop_forced_to_l2_restart():
    """StatefulSet 遇到 CrashLoopBackOff: LLM 容易给 none (保守),
    但 L2 人审重启是合理的, 强制 restart_statefulset_pod."""
    plan = {
        "action": "none",                 # LLM 给的 none
        "safety_level": "N/A",
        "target": "",
    }
    out = _post_process_plan(plan, _alert(
        ns="prod", pod="my-sts-0",
        owner="StatefulSet", alertname="CrashLoopBackOff"))
    assert out["action"] == "restart_statefulset_pod"
    assert out["safety_level"] == "L2"
    assert out["target"] == "prod/my-sts-0"
    assert "R2" in out["_overridden"]
    # rationale / rollback 应被填充
    assert out.get("rationale")
    assert out.get("rollback")


def test_R2_statefulset_oomkilled_forced_to_l2():
    plan = {"action": "none", "safety_level": "N/A", "target": ""}
    out = _post_process_plan(plan, _alert(
        ns="prod", pod="redis-0",
        owner="StatefulSet", alertname="OOMKilled"))
    assert out["action"] == "restart_statefulset_pod"
    assert out["safety_level"] == "L2"


def test_R2_statefulset_pending_NOT_forced():
    """StatefulSet + Pending (调度问题) 重启没用, 不该走 R2."""
    plan = {"action": "none", "safety_level": "N/A", "target": ""}
    out = _post_process_plan(plan, _alert(
        owner="StatefulSet", alertname="Pending"))
    assert out["action"] == "none"
    assert "_overridden" not in out


def test_R2_skips_when_llm_already_correct():
    """LLM 已经正确给了 restart_statefulset_pod, 不要再触发 R2 标记."""
    plan = {
        "action": "restart_statefulset_pod",
        "safety_level": "L2",
        "target": "prod/my-sts-0",
    }
    out = _post_process_plan(plan, _alert(
        ns="prod", pod="my-sts-0",
        owner="StatefulSet", alertname="CrashLoopBackOff"))
    assert out["action"] == "restart_statefulset_pod"
    assert "_overridden" not in out


def test_R2_does_not_apply_to_replicaset():
    """ReplicaSet + CrashLoopBackOff: 走 restart_pod (L3), 不被 R2 误改."""
    plan = {
        "action": "restart_pod",
        "safety_level": "L3",
        "target": "default/my-app-rs-abc-123",
    }
    out = _post_process_plan(plan, _alert(
        owner="ReplicaSet", alertname="CrashLoopBackOff"))
    assert out["action"] == "restart_pod"
    assert out["safety_level"] == "L3"
    assert "_overridden" not in out


# ---------------------------------------------------------------
# 边界
# ---------------------------------------------------------------
def test_returns_input_when_plan_not_dict():
    assert _post_process_plan(None, _alert()) is None
    assert _post_process_plan("not a dict", _alert()) == "not a dict"


def test_returns_input_when_no_alerts():
    plan = {"action": "restart_pod", "safety_level": "L3"}
    out = _post_process_plan(plan, [])
    # 没原始告警, 仅清洗 action= 前缀, 其他规则跳过
    assert out["action"] == "restart_pod"


def test_handles_alert_without_labels():
    """raw_alerts[0] 缺 labels 时不该 crash."""
    plan = {"action": "restart_pod", "safety_level": "L3", "target": "x/y"}
    out = _post_process_plan(plan, [{}])
    assert out["action"] == "restart_pod"
