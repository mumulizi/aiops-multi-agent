"""统一的 LLM 客户端工厂.

让所有 Agent 通过 build_llm() 拿 ChatOpenAI 实例, 模型/端口/key 通过环境变量
切换, 不需要改代码. 这是 v2.8 多集群部署的基础设施.

## 使用

```python
from tools.llm_factory import build_llm
_llm = build_llm("investigator", temperature=0, max_tokens=512)
```

## 环境变量

全局默认 (5 个 Agent 共享):
  LLM_MODEL       默认 qwen2.5-32b   (与 vLLM --served-model-name 对齐)
  LLM_BASE_URL    默认 http://localhost:8001/v1
  LLM_API_KEY     默认 dummy        (本地 vLLM 不校验)

按角色覆盖 (优先级高于全局):
  <ROLE>_MODEL / <ROLE>_BASE_URL / <ROLE>_API_KEY
  ROLE = INSPECTOR | INVESTIGATOR | REMEDIATOR | CLASSIFIER | AGGREGATOR

例: 让 Investigator/Remediator 走云 API 强模型, 其他保留本地:
  export INVESTIGATOR_MODEL=deepseek-chat
  export INVESTIGATOR_BASE_URL=https://api.deepseek.com/v1
  export INVESTIGATOR_API_KEY=sk-xxx
  export REMEDIATOR_MODEL=deepseek-chat
  export REMEDIATOR_BASE_URL=https://api.deepseek.com/v1
  export REMEDIATOR_API_KEY=sk-xxx

多集群部署: 每个集群用一份 .env / systemd 环境变量, 主程序不需要改.
"""
import os
from typing import Optional

from langchain_openai import ChatOpenAI
from tools.langfuse_setup import LANGFUSE_HANDLER


def _resolve(role: str, key: str, default: str) -> str:
    """优先级: 角色专属 env > 全局 LLM_xxx > default."""
    role_env = f"{role.upper()}_{key}"
    global_env = f"LLM_{key}"
    return os.getenv(role_env) or os.getenv(global_env) or default


def build_llm(role: str, *, temperature: float = 0,
              max_tokens: Optional[int] = None) -> ChatOpenAI:
    """创建一个 ChatOpenAI 实例.

    role: agent 名 (inspector / investigator / remediator / classifier /
          aggregator). 仅用于环境变量前缀.
    temperature: 默认 0 (确定性). Inspector 巡检阶段建议 0.1 留点探索 (但
                 v2.7 后 Inspector 不再调用 LLM).
    max_tokens: None 表示不显式限制. 默认 1024 (覆盖大部分 RCA / plan 输出
                不被截断的需求).
    """
    model = _resolve(role, "MODEL", "qwen2.5-32b")
    base_url = _resolve(role, "BASE_URL", "http://localhost:8001/v1")
    api_key = _resolve(role, "API_KEY", "dummy")

    callbacks = [LANGFUSE_HANDLER] if LANGFUSE_HANDLER else []
    kwargs = {
        "model": model,
        "base_url": base_url,
        "api_key": api_key,
        "temperature": temperature,
        "callbacks": callbacks,
    }
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    return ChatOpenAI(**kwargs)


def get_region() -> str:
    """获取当前部署的 region 标识符 (用于多集群部署区分告警来源).

    优先级:
    1. REGION 环境变量
    2. AIOPS_REGION 环境变量 (向后兼容备选)
    3. 默认 "default"
    """
    return os.getenv("REGION") or os.getenv("AIOPS_REGION") or "default"
