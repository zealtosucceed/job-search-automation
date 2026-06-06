"""Anthropic / Claude adapter (default provider).

Uses the official anthropic SDK. The SDK auto-retries 429/5xx with exponential
backoff (max_retries), so we only add JSON-validity retry in the base class.
Adaptive thinking is enabled for reasoning-heavy calls; thinking text is not
read back (we only consume the final text block).
"""

from __future__ import annotations

from .base import LLMAdapter, log


class AnthropicAdapter(LLMAdapter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        try:
            import anthropic
        except ImportError as e:  # pragma: no cover
            raise ImportError("Run `pip install anthropic` to use the Anthropic provider.") from e
        if not self.api_key:
            raise ValueError("ANTHROPIC_API_KEY is not set (add it to .env).")
        # max_retries covers transient 429/5xx with the SDK's own backoff.
        self._client = anthropic.Anthropic(api_key=self.api_key, max_retries=3)

    def complete(self, prompt: str, system: str | None = None) -> str:
        kwargs: dict = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            kwargs["system"] = system
        if self.thinking:
            kwargs["thinking"] = {"type": "adaptive"}

        resp = self._client.messages.create(**kwargs)
        parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
        text = "".join(parts).strip()
        if not text:
            log.warning("Empty text response (stop_reason=%s)", getattr(resp, "stop_reason", "?"))
        return text
