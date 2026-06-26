"""单测: agents.validator._diagnose_restart_futility

覆盖 v2.1 引入的 "重启无救" 型故障识别:
重启完仍是 RunContainerError / ImagePullBackOff / CreateContainerConfigError 等,
说明根因不在 runtime, 重启 1000 次也没用 → 升级人审 (status=escalate_human).
"""
from agents.validator import _diagnose_restart_futility, _NON_RESTARTABLE_REASONS


def _snap(*reasons):
    """构造一个只含 waiting_reasons 的 snapshot dict."""
    return {"waiting_reasons": list(reasons)}


# ---------------------------------------------------------------
# 命中: 各类 "重启无救" 异常 reason
# ---------------------------------------------------------------
def test_run_container_error_is_futile():
    """容器进程无法 exec (启动命令错), 重启没用."""
    is_futile, matched = _diagnose_restart_futility(_snap("RunContainerError"))
    assert is_futile is True
    assert "RunContainerError" in matched


def test_image_pull_backoff_is_futile():
    """拉镜像失败, 反复退避, 重启不解决问题."""
    is_futile, matched = _diagnose_restart_futility(_snap("ImagePullBackOff"))
    assert is_futile is True
    assert matched == ["ImagePullBackOff"]


def test_err_image_pull_is_futile():
    is_futile, matched = _diagnose_restart_futility(_snap("ErrImagePull"))
    assert is_futile is True


def test_invalid_image_name_is_futile():
    is_futile, matched = _diagnose_restart_futility(_snap("InvalidImageName"))
    assert is_futile is True


def test_create_container_config_error_is_futile():
    """ConfigMap/Secret 引用错: 改 ConfigMap 才行, 重启没用."""
    is_futile, matched = _diagnose_restart_futility(
        _snap("CreateContainerConfigError"))
    assert is_futile is True


def test_create_container_error_is_futile():
    is_futile, matched = _diagnose_restart_futility(_snap("CreateContainerError"))
    assert is_futile is True


def test_image_inspect_error_is_futile():
    is_futile, matched = _diagnose_restart_futility(_snap("ImageInspectError"))
    assert is_futile is True


def test_err_image_never_pull_is_futile():
    """imagePullPolicy=Never 但本地无镜像."""
    is_futile, matched = _diagnose_restart_futility(_snap("ErrImageNeverPull"))
    assert is_futile is True


def test_multiple_waiting_reasons_all_match():
    """多容器各自 waiting, 都命中黑名单时全部返回."""
    is_futile, matched = _diagnose_restart_futility(
        _snap("ImagePullBackOff", "RunContainerError"))
    assert is_futile is True
    assert set(matched) == {"ImagePullBackOff", "RunContainerError"}


def test_partial_match_is_still_futile():
    """有一个容器命中即视为 futile (不能因为另一个在恢复就忽略)."""
    is_futile, matched = _diagnose_restart_futility(
        _snap("PodInitializing", "ImagePullBackOff"))
    assert is_futile is True
    assert matched == ["ImagePullBackOff"]


# ---------------------------------------------------------------
# 不命中: 正常恢复中 / 临时状态 / 未知 reason
# ---------------------------------------------------------------
def test_pod_initializing_is_not_futile():
    """Pod 启动中, 是临时状态, 应该等等再看."""
    is_futile, matched = _diagnose_restart_futility(_snap("PodInitializing"))
    assert is_futile is False
    assert matched == []


def test_container_creating_is_not_futile():
    """ContainerCreating 是中间态, 不是 futile."""
    is_futile, matched = _diagnose_restart_futility(_snap("ContainerCreating"))
    assert is_futile is False


def test_crash_loop_back_off_NOT_in_futile():
    """CrashLoopBackOff 不在 _NON_RESTARTABLE_REASONS 里 (重启 RS/DS 通常能救).
    特意用 assert 显式标明这个边界."""
    assert "CrashLoopBackOff" not in _NON_RESTARTABLE_REASONS
    is_futile, matched = _diagnose_restart_futility(_snap("CrashLoopBackOff"))
    assert is_futile is False


def test_empty_reasons_is_not_futile():
    """容器都 ready 没有 waiting reason, 当然不 futile."""
    is_futile, matched = _diagnose_restart_futility(_snap())
    assert is_futile is False
    assert matched == []


# ---------------------------------------------------------------
# 边界 / 容错
# ---------------------------------------------------------------
def test_handles_none_snapshot():
    """snap_now=None 时不该 crash."""
    is_futile, matched = _diagnose_restart_futility(None)
    assert is_futile is False
    assert matched == []


def test_handles_empty_dict():
    is_futile, matched = _diagnose_restart_futility({})
    assert is_futile is False


def test_handles_missing_waiting_reasons_key():
    """旧版 snapshot (没收集 waiting_reasons) 应该被当作 'no waiting'."""
    is_futile, matched = _diagnose_restart_futility(
        {"phase": "Running", "containers": []})
    assert is_futile is False


def test_handles_explicit_none_value():
    is_futile, matched = _diagnose_restart_futility({"waiting_reasons": None})
    assert is_futile is False


def test_unknown_reason_is_not_futile():
    """新出现的 reason 默认不当 futile, 走原有 30s 验证逻辑."""
    is_futile, matched = _diagnose_restart_futility(
        _snap("SomeNewReasonFromK8sV132"))
    assert is_futile is False
