"""单测: v2.9 Function Calling Native — _parse_fc_final 的自然语言抽取.

Function Calling 模式下, LLM 收尾时返回自然语言 (不再 JSON).
_parse_fc_final 负责把 "根因: ... / 置信度: ... / 关键证据: ..." 解析成 dict.

只测纯解析函数, 不调真实 LLM.
"""
from agents.investigator import _parse_fc_final


def test_standard_format():
    """标准三行格式: 根因 / 置信度 / 关键证据."""
    text = (
        "根因: kube-external-auditor Pod 因为 -kubeConfig 标志未定义崩溃.\n"
        "置信度: 高\n"
        "关键证据: flag provided but not defined: -kubeConfig"
    )
    out = _parse_fc_final(text)
    assert "kubeConfig" in out["hypothesis"]
    assert "未定义" in out["hypothesis"]
    assert out["confidence"] == "高"
    assert any("flag provided" in e for e in out["key_evidence"])


def test_chinese_colon():
    """中文冒号 ：也要识别."""
    text = (
        "根因：noaheeops-task-manager 配置文件路径错.\n"
        "置信度：中\n"
        "关键证据：no such file or directory"
    )
    out = _parse_fc_final(text)
    assert "配置文件路径错" in out["hypothesis"]
    assert out["confidence"] == "中"


def test_low_confidence():
    text = (
        "根因: 未明确, 可能是定时任务退出.\n"
        "置信度: 低\n"
        "关键证据: container_statuses.last_terminated.reason=Completed exit_code=0"
    )
    out = _parse_fc_final(text)
    assert out["confidence"] == "低"


def test_multi_evidence_comma():
    """多条证据用中文 / 英文逗号分隔."""
    text = (
        "根因: OOM.\n"
        "置信度: 高\n"
        "关键证据: 内存超限, last_terminated.reason=OOMKilled, exit_code=137"
    )
    out = _parse_fc_final(text)
    assert len(out["key_evidence"]) == 3


def test_multi_evidence_semicolon():
    text = (
        "根因: 镜像拉取失败.\n"
        "置信度: 高\n"
        "关键证据: rpc error; failed to pull image; registry timeout"
    )
    out = _parse_fc_final(text)
    assert len(out["key_evidence"]) == 3


def test_no_evidence_field():
    """LLM 偶尔忘写关键证据, 不该 crash."""
    text = (
        "根因: ImagePullBackOff.\n"
        "置信度: 中"
    )
    out = _parse_fc_final(text)
    assert out["hypothesis"]
    assert out["confidence"] == "中"
    assert out["key_evidence"] == []


def test_no_confidence_default_to_medium():
    """没写置信度时默认 '中', 不丢失结论."""
    text = "根因: 调度失败, 节点亲和性不匹配."
    out = _parse_fc_final(text)
    assert "调度失败" in out["hypothesis"]
    assert out["confidence"] == "中"


def test_freeform_text_fallback():
    """LLM 没按格式输出, 整段当 hypothesis."""
    text = (
        "我觉得这个 Pod 挂了是因为内存不够, "
        "container_memory_usage_bytes 显示一直在涨, 没有 limit."
    )
    out = _parse_fc_final(text)
    assert len(out["hypothesis"]) > 10
    assert out["confidence"] == "中"
    assert out["key_evidence"] == []


def test_empty_string():
    """空回答兜底."""
    out = _parse_fc_final("")
    assert out["hypothesis"] == "(空回答)"
    assert out["confidence"] == "?"


def test_none_input():
    """None 输入兜底 (LLM 调用失败时)."""
    out = _parse_fc_final(None)
    assert out["hypothesis"] == "(空回答)"


def test_long_hypothesis_truncated_at_400():
    """超长自由文本截到 400 字."""
    long_text = "x" * 1000
    out = _parse_fc_final(long_text)
    assert len(out["hypothesis"]) == 400


def test_hypothesis_stops_at_next_label():
    """根因后面跟置信度时, 根因不该把后面的字段吸进去."""
    text = (
        "根因: A 服务调 B 服务超时.\n"
        "置信度: 高"
    )
    out = _parse_fc_final(text)
    assert out["hypothesis"] == "A 服务调 B 服务超时."
    assert "置信度" not in out["hypothesis"]
