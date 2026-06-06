"""Provider-agnostic LLM interface.

Business logic (scorer, document generator, resume parser) only ever sees this
interface and the JSON contract — never a vendor SDK. Adding a provider means
adding one adapter class + one registry line.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ...utils import extract_json, get_logger

log = get_logger("llm")


class LLMAdapter(ABC):
    """Base class for all providers."""

    def __init__(self, model: str, max_tokens: int = 8000, thinking: bool = True,
                 api_key: str | None = None):
        self.model = model
        self.max_tokens = max_tokens
        self.thinking = thinking
        self.api_key = api_key

    @abstractmethod
    def complete(self, prompt: str, system: str | None = None) -> str:
        """Return raw text for a single-shot completion."""

    def complete_json(self, prompt: str, system: str | None = None,
                      retries: int = 1) -> dict | list:
        """Complete and parse JSON, retrying once on invalid JSON.

        Centralizes the PRD's "retryable if invalid JSON" rule so every caller
        gets it for free, regardless of provider.
        """
        last_err: Exception | None = None
        attempt_prompt = prompt
        for attempt in range(retries + 1):
            text = self.complete(attempt_prompt, system=system)
            try:
                return extract_json(text)
            except ValueError as e:
                last_err = e
                log.warning("Invalid JSON from %s (attempt %d/%d): %s",
                            self.model, attempt + 1, retries + 1, e)
                attempt_prompt = (
                    prompt
                    + "\n\nIMPORTANT: Your previous reply was not valid JSON. "
                    "Return ONLY a single valid JSON value, no prose, no code fences."
                )
        raise ValueError(f"LLM did not return valid JSON after {retries + 1} attempts: {last_err}")
