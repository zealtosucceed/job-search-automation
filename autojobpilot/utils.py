"""Small shared helpers: logging and JSON extraction."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from .config import LOGS_DIR

_CONFIGURED: set[str] = set()


def get_logger(name: str = "app") -> logging.Logger:
    """Return a logger writing to logs/<name>.log and stderr.

    Log file names match the PRD: app, scheduler, scraping, llm, document_generation.
    """
    logger = logging.getLogger(f"autojobpilot.{name}")
    if name in _CONFIGURED:
        return logger
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    fh = logging.FileHandler(LOGS_DIR / f"{name}.log", encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    logger.propagate = False
    _CONFIGURED.add(name)
    return logger


def extract_json(text: str) -> dict | list:
    """Best-effort parse of a JSON object/array out of an LLM response.

    Handles ```json fenced blocks and leading/trailing prose. Raises ValueError
    if no valid JSON can be recovered (the caller retries).
    """
    if not text:
        raise ValueError("empty LLM response")

    # Strip code fences if present.
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    candidate = fenced.group(1).strip() if fenced else text.strip()

    # Fast path.
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    # Fall back to the first balanced {...} or [...] span.
    for opener, closer in (("{", "}"), ("[", "]")):
        start = candidate.find(opener)
        end = candidate.rfind(closer)
        if start != -1 and end > start:
            snippet = candidate[start : end + 1]
            try:
                return json.loads(snippet)
            except json.JSONDecodeError:
                continue
    raise ValueError("no valid JSON found in LLM response")


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)


def read_json(path: Path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)
