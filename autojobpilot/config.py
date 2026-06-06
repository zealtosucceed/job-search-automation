"""Loads the single master config (config.yaml) + secrets from .env.

Everything tunable lives in config.yaml. Secrets stay in environment variables
(loaded from .env) and are never written into the config object's serialized form.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# Repo root = parent of the autojobpilot package directory.
ROOT = Path(__file__).resolve().parent.parent

DATA_DIR = ROOT / "data"
OUTPUTS_DIR = ROOT / "outputs"
LOGS_DIR = ROOT / "logs"
RESUMES_DIR = ROOT / "resumes"
DB_PATH = DATA_DIR / "app.db"
RESUME_JSON_PATH = DATA_DIR / "resume.json"

_REQUIRED_SECTIONS = ("profile", "search", "sources", "schedule", "llm", "output")


class Config:
    """Thin attribute wrapper over the parsed config.yaml dict.

    Access sections as attributes returning plain dicts, e.g. ``cfg.search["minimum_score"]``.
    Secrets are exposed via dedicated properties that read the environment.
    """

    def __init__(self, data: dict[str, Any], path: Path):
        self._d = data
        self.path = path
        missing = [s for s in _REQUIRED_SECTIONS if s not in data]
        if missing:
            raise ValueError(f"config.yaml missing required sections: {missing}")

    # --- sections ---
    @property
    def profile(self) -> dict: return self._d["profile"]

    @property
    def search(self) -> dict: return self._d["search"]

    @property
    def sources(self) -> dict: return self._d["sources"]

    @property
    def schedule(self) -> dict: return self._d["schedule"]

    @property
    def llm(self) -> dict: return self._d["llm"]

    @property
    def output(self) -> dict: return self._d["output"]

    # --- secrets (from env, never from yaml) ---
    @property
    def anthropic_api_key(self) -> str | None:
        return os.environ.get("ANTHROPIC_API_KEY")

    @property
    def openai_api_key(self) -> str | None:
        return os.environ.get("OPENAI_API_KEY")

    @property
    def groq_api_key(self) -> str | None:
        return os.environ.get("GROQ_API_KEY")

    def api_key_for(self, provider: str) -> str | None:
        return {
            "anthropic": self.anthropic_api_key,
            "openai": self.openai_api_key,
            "groq": self.groq_api_key,
            "ollama": None,  # local, no key
        }.get(provider)

    def raw(self) -> dict:
        return self._d


def ensure_dirs() -> None:
    """Create the runtime directories (idempotent)."""
    for d in (DATA_DIR, OUTPUTS_DIR, LOGS_DIR, RESUMES_DIR):
        d.mkdir(parents=True, exist_ok=True)


def load_config(path: Path | str | None = None) -> Config:
    """Load .env then config.yaml and return a validated Config."""
    load_dotenv(ROOT / ".env")
    cfg_path = Path(path) if path else (ROOT / "config.yaml")
    if not cfg_path.exists():
        raise FileNotFoundError(
            f"{cfg_path} not found. Copy config.example.yaml to config.yaml and edit it."
        )
    with cfg_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    ensure_dirs()
    return Config(data, cfg_path)


def save_config(cfg: Config) -> None:
    """Persist the (possibly dashboard-edited) config back to disk."""
    with cfg.path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(cfg.raw(), f, sort_keys=False, allow_unicode=True)
