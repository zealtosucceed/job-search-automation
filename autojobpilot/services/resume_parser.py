"""Master resume management (PRD Feature 1).

Flow: PDF (or DOCX/TXT) -> raw text (PyMuPDF) -> LLM -> resume.json.
The original master file is copied into resumes/ and never overwritten;
every customized CV is a separate file generated downstream.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .. import prompts
from ..config import RESUME_JSON_PATH, RESUMES_DIR
from ..utils import get_logger, read_json, write_json
from .llm.base import LLMAdapter

log = get_logger("app")


def extract_text(path: Path) -> str:
    """Extract plain text from a PDF/DOCX/TXT resume."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        import fitz  # PyMuPDF
        with fitz.open(path) as doc:
            return "\n".join(page.get_text() for page in doc)
    if suffix == ".docx":
        import docx
        d = docx.Document(str(path))
        return "\n".join(p.text for p in d.paragraphs)
    if suffix == ".txt":
        return path.read_text(encoding="utf-8", errors="ignore")
    raise ValueError(f"Unsupported resume format: {suffix} (use PDF, DOCX, or TXT)")


def ingest_resume(uploaded_path: Path, llm: LLMAdapter) -> dict:
    """Parse a master resume and persist resume.json.

    The original is preserved in resumes/master<ext>. Returns the parsed dict.
    """
    uploaded_path = Path(uploaded_path)
    RESUMES_DIR.mkdir(parents=True, exist_ok=True)
    master_copy = RESUMES_DIR / f"master{uploaded_path.suffix.lower()}"
    if uploaded_path.resolve() != master_copy.resolve():
        shutil.copy2(uploaded_path, master_copy)

    text = extract_text(master_copy)
    if not text.strip():
        raise ValueError("Could not extract any text from the resume.")

    log.info("Parsing resume (%d chars) via LLM", len(text))
    prompt = prompts.render("resume_parser", resume_text=text)
    parsed = llm.complete_json(prompt)

    # Keep the raw text alongside the structured data for downstream scoring/generation.
    if isinstance(parsed, dict):
        parsed["_raw_text"] = text
        parsed["_master_file"] = str(master_copy)
    write_json(RESUME_JSON_PATH, parsed)
    log.info("Saved parsed resume -> %s", RESUME_JSON_PATH)
    return parsed


def load_resume() -> dict | None:
    if RESUME_JSON_PATH.exists():
        return read_json(RESUME_JSON_PATH)
    return None


def resume_text(parsed: dict) -> str:
    """Return the best available plain-text form of the resume for prompts."""
    if parsed.get("_raw_text"):
        return parsed["_raw_text"]
    # Fallback: stringify the structured fields.
    import json
    return json.dumps({k: v for k, v in parsed.items() if not k.startswith("_")}, indent=2)
