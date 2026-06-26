"""单测: tools.llm_factory 的 _resolve / build_llm / get_region.

只测纯函数 (env 变量 + 优先级解析), 不真的实例化 ChatOpenAI 也不调 LLM.
ChatOpenAI 实例本身用 monkeypatch 替成 fake, 验证传给它的 kwargs 是对的.
"""
import pytest


# ===============================================================
# _resolve: 优先级 ROLE_xxx > LLM_xxx > default
# ===============================================================
def test_resolve_returns_default_when_no_env(monkeypatch):
    # 清掉相关 env
    for k in ("INVESTIGATOR_MODEL", "LLM_MODEL"):
        monkeypatch.delenv(k, raising=False)
    from tools.llm_factory import _resolve
    assert _resolve("investigator", "MODEL", "fallback") == "fallback"


def test_resolve_uses_global_when_role_missing(monkeypatch):
    monkeypatch.delenv("INVESTIGATOR_MODEL", raising=False)
    monkeypatch.setenv("LLM_MODEL", "global-model")
    from tools.llm_factory import _resolve
    assert _resolve("investigator", "MODEL", "fallback") == "global-model"


def test_resolve_role_overrides_global(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "global-model")
    monkeypatch.setenv("INVESTIGATOR_MODEL", "role-model")
    from tools.llm_factory import _resolve
    assert _resolve("investigator", "MODEL", "fallback") == "role-model"


def test_resolve_case_insensitive_role(monkeypatch):
    """role 'investigator' 和 'Investigator' 都生成 INVESTIGATOR_xxx."""
    monkeypatch.setenv("INVESTIGATOR_MODEL", "x")
    from tools.llm_factory import _resolve
    assert _resolve("investigator", "MODEL", "fb") == "x"
    assert _resolve("INVESTIGATOR", "MODEL", "fb") == "x"
    assert _resolve("Investigator", "MODEL", "fb") == "x"


def test_resolve_different_keys(monkeypatch):
    monkeypatch.setenv("INVESTIGATOR_MODEL", "m")
    monkeypatch.setenv("INVESTIGATOR_BASE_URL", "url")
    monkeypatch.setenv("INVESTIGATOR_API_KEY", "key")
    from tools.llm_factory import _resolve
    assert _resolve("investigator", "MODEL", "") == "m"
    assert _resolve("investigator", "BASE_URL", "") == "url"
    assert _resolve("investigator", "API_KEY", "") == "key"


# ===============================================================
# build_llm: 验证传给 ChatOpenAI 的 kwargs
# ===============================================================
def _capture_chat_openai_kwargs(monkeypatch):
    """把 ChatOpenAI 替成一个记录 kwargs 的 fake 类, 返回 captured dict."""
    captured = {}

    class _FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    import tools.llm_factory as lf
    monkeypatch.setattr(lf, "ChatOpenAI", _FakeChatOpenAI)
    return captured


def test_build_llm_default(monkeypatch):
    for k in ("INVESTIGATOR_MODEL", "LLM_MODEL", "INVESTIGATOR_BASE_URL",
              "LLM_BASE_URL", "INVESTIGATOR_API_KEY", "LLM_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    captured = _capture_chat_openai_kwargs(monkeypatch)
    from tools.llm_factory import build_llm
    build_llm("investigator")
    assert captured["model"] == "qwen2.5-32b"
    assert captured["base_url"] == "http://localhost:8001/v1"
    assert captured["api_key"] == "dummy"
    assert captured["temperature"] == 0
    # 没传 max_tokens 时不该出现这个 key
    assert "max_tokens" not in captured


def test_build_llm_with_max_tokens(monkeypatch):
    captured = _capture_chat_openai_kwargs(monkeypatch)
    from tools.llm_factory import build_llm
    build_llm("investigator", max_tokens=1024)
    assert captured["max_tokens"] == 1024


def test_build_llm_with_temperature(monkeypatch):
    captured = _capture_chat_openai_kwargs(monkeypatch)
    from tools.llm_factory import build_llm
    build_llm("inspector", temperature=0.3)
    assert captured["temperature"] == 0.3


def test_build_llm_global_env(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "deepseek-chat")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    captured = _capture_chat_openai_kwargs(monkeypatch)
    from tools.llm_factory import build_llm
    build_llm("aggregator")
    assert captured["model"] == "deepseek-chat"
    assert captured["base_url"] == "https://api.deepseek.com/v1"
    assert captured["api_key"] == "sk-test"


def test_build_llm_role_specific_env(monkeypatch):
    """只为 Investigator 设 env, Aggregator 用全局/默认."""
    monkeypatch.setenv("INVESTIGATOR_MODEL", "claude-3-opus")
    monkeypatch.setenv("INVESTIGATOR_BASE_URL", "https://api.anthropic.com/v1")
    monkeypatch.delenv("LLM_MODEL", raising=False)
    captured = _capture_chat_openai_kwargs(monkeypatch)
    from tools.llm_factory import build_llm

    # Investigator 走专属配置
    build_llm("investigator")
    assert captured["model"] == "claude-3-opus"
    assert captured["base_url"] == "https://api.anthropic.com/v1"

    # 清空捕获, Aggregator 应该用默认值
    captured.clear()
    build_llm("aggregator")
    assert captured["model"] == "qwen2.5-32b"
    assert captured["base_url"] == "http://localhost:8001/v1"


# ===============================================================
# get_region: REGION env / AIOPS_REGION / 默认
# ===============================================================
def test_get_region_default(monkeypatch):
    monkeypatch.delenv("REGION", raising=False)
    monkeypatch.delenv("AIOPS_REGION", raising=False)
    from tools.llm_factory import get_region
    assert get_region() == "default"


def test_get_region_from_REGION(monkeypatch):
    monkeypatch.setenv("REGION", "prod-bj")
    from tools.llm_factory import get_region
    assert get_region() == "prod-bj"


def test_get_region_AIOPS_REGION_fallback(monkeypatch):
    monkeypatch.delenv("REGION", raising=False)
    monkeypatch.setenv("AIOPS_REGION", "staging")
    from tools.llm_factory import get_region
    assert get_region() == "staging"


def test_get_region_REGION_takes_precedence(monkeypatch):
    """REGION 优先于 AIOPS_REGION."""
    monkeypatch.setenv("REGION", "primary")
    monkeypatch.setenv("AIOPS_REGION", "backup")
    from tools.llm_factory import get_region
    assert get_region() == "primary"


def test_get_region_empty_string_falls_back(monkeypatch):
    """显式设置为空字符串等同未设置, 走 fallback."""
    monkeypatch.setenv("REGION", "")
    monkeypatch.delenv("AIOPS_REGION", raising=False)
    from tools.llm_factory import get_region
    # 空字符串 → falsy → 走 default
    assert get_region() == "default"
