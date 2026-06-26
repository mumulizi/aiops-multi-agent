"""单测: tools.policy 忽略策略.

覆盖:
- load_policies: 文件不存在 / YAML 格式错 / 空文件 / 正常加载
- should_ignore: 整 ns / ns+pod 精确 / ns+pod_pattern glob / 不命中
- filter_issues: 批量过滤 + 忽略原因记录
- 边界: 缺字段, 字段类型错, 多规则混用
"""
import pytest
from pathlib import Path

from tools.policy import (
    load_policies, should_ignore, filter_issues, _match_one_rule,
)


def _issue(ns, pod, **kw):
    base = {"namespace": ns, "pod": pod, "type": "X", "severity": "high"}
    base.update(kw)
    return base


# ===============================================================
# load_policies
# ===============================================================
def test_load_missing_file_returns_empty(tmp_path):
    """文件不存在: 返回空 dict, 不打印警告 (合法的 '无策略' 状态)."""
    p = load_policies(str(tmp_path / "nonexistent.yaml"))
    assert p == {}


def test_load_empty_file(tmp_path):
    f = tmp_path / "empty.yaml"
    f.write_text("")
    p = load_policies(str(f))
    assert p == {}


def test_load_only_comments(tmp_path):
    f = tmp_path / "comments.yaml"
    f.write_text("# 这是注释\n# 没有任何规则\n")
    p = load_policies(str(f))
    assert p == {}


def test_load_normal(tmp_path):
    f = tmp_path / "policies.yaml"
    f.write_text("""
ignores:
  - namespace: monitoring
    reason: monitor team
  - namespace: dev
    pod_pattern: "test-*"
    reason: test pods
""")
    p = load_policies(str(f))
    assert isinstance(p, dict)
    assert len(p["ignores"]) == 2
    assert p["ignores"][0]["namespace"] == "monitoring"


def test_load_invalid_yaml_returns_empty(tmp_path, capsys):
    f = tmp_path / "bad.yaml"
    # 这才是真正会让 YAML 解析失败的格式 (不闭合的 flow 序列)
    f.write_text("ignores: [\n  bad: : :\n")
    p = load_policies(str(f))
    assert p == {}
    out = capsys.readouterr().out
    assert "失败" in out or "无策略" in out


def test_load_top_level_not_dict(tmp_path, capsys):
    f = tmp_path / "list.yaml"
    f.write_text("- just a list\n")
    p = load_policies(str(f))
    assert p == {}


# ===============================================================
# _match_one_rule
# ===============================================================
def test_rule_namespace_only_matches_any_pod_in_ns():
    """只写 namespace, 不写 pod → 整 ns 任意 pod 都命中."""
    rule = {"namespace": "monitoring"}
    assert _match_one_rule(rule, "monitoring", "anything") is True
    assert _match_one_rule(rule, "monitoring", "another-pod") is True


def test_rule_namespace_only_does_not_match_other_ns():
    rule = {"namespace": "monitoring"}
    assert _match_one_rule(rule, "default", "x") is False


def test_rule_namespace_pod_exact():
    rule = {"namespace": "default", "pod": "my-pod-123"}
    assert _match_one_rule(rule, "default", "my-pod-123") is True
    assert _match_one_rule(rule, "default", "my-pod-456") is False
    assert _match_one_rule(rule, "other", "my-pod-123") is False


def test_rule_namespace_pod_pattern_glob():
    rule = {"namespace": "ci", "pod_pattern": "test-*"}
    assert _match_one_rule(rule, "ci", "test-foo") is True
    assert _match_one_rule(rule, "ci", "test-") is True   # * 匹配 0 个字符
    assert _match_one_rule(rule, "ci", "production-test") is False
    assert _match_one_rule(rule, "ci", "Test-foo") is False  # case sensitive
    assert _match_one_rule(rule, "default", "test-foo") is False


def test_rule_pod_pattern_with_question_mark():
    """? 匹配单个字符."""
    rule = {"namespace": "x", "pod_pattern": "pod-?"}
    assert _match_one_rule(rule, "x", "pod-a") is True
    assert _match_one_rule(rule, "x", "pod-ab") is False


def test_rule_pod_pattern_with_charset():
    """[abc] 字符集."""
    rule = {"namespace": "x", "pod_pattern": "pod-[123]"}
    assert _match_one_rule(rule, "x", "pod-1") is True
    assert _match_one_rule(rule, "x", "pod-2") is True
    assert _match_one_rule(rule, "x", "pod-9") is False


def test_rule_with_both_pod_and_pattern():
    """同时写 pod 和 pod_pattern: 任一命中即算命中."""
    rule = {"namespace": "x", "pod": "exact-pod",
            "pod_pattern": "wild-*"}
    assert _match_one_rule(rule, "x", "exact-pod") is True
    assert _match_one_rule(rule, "x", "wild-anything") is True
    assert _match_one_rule(rule, "x", "neither") is False


def test_rule_empty_namespace_never_matches():
    """规则的 namespace 字段为空, 视为不匹配."""
    rule = {"namespace": "", "pod_pattern": "*"}
    assert _match_one_rule(rule, "anything", "anything") is False


def test_rule_missing_namespace_field_never_matches():
    rule = {"pod": "x"}  # 没 namespace
    assert _match_one_rule(rule, "default", "x") is False


def test_rule_not_dict_returns_false():
    """规则不是 dict 时优雅 return False."""
    assert _match_one_rule(None, "x", "y") is False
    assert _match_one_rule("not a dict", "x", "y") is False
    assert _match_one_rule(["list"], "x", "y") is False


# ===============================================================
# should_ignore
# ===============================================================
def test_should_ignore_no_policies():
    assert should_ignore(_issue("default", "p"), {}) == (False, "")
    assert should_ignore(_issue("default", "p"), None) == (False, "")


def test_should_ignore_empty_ignores():
    assert should_ignore(_issue("default", "p"), {"ignores": []}) == (False, "")
    assert should_ignore(_issue("default", "p"), {"ignores": None}) == (False, "")


def test_should_ignore_namespace_match():
    policies = {"ignores": [
        {"namespace": "monitoring", "reason": "monitor team"},
    ]}
    ok, reason = should_ignore(_issue("monitoring", "any-pod"), policies)
    assert ok is True
    assert "monitor team" in reason
    assert "整 ns=monitoring" in reason


def test_should_ignore_namespace_pod_exact():
    policies = {"ignores": [
        {"namespace": "default", "pod": "broken-pod",
         "reason": "known issue"},
    ]}
    ok, reason = should_ignore(_issue("default", "broken-pod"), policies)
    assert ok is True
    assert "known issue" in reason
    assert "ns=default+pod=broken-pod" in reason


def test_should_ignore_pattern_match():
    policies = {"ignores": [
        {"namespace": "ci", "pod_pattern": "test-*",
         "reason": "ci tmp pods"},
    ]}
    ok, reason = should_ignore(_issue("ci", "test-build-123"), policies)
    assert ok is True
    assert "ci tmp pods" in reason
    assert "pattern=test-*" in reason


def test_should_ignore_no_match_returns_false():
    policies = {"ignores": [
        {"namespace": "monitoring", "reason": "x"},
        {"namespace": "ci", "pod_pattern": "test-*"},
    ]}
    ok, reason = should_ignore(_issue("default", "my-pod"), policies)
    assert ok is False
    assert reason == ""


def test_should_ignore_first_match_wins():
    """多条规则按顺序匹配, 第一个命中的就用 (reason 取自该规则)."""
    policies = {"ignores": [
        {"namespace": "x", "pod_pattern": "*", "reason": "first rule"},
        {"namespace": "x", "pod_pattern": "*", "reason": "second rule"},
    ]}
    ok, reason = should_ignore(_issue("x", "y"), policies)
    assert ok is True
    assert "first rule" in reason


def test_should_ignore_missing_reason_uses_default():
    """规则没写 reason 字段也不该 crash."""
    policies = {"ignores": [{"namespace": "x"}]}
    ok, reason = should_ignore(_issue("x", "y"), policies)
    assert ok is True
    assert "无理由说明" in reason


def test_should_ignore_handles_missing_issue_fields():
    policies = {"ignores": [{"namespace": "x"}]}
    # issue 缺字段
    assert should_ignore({}, policies) == (False, "")
    assert should_ignore({"namespace": ""}, policies) == (False, "")


# ===============================================================
# filter_issues 批量
# ===============================================================
def test_filter_issues_basic():
    policies = {"ignores": [
        {"namespace": "monitoring", "reason": "skip"},
        {"namespace": "default", "pod_pattern": "test-*", "reason": "tests"},
    ]}
    issues = [
        _issue("monitoring", "prom-server"),
        _issue("default", "test-build-1"),
        _issue("default", "real-app"),
        _issue("prod", "user-service-1"),
    ]
    kept, ignored = filter_issues(issues, policies)
    assert len(kept) == 2
    assert {i["pod"] for i in kept} == {"real-app", "user-service-1"}
    assert len(ignored) == 2
    assert {x["pod"] for x in ignored} == {"prom-server", "test-build-1"}


def test_filter_issues_no_match_keeps_all():
    policies = {"ignores": [{"namespace": "monitoring"}]}
    issues = [_issue("default", "a"), _issue("prod", "b")]
    kept, ignored = filter_issues(issues, policies)
    assert len(kept) == 2
    assert ignored == []


def test_filter_issues_all_ignored():
    policies = {"ignores": [{"namespace": "x"}]}
    issues = [_issue("x", "a"), _issue("x", "b"), _issue("x", "c")]
    kept, ignored = filter_issues(issues, policies)
    assert kept == []
    assert len(ignored) == 3


def test_filter_issues_empty_input():
    kept, ignored = filter_issues([], {"ignores": [{"namespace": "x"}]})
    assert kept == []
    assert ignored == []


def test_filter_issues_no_policies():
    issues = [_issue("a", "b")]
    kept, ignored = filter_issues(issues, {})
    assert len(kept) == 1
    assert ignored == []
