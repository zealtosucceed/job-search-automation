"""Adapter registry — the single place a provider is wired in.

To add a provider later (OpenAI, Ollama, ...):
  1. write an adapter class implementing LLMAdapter.complete()
  2. add one line to ADAPTERS below
Then switching is a one-line change in config.yaml: `llm.provider: <name>`.
"""

from __future__ import annotations

from ...config import Config
from .anthropic_adapter import AnthropicAdapter
from .base import LLMAdapter
from .groq_adapter import GroqAdapter
from .openai_adapter import OpenAIAdapter

# name -> adapter class
ADAPTERS: dict[str, type[LLMAdapter]] = {
    "anthropic": AnthropicAdapter,
    "openai": OpenAIAdapter,
    "groq": GroqAdapter,
    # "ollama": OllamaAdapter,    # future — local
}


def get_llm(cfg: Config) -> LLMAdapter:
    provider = cfg.llm.get("provider", "anthropic")
    if provider not in ADAPTERS:
        raise ValueError(
            f"Unknown llm.provider '{provider}'. Available: {sorted(ADAPTERS)}"
        )
    adapter_cls = ADAPTERS[provider]
    return adapter_cls(
        model=cfg.llm.get("model", "claude-opus-4-8"),
        max_tokens=int(cfg.llm.get("max_tokens", 8000)),
        thinking=bool(cfg.llm.get("thinking", True)),
        api_key=cfg.api_key_for(provider),
    )
