"""Plain dataclasses passed through the in-memory pipeline.

These are the runtime working set — held in RAM during a run and serialized to
the per-run checkpoint files (raw_jobs.json) and the SQLite ledger.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class Job:
    """A normalized job listing. Mirrors the PRD's job record schema."""
    source: str                       # "EthicalJobs" | "LinkedIn"
    job_title: str
    company: str
    job_url: str
    location: str = ""
    job_type: str = ""
    work_mode: str = ""
    date_posted: str = ""
    application_deadline: str | None = None
    description: str = ""
    responsibilities: list[str] = field(default_factory=list)
    requirements: list[str] = field(default_factory=list)
    preferred_skills: list[str] = field(default_factory=list)
    salary: str | None = None
    recruiter_name: str | None = None
    recruiter_profile_url: str | None = None
    hiring_team_contacts: list[dict] = field(default_factory=list)

    # Pipeline state (not from the source)
    status: str = "new"               # new|shortlisted|rejected|needs_review|applied|saved|error
    job_id: str = ""                  # stable hash, assigned at staging

    def compute_id(self) -> str:
        """Stable per-job id from the canonical URL (used as dedup + filesystem key)."""
        key = (self.job_url or f"{self.company}|{self.job_title}|{self.location}").strip().lower()
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]

    def dedup_signature(self) -> str:
        """Secondary dedup key: company + title + location (normalized)."""
        sig = f"{self.company}|{self.job_title}|{self.location}".strip().lower()
        return hashlib.sha256(sig.encode("utf-8")).hexdigest()[:16]

    def folder_name(self) -> str:
        """Filesystem-safe per-job folder, e.g. ABC_HR_Operations_Associate."""
        def clean(s: str) -> str:
            keep = "".join(c if c.isalnum() or c in " -_" else " " for c in s)
            return "_".join(keep.split())[:60]
        return f"{clean(self.company)}_{clean(self.job_title)}".strip("_") or self.job_id

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Job":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class FitAnalysis:
    """Output of the LLM fit scorer (PRD Feature 5)."""
    job_id: str
    overall_score: float
    decision: str                     # shortlist | reject | needs_review
    reason: str = ""
    matched_skills: list[str] = field(default_factory=list)
    missing_skills: list[str] = field(default_factory=list)
    resume_strengths_for_this_role: list[str] = field(default_factory=list)
    risk_factors: list[str] = field(default_factory=list)
    recommended_cv_angle: str = ""
    recommended_cover_letter_angle: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_llm(cls, job_id: str, d: dict[str, Any]) -> "FitAnalysis":
        return cls(
            job_id=job_id,
            overall_score=float(d.get("overall_score", 0) or 0),
            decision=str(d.get("decision", "reject")),
            reason=d.get("reason", ""),
            matched_skills=d.get("matched_skills", []) or [],
            missing_skills=d.get("missing_skills", []) or [],
            resume_strengths_for_this_role=d.get("resume_strengths_for_this_role", []) or [],
            risk_factors=d.get("risk_factors", []) or [],
            recommended_cv_angle=d.get("recommended_cv_angle", ""),
            recommended_cover_letter_angle=d.get("recommended_cover_letter_angle", ""),
        )
