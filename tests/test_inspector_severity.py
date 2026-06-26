"""单测: Inspector 的严重度分级 + 卡死状态优先排序.

v2.2 修复: 之前 _classify_severity 只看 restarts + phase,
ImagePullBackOff / ConfigError / Pending 这些"卡死但不重启" (restarts=0) 的
Pod 被打成 low, 会被优先级过滤 (critical/high) 漏掉, 根本不进诊断流水线.

修复后:
- 镜像错 / Container 配置错 / RunContainerError → 即使 restart=0 也是 high
- Pending (调度失败 / PVC 没绑) → high
- 排序也调整: 卡死状态优先, 再按 restart 数
"""
from agents.inspector import _classify_severity


# ---------------------------------------------------------------
# 卡死状态 (永不自愈) → high (即使 restart=0)
# ---------------------------------------------------------------
def test_imagepullbackoff_restart_0_is_high():
    """ImagePullBackOff 通常 restart=0, 但永远不会自愈, 必须 high."""
    assert _classify_severity(0, "Pending", "ImagePullBackOff") == "high"


def test_errimagepull_is_high():
    assert _classify_severity(0, "Pending", "ErrImagePull") == "high"


def test_invalid_image_name_is_high():
    assert _classify_severity(0, "Pending", "InvalidImageName") == "high"


def test_create_container_config_error_is_high():
    assert _classify_severity(0, "Pending", "CreateContainerConfigError") == "high"


def test_create_container_error_is_high():
    assert _classify_severity(0, "Pending", "CreateContainerError") == "high"


def test_run_container_error_is_high():
    assert _classify_severity(0, "Running", "RunContainerError") == "high"


def test_image_pull_keyword_case_insensitive():
    """关键词匹配大小写不敏感."""
    assert _classify_severity(0, "Pending", "imagePullBackOff") == "high"
    assert _classify_severity(0, "Pending", "errImagePull") == "high"


def test_image_pull_in_last_reason_string():
    """reason 可能是 'last:ImagePullBackOff' 这种带前缀的形式."""
    assert _classify_severity(0, "Pending", "last:ImagePullBackOff") == "high"


# ---------------------------------------------------------------
# Pending (调度失败) → high
# ---------------------------------------------------------------
def test_pending_no_reason_is_high():
    """Pending 状态本身就是异常, 即使 reason 为空也该是 high."""
    assert _classify_severity(0, "Pending", "") == "high"


def test_pending_other_reason_is_high():
    assert _classify_severity(0, "Pending", "Unschedulable") == "high"


# ---------------------------------------------------------------
# Failed / Unknown phase → critical (与之前一致)
# ---------------------------------------------------------------
def test_failed_phase_is_critical():
    assert _classify_severity(0, "Failed", "") == "critical"


def test_unknown_phase_is_critical():
    assert _classify_severity(0, "Unknown", "") == "critical"


def test_failed_phase_overrides_image_pull():
    """phase=Failed 优先级最高."""
    assert _classify_severity(0, "Failed", "ImagePullBackOff") == "critical"


# ---------------------------------------------------------------
# 反复重启型: 按 restart 数分级 (与之前一致)
# ---------------------------------------------------------------
def test_restart_1000_is_critical():
    assert _classify_severity(1000, "Running", "CrashLoopBackOff") == "critical"


def test_restart_100_is_high():
    assert _classify_severity(100, "Running", "CrashLoopBackOff") == "high"


def test_restart_10_is_medium():
    assert _classify_severity(10, "Running", "CrashLoopBackOff") == "medium"


def test_restart_5_running_is_low():
    """少量重启, 没卡死, 仍是 low (调度器不会优先处理)."""
    assert _classify_severity(5, "Running", "CrashLoopBackOff") == "low"


def test_restart_0_running_no_reason_is_low():
    """完全正常的 pod (实际不会进 unhealthy 列表, 但函数本身也不该报错)."""
    assert _classify_severity(0, "Running", "") == "low"


# ---------------------------------------------------------------
# 优先级覆盖关系
# ---------------------------------------------------------------
def test_image_pull_with_high_restart_still_high():
    """同时是镜像错 + 大量 restart, 仍是 high (不是 critical, 不在 phase=Failed)."""
    assert _classify_severity(500, "Running", "ImagePullBackOff") == "high"


def test_oom_killed_high_restart_is_high():
    """OOM 100 次, 按 restart 数走 high (能自愈)."""
    assert _classify_severity(100, "Running", "OOMKilled") == "high"


def test_reason_none_handled():
    """reason=None 不该 crash, 默认按 restart 数判."""
    assert _classify_severity(100, "Running", None) == "high"
    assert _classify_severity(0, "Running", None) == "low"
