"""单测: tools.fault_memory SQLite 持久化的故障 Memory.

覆盖:
- generate_fingerprint: 同输入相同输出, 不同 ns/alert/rca 不同输出
- record_success + lookup: 写入后能查到, plan/rca/confidence 字段都对
- record_hit: hits +1, last_used 更新
- TTL: 超过 ttl_sec 后 lookup 返回 None
- only_high_confidence 过滤: 中/低置信度默认不返回
- forget: 删除后 lookup 返回 None
- list_hot / stats: 按 hits 排序, 总数对得上

每个测试用临时数据库 (monkeypatch DB_PATH), 互不影响.
"""
import time

import pytest


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """每个测试用独立 SQLite 文件, 避免互相污染."""
    from tools import fault_memory as fm
    db_path = tmp_path / "test_fault_memory.db"
    monkeypatch.setattr(fm, "DB_PATH", db_path)


# ===============================================================
# generate_fingerprint
# ===============================================================
def test_fingerprint_same_input_same_output():
    from tools.fault_memory import generate_fingerprint
    fp1 = generate_fingerprint("default", "CrashLoopBackOff", "OOM at line 5")
    fp2 = generate_fingerprint("default", "CrashLoopBackOff", "OOM at line 5")
    assert fp1 == fp2
    assert len(fp1) == 12  # md5 前 12 位


def test_fingerprint_diff_ns_diff_fp():
    from tools.fault_memory import generate_fingerprint
    fp1 = generate_fingerprint("ns-a", "X", "rca")
    fp2 = generate_fingerprint("ns-b", "X", "rca")
    assert fp1 != fp2


def test_fingerprint_diff_alertname_diff_fp():
    from tools.fault_memory import generate_fingerprint
    fp1 = generate_fingerprint("default", "CrashLoopBackOff", "rca")
    fp2 = generate_fingerprint("default", "OOMKilled", "rca")
    assert fp1 != fp2


def test_fingerprint_normalizes_whitespace():
    """RCA 多空格被压缩, 不影响指纹."""
    from tools.fault_memory import generate_fingerprint
    fp1 = generate_fingerprint("ns", "X", "OOM at  line   5")
    fp2 = generate_fingerprint("ns", "X", "OOM at line 5")
    assert fp1 == fp2


def test_fingerprint_case_insensitive():
    from tools.fault_memory import generate_fingerprint
    fp1 = generate_fingerprint("ns", "X", "OOMKilled")
    fp2 = generate_fingerprint("ns", "X", "oomkilled")
    assert fp1 == fp2


def test_fingerprint_only_uses_first_100_chars():
    """RCA 第 101 字符之后的差异不该影响指纹 (实战: Pod 名差异在尾部)."""
    from tools.fault_memory import generate_fingerprint
    rca_a = "x" * 100 + " pod-name-aaaa"
    rca_b = "x" * 100 + " pod-name-bbbb"
    assert generate_fingerprint("ns", "X", rca_a) == generate_fingerprint("ns", "X", rca_b)


def test_fingerprint_within_100_chars_diff_fp():
    """RCA 前 100 字符内的差异要触发不同指纹."""
    from tools.fault_memory import generate_fingerprint
    fp1 = generate_fingerprint("ns", "X", "OOM at line 5")
    fp2 = generate_fingerprint("ns", "X", "ImagePullBackOff registry timeout")
    assert fp1 != fp2


def test_fingerprint_empty_rca_handled():
    from tools.fault_memory import generate_fingerprint
    fp = generate_fingerprint("ns", "X", "")
    assert len(fp) == 12


# ===============================================================
# record_success + lookup
# ===============================================================
def test_record_and_lookup_roundtrip():
    from tools.fault_memory import record_success, lookup
    plan = {"action": "restart_pod", "target": "default/my-pod", "safety_level": "L3"}
    record_success("fp001", "default", "CrashLoopBackOff",
                   "container restart due to oom", plan, confidence="高")
    out = lookup("fp001")
    assert out is not None
    assert out["fingerprint"] == "fp001"
    assert out["namespace"] == "default"
    assert out["alertname"] == "CrashLoopBackOff"
    assert out["plan"] == plan
    assert out["confidence"] == "高"
    assert out["hits"] == 0  # 还没 record_hit


def test_lookup_missing_returns_none():
    from tools.fault_memory import lookup
    assert lookup("nonexistent") is None


def test_lookup_empty_fp_returns_none():
    from tools.fault_memory import lookup
    assert lookup("") is None
    assert lookup(None) is None


def test_record_overwrites_plan_keeps_hits():
    """同 fingerprint 第二次 record_success 应更新 plan, 但保留 hits."""
    from tools.fault_memory import record_success, record_hit, lookup
    plan_v1 = {"action": "restart_pod", "target": "x/y"}
    record_success("fp002", "ns", "X", "rca v1", plan_v1, confidence="高")
    record_hit("fp002")
    record_hit("fp002")
    record_hit("fp002")
    plan_v2 = {"action": "restart_pod_for_image_pull", "target": "x/y"}
    record_success("fp002", "ns", "X", "rca v2", plan_v2, confidence="高")
    out = lookup("fp002")
    assert out["plan"] == plan_v2
    assert out["rca_text"] == "rca v2"
    assert out["hits"] == 3  # 保留


# ===============================================================
# record_hit
# ===============================================================
def test_record_hit_increments():
    from tools.fault_memory import record_success, record_hit, lookup
    record_success("fp003", "ns", "X", "rca", {"action": "none"}, confidence="高")
    assert lookup("fp003")["hits"] == 0
    record_hit("fp003")
    assert lookup("fp003")["hits"] == 1
    record_hit("fp003")
    record_hit("fp003")
    assert lookup("fp003")["hits"] == 3


def test_record_hit_on_missing_fp_no_crash():
    from tools.fault_memory import record_hit
    record_hit("nonexistent")  # 不该 raise


def test_record_hit_empty_fp_noop():
    from tools.fault_memory import record_hit
    record_hit("")
    record_hit(None)


# ===============================================================
# TTL 过期
# ===============================================================
def test_ttl_expiration(monkeypatch):
    """超过 ttl_sec 的记录不再被 lookup 返回."""
    from tools.fault_memory import record_success, lookup
    record_success("fp_ttl", "ns", "X", "rca",
                   {"action": "restart_pod"}, confidence="高", ttl_sec=10)
    # 立即查命中
    assert lookup("fp_ttl") is not None
    # 把"现在"往后调 100s, 超过 ttl=10
    real_time = time.time
    monkeypatch.setattr("time.time", lambda: real_time() + 100)
    assert lookup("fp_ttl") is None


def test_ttl_long_default():
    """默认 TTL 1h 内不会过期."""
    from tools.fault_memory import record_success, lookup
    record_success("fp_long", "ns", "X", "rca",
                   {"action": "restart_pod"}, confidence="高")
    out = lookup("fp_long")
    assert out is not None
    assert out["ttl_sec"] == 3600


# ===============================================================
# 置信度过滤
# ===============================================================
def test_lookup_default_filters_low_confidence():
    """默认 only_high_confidence=True, 中/低置信度不返回."""
    from tools.fault_memory import record_success, lookup
    record_success("fp_med", "ns", "X", "rca",
                   {"action": "restart_pod"}, confidence="中")
    assert lookup("fp_med") is None  # 默认过滤
    assert lookup("fp_med", only_high_confidence=False) is not None  # 显式不过滤


def test_lookup_high_confidence_passes():
    from tools.fault_memory import record_success, lookup
    record_success("fp_high", "ns", "X", "rca",
                   {"action": "restart_pod"}, confidence="高")
    assert lookup("fp_high") is not None


# ===============================================================
# forget / stats / list_hot
# ===============================================================
def test_forget_removes_entry():
    from tools.fault_memory import record_success, lookup, forget
    record_success("fp_del", "ns", "X", "rca",
                   {"action": "restart_pod"}, confidence="高")
    assert lookup("fp_del") is not None
    assert forget("fp_del") is True
    assert lookup("fp_del") is None


def test_forget_missing_returns_false():
    from tools.fault_memory import forget
    assert forget("nonexistent") is False


def test_stats_total_entries_and_hits():
    from tools.fault_memory import record_success, record_hit, stats
    record_success("a", "ns", "X", "rca", {"action": "x"}, confidence="高")
    record_success("b", "ns", "X", "rca", {"action": "y"}, confidence="高")
    record_hit("a")
    record_hit("a")
    record_hit("b")
    s = stats()
    assert s["total_entries"] == 2
    assert s["total_hits"] == 3
    assert s["avg_hits_per_entry"] == 1.5


def test_stats_empty():
    from tools.fault_memory import stats
    s = stats()
    assert s["total_entries"] == 0
    assert s["total_hits"] == 0
    assert s["avg_hits_per_entry"] == 0


def test_list_hot_sorts_by_hits():
    from tools.fault_memory import record_success, record_hit, list_hot
    for i, n in enumerate(("a", "b", "c")):
        record_success(n, "ns", "X", f"rca {n}",
                       {"action": "x"}, confidence="高")
    # b 命中最多, a 中等, c 没命中
    for _ in range(5): record_hit("b")
    for _ in range(2): record_hit("a")
    hot = list_hot(limit=10)
    assert len(hot) == 3
    assert hot[0]["fingerprint"] == "b"
    assert hot[0]["hits"] == 5
    assert hot[1]["fingerprint"] == "a"
    assert hot[1]["hits"] == 2
    assert hot[2]["fingerprint"] == "c"
    assert hot[2]["hits"] == 0


def test_list_hot_respects_limit():
    from tools.fault_memory import record_success, list_hot
    for n in ("a", "b", "c", "d", "e"):
        record_success(n, "ns", "X", f"rca {n}",
                       {"action": "x"}, confidence="高")
    assert len(list_hot(limit=3)) == 3
    assert len(list_hot(limit=100)) == 5
