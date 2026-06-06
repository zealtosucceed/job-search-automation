"""Groq adapter — fast, free-tier-friendly inference.

Same LLMAdapter interface as the other providers, so business logic is unchanged.
Groq's chat API mirrors OpenAI's. JSON handling stays in the provider-agnostic
base.complete_json (prompt-instructed JSON + parse + retry).
"""

from __future__ import annotations

from .base import LLMAdapter, log


class GroqAdapter(LLMAdapter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        try:
            from groq import Groq
        except ImportError as e:  # pragma: no cover
            raise ImportError("Run `pip install groq` to use the Groq provider.") from e
        if not self.api_key:
            raise ValueError("GROQ_API_KEY is not set (add it to .env).")
        # The SDK retries 429/5xx with exponential backoff.
        self._client = Groq(api_key=self.api_key, max_retries=3)

    def complete(self, prompt: str, system: str | None = None) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        resp = self._client.chat.completions.create(
            model=self.model,
            max_tokens=self.max_tokens,
            messages=messages,
        )
        text = (resp.choices[0].message.content or "").strip()
        if not text:
            log.warning("Empty Groq response (finish_reason=%s)",
                        resp.choices[0].finish_reason)
        return text
