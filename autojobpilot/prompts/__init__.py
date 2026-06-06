"""Prompt template loading + rendering (simple {{var}} substitution)."""

from __future__ import annotations

from pathlib import Path

_DIR = Path(__file__).resolve().parent
_cache: dict[str, str] = {}


def load(name: str) -> str:
    if name not in _cache:
        _cache[name] = (_DIR / f"{name}.txt").read_text(encoding="utf-8")
    return _cache[name]


def render(name: str, **vars: str) -> str:
    """Render a template, replacing each {{key}} with str(value)."""
    text = load(name)
    for key, val in vars.items():
        text = text.replace("{{" + key + "}}", str(val))
    return text
