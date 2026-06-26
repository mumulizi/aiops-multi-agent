"""单测: agents.remediator R3 强制规则 (重启无救型故障).

R3 是 v2.2 新增的强制规则:
- RCA 文本里出现 "no such file" / "flag provided but not defined" 等关键词
- 或 alertname 命中 RunContainerError / ImagePullBackOff 等
→ 强制 action=none, 标 escalate_human, 不让 restart 假修复.

关键场景: 实战里出现过 noaheeops-task-manager-0 被 R2 强制成 restart_statefulset_pod L2,
但根因是 config.yaml 路径错, 重启 1000 次也没用. R3 在这之上再覆盖一层, 截断假修复.
"""
from agents.remediator import _post_process_plan, _is_non_restartable_failure


def _alert(ns="default", pod="my-app-abc", owner="ReplicaSet", alertname="CrashLoopBackOff"):
    return [{"labels": {
        "namespace": ns, "instance": pod,
        "owner_kind": owner, "alertname": alertname,
    }}]


# ---------------------------------------------------------------
# _is_non_restartable_failure: 判定逻辑
# ---------------------------------------------------------------
def test_alertname_runcontainererror_is_futile():
    assert _is_non_restartable_failure("", "RunContainerError") is True


def test_alertname_imagepullbackoff_is_futile():
    assert _is_non_restartable_failure("", "ImagePullBackOff") is True


def test_alertname_createcontainerconfigerror_is_futile():
    assert _is_non_restartable_failure("", "CreateContainerConfigError") is True


def test_normal_alertname_not_futile():
    assert _is_non_restartable_failure("", "CrashLoopBackOff") is False
    assert _is_non_restartable_failure("", "OOMKilled") is False
    assert _is_non_restartable_failure("", "Unhealthy") is False


# RCA 文本命中
def test_rca_no_such_file_is_futile():
    rca = "exec: stat /home/work/config.yaml: no such file or directory"
    assert _is_non_restartable_failure(rca.lower(), "CrashLoopBackOff") is True


def test_rca_flag_not_defined_is_futile():
    rca = "flag provided but not defined: -kubeConfig"
    assert _is_non_restartable_failure(rca.lower(), "CrashLoopBackOff") is True


def test_rca_executable_not_found_is_futile():
    rca = "executable file not found in $path"
    assert _is_non_restartable_failure(rca.lower(), "CrashLoopBackOff") is True


def test_rca_image_pull_keyword_is_futile():
    rca = "container in errimagepull state, registry not reachable"
    assert _is_non_restartable_failure(rca.lower(), "CrashLoopBackOff") is True


def test_rca_normal_oom_is_not_futile():
    """普通 OOM/CrashLoop 不该被当 R3 命中, restart 是合理修复."""
    rca = "container memory exceeded limit 512mi, restart history shows similar pattern"
    assert _is_non_restartable_failure(rca.lower(), "CrashLoopBackOff") is False


def test_rca_liveness_probe_is_not_futile():
    """liveness probe 失败 - restart_pod 是合理治标方案."""
    rca = "liveness probe failed, container marked unhealthy and killed"
    assert _is_non_restartable_failure(rca.lower(), "Unhealthy") is False


# 边界
def test_empty_rca_and_alertname_not_futile():
    assert _is_non_restartable_failure("", "") is False


def test_none_rca_handled():
    """rca=None 不该 crash."""
    assert _is_non_restartable_failure("", "RunContainerError") is True


# ---------------------------------------------------------------
# _post_process_plan: R3 实际覆盖效果
# ---------------------------------------------------------------
def test_R3_restart_pod_forced_to_none_by_rca():
    """LLM 给了 restart_pod, 但 RCA 显示是配置文件错, R3 强制改 none."""
    plan = {
        "action": "restart_pod",
        "safety_level": "L3",
        "target": "default/my-app-abc",
        "rationale": "...",
    }
    rca = "exec: stat /etc/app/config.yaml: no such file or directory"
    out = _post_process_plan(plan, _alert(owner="ReplicaSet"), rca=rca)
    assert out["action"] == "none"
    assert out["safety_level"] == "N/A"
    assert out["target"] == ""
    assert out.get("escalate_human") is True
    assert "R3" in out["_overridden"]


def test_R3_restart_statefulset_pod_forced_to_none():
    """实战 case: noaheeops-task-manager-0 (StatefulSet) 配置文件错.
    R2 之前会强制成 restart_statefulset_pod, R3 再覆盖一层成 none."""
    plan = {
        "action": "none",  # LLM 给的 none
        "safety_level": "N/A",
        "target": "",
    }
    rca = ('exec: "--config=/home/work/noah/config.yaml": '
           'stat --config=/home/work/noah/config.yaml: no such file or directory')
    # owner=StatefulSet + CrashLoopBackOff 会先触发 R2, R3 应该再覆盖
    out = _post_process_plan(
        plan,
        _alert(ns="prod", pod="task-manager-0", owner="StatefulSet",
               alertname="CrashLoopBackOff"),
        rca=rca,
    )
    # R3 应该最终生效
    assert out["action"] == "none"
    assert out.get("escalate_human") is True
    assert "R3" in out.get("_overridden", "")


def test_R3_image_pull_alertname_forced_to_none():
    """alertname=ImagePullBackOff 直接命中 R3, 不再走 restart_pod_for_image_pull."""
    plan = {
        "action": "restart_pod_for_image_pull",
        "safety_level": "L3",
        "target": "default/my-app-abc",
    }
    out = _post_process_plan(
        plan,
        _alert(owner="ReplicaSet", alertname="ImagePullBackOff"),
        rca="container waiting for image pull, registry returned 401",
    )
    assert out["action"] == "none"
    assert "R3" in out["_overridden"]
    assert out.get("escalate_human") is True


def test_R3_does_not_touch_action_none():
    """LLM 已经给 none, R3 不该多此一举改成 none (本来就是)."""
    plan = {"action": "none", "safety_level": "N/A", "target": ""}
    rca = "no such file or directory"
    out = _post_process_plan(plan, _alert(owner="BarePod"), rca=rca)
    # R1 (BarePod) 触发, 但 LLM 已经给了 none, 不会有 _overridden
    assert out["action"] == "none"
    # R3 也不该再覆盖 _overridden, 因为 R1 没改动作 (action 一直是 none)
    # escalate_human 不会被设, 因为 R3 只在 cur_action ∈ restart_xxx 时才动
    assert out.get("escalate_human") is None or out.get("escalate_human") is False


def test_R3_does_not_apply_to_normal_failure():
    """普通 CrashLoop 没 RCA 命中, restart_pod 应该保留."""
    plan = {
        "action": "restart_pod",
        "safety_level": "L3",
        "target": "default/my-app-abc",
    }
    rca = "container memory exceeded limit, oomkilled by kernel"
    out = _post_process_plan(plan, _alert(owner="ReplicaSet"), rca=rca)
    assert out["action"] == "restart_pod"
    assert out["safety_level"] == "L3"
    assert out.get("escalate_human") is None or out.get("escalate_human") is False


def test_R3_handles_empty_rca():
    """没 RCA 也不该 crash (走 alertname 路径)."""
    plan = {"action": "restart_pod", "safety_level": "L3",
            "target": "default/my-app-abc"}
    out = _post_process_plan(plan, _alert(owner="ReplicaSet"), rca="")
    # alertname=CrashLoopBackOff 不命中 R3, action 保留
    assert out["action"] == "restart_pod"


def test_R3_priority_over_R2():
    """同时满足 R2 (StatefulSet+CrashLoop) 和 R3 (RCA 命中) 时, R3 最终生效."""
    plan = {"action": "none", "safety_level": "N/A", "target": ""}
    rca = "flag provided but not defined: -kubeConfig"
    out = _post_process_plan(
        plan,
        _alert(ns="kube-system", pod="my-sts-0", owner="StatefulSet",
               alertname="CrashLoopBackOff"),
        rca=rca,
    )
    # R2 先把 action 改成 restart_statefulset_pod, R3 再覆盖回 none
    assert out["action"] == "none"
    assert "R3" in out.get("_overridden", "")


# ---------------------------------------------------------------
# 向后兼容: rca=None 默认值
# ---------------------------------------------------------------
def test_post_process_without_rca_param():
    """rca 是关键字参数有默认值, 不传时 R3 跳过, 老调用不受影响."""
    plan = {"action": "restart_pod", "safety_level": "L3",
            "target": "default/my-app-abc"}
    out = _post_process_plan(plan, _alert(owner="ReplicaSet"))
    # 没 RCA 输入, alertname=CrashLoopBackOff 不命中 R3, restart_pod 保留
    assert out["action"] == "restart_pod"
