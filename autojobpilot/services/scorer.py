"""Resume-to-job fit scoring (PRD Feature 5) + shortlisting (Feature 6)."""

from __future__ import annotations

import json

from .. import prompts
from ..models import FitAnalysis, Job
from ..utils import get_logger
from .llm.base import LLMAdapter

log = get_logger("llm")


def job_description_text(job: Job) -> str:
    """Compose a single description string from the job's fields for the prompt."""
    parts = [
        f"Title: {job.job_title}",
        f"Company: {job.company}",
        f"Location: {job.location}",
        f"Work mode: {job.work_mode}",
        f"Type: {job.job_type}",
        "",
        job.description,
    ]
    if job.requirements:
        parts += ["", "Requirements:", *[f"- {r}" for r in job.requirements]]
    if job.preferred_skills:
        parts += ["", "Preferred skills:", *[f"- {s}" for s in job.preferred_skills]]
    return "\n".join(p for p in parts if p is not None)


def score_job(job: Job, resume_text: str, llm: LLMAdapter) -> FitAnalysis:
    prompt = prompts.render(
        "fit_scorer",
        resume_text=resume_text,
        job_description=job_description_text(job),
    )
    data = llm.complete_json(prompt)
    if not isinstance(data, dict):
        raise ValueError("fit scorer did not return a JSON object")
    return FitAnalysis.from_llm(job.compute_id(), data)


def decide(analysis: FitAnalysis, job: Job, cfg) -> str:
    """Apply shortlisting logic. Returns a status: shortlisted|needs_review|rejected."""
    minimum = float(cfg.search.get("minimum_score", 5))
    band = cfg.search.get("needs_review_band", [4.5, 5])
    excluded = [k.lower() for k in cfg.search.get("excluded_keywords", [])]

    haystack = f"{job.job_title} {job.description}".lower()
    if any(k in haystack for k in excluded):
        return "rejected"

    score = analysis.overall_score
    if score >= minimum:
        return "shortlisted"
    if len(band) == 2 and band[0] <= score < band[1]:
        return "needs_review"
    return "rejected"
