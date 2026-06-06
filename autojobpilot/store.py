"""SQLite ledger — the cross-run source of truth for dedup, status, and history.

Single embedded file (data/app.db), no server. Three tables instead of the PRD's
seven, since this is single-user and resume/preferences live as files on disk:
  jobs      — one row per unique job (dedup keys + status + recruiter)
  analysis  — fit score + verdict per job
  runs      — per-cycle run history
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator

from .config import DB_PATH
from .models import FitAnalysis, Job

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id          TEXT PRIMARY KEY,
    dedup_signature TEXT,
    source          TEXT,
    job_title       TEXT,
    company         TEXT,
    location        TEXT,
    job_type        TEXT,
    work_mode       TEXT,
    date_posted     TEXT,
    application_deadline TEXT,
    job_url         TEXT UNIQUE,
    description     TEXT,
    recruiter_name  TEXT,
    recruiter_profile_url TEXT,
    salary          TEXT,
    status          TEXT DEFAULT 'new',
    first_seen_run  TEXT,
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_jobs_sig ON jobs (dedup_signature);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (status);

CREATE TABLE IF NOT EXISTS analysis (
    job_id        TEXT PRIMARY KEY,
    score         REAL,
    decision      TEXT,
    matched_skills TEXT,
    missing_skills TEXT,
    strengths     TEXT,
    risks         TEXT,
    analysis_json TEXT,
    created_at    TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS runs (
    run_id            TEXT PRIMARY KEY,
    run_type          TEXT,
    status            TEXT,
    started_at        TEXT,
    ended_at          TEXT,
    total_jobs_found  INTEGER DEFAULT 0,
    new_jobs          INTEGER DEFAULT 0,
    shortlisted_jobs  INTEGER DEFAULT 0,
    rejected_jobs     INTEGER DEFAULT 0,
    needs_review_jobs INTEGER DEFAULT 0,
    attempts          INTEGER DEFAULT 0,
    errors            TEXT
);
"""


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as con:
        con.executescript(_SCHEMA)


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


# ── Dedup ──────────────────────────────────────────────────────────────────
def is_known(job: Job) -> bool:
    """True if this job already exists (by id, url, or company+title+location)."""
    with _connect() as con:
        row = con.execute(
            "SELECT 1 FROM jobs WHERE job_id = ? OR job_url = ? OR dedup_signature = ? LIMIT 1",
            (job.job_id or job.compute_id(), job.job_url, job.dedup_signature()),
        ).fetchone()
    return row is not None


def has_analysis(job_id: str) -> bool:
    """True only if this job was SUCCESSFULLY scored (has an analysis row).

    This is the real dedup key: a job that was merely seen but never scored
    (e.g. scoring 429'd on a rate limit, leaving status='error') must NOT be
    treated as a duplicate — it should be retried on the next run.
    """
    with _connect() as con:
        row = con.execute(
            "SELECT 1 FROM analysis WHERE job_id = ? LIMIT 1", (job_id,)
        ).fetchone()
    return row is not None


def unscored_jobs() -> list[Job]:
    """Reconstruct Job objects for every ledger row that has no analysis yet.

    Descriptions are stored in the jobs table, so these can be re-scored in place
    without re-scraping.
    """
    with _connect() as con:
        rows = con.execute(
            "SELECT j.* FROM jobs j LEFT JOIN analysis a ON a.job_id = j.job_id "
            "WHERE a.job_id IS NULL ORDER BY j.created_at"
        ).fetchall()
    jobs: list[Job] = []
    for r in rows:
        job = Job(
            source=r["source"] or "", job_title=r["job_title"] or "",
            company=r["company"] or "", job_url=r["job_url"] or "",
            location=r["location"] or "", job_type=r["job_type"] or "",
            work_mode=r["work_mode"] or "", date_posted=r["date_posted"] or "",
            application_deadline=r["application_deadline"], description=r["description"] or "",
            recruiter_name=r["recruiter_name"], recruiter_profile_url=r["recruiter_profile_url"],
            salary=r["salary"], status=r["status"] or "new",
        )
        job.job_id = r["job_id"]
        jobs.append(job)
    return jobs


def _coerce(v):
    """Make any scraped value safe to bind to a TEXT column. schema.org JSON-LD
    sometimes yields lists/dicts (e.g. employmentType=['FULL_TIME'])."""
    if v is None or isinstance(v, (str, int, float)):
        return v
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v)
    return str(v)


# ── Jobs ───────────────────────────────────────────────────────────────────
def upsert_job(job: Job, run_id: str) -> None:
    with _connect() as con:
        con.execute(
            """
            INSERT INTO jobs (job_id, dedup_signature, source, job_title, company, location,
                job_type, work_mode, date_posted, application_deadline, job_url, description,
                recruiter_name, recruiter_profile_url, salary, status, first_seen_run)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(job_id) DO UPDATE SET
                status=excluded.status,
                recruiter_name=COALESCE(excluded.recruiter_name, jobs.recruiter_name),
                recruiter_profile_url=COALESCE(excluded.recruiter_profile_url, jobs.recruiter_profile_url),
                updated_at=datetime('now')
            """,
            (
                job.job_id or job.compute_id(), job.dedup_signature(), job.source,
                _coerce(job.job_title), _coerce(job.company), _coerce(job.location),
                _coerce(job.job_type), _coerce(job.work_mode), _coerce(job.date_posted),
                _coerce(job.application_deadline), job.job_url, _coerce(job.description),
                _coerce(job.recruiter_name), _coerce(job.recruiter_profile_url),
                _coerce(job.salary), job.status, run_id,
            ),
        )


def set_status(job_id: str, status: str) -> None:
    with _connect() as con:
        con.execute(
            "UPDATE jobs SET status = ?, updated_at = datetime('now') WHERE job_id = ?",
            (status, job_id),
        )


def list_jobs(status: str | None = None) -> list[dict]:
    q = "SELECT j.*, a.score, a.decision FROM jobs j LEFT JOIN analysis a ON a.job_id = j.job_id"
    params: tuple = ()
    if status:
        q += " WHERE j.status = ?"
        params = (status,)
    q += " ORDER BY j.created_at DESC"
    with _connect() as con:
        return [dict(r) for r in con.execute(q, params).fetchall()]


# ── Analysis ───────────────────────────────────────────────────────────────
def save_analysis(a: FitAnalysis) -> None:
    with _connect() as con:
        con.execute(
            """
            INSERT INTO analysis (job_id, score, decision, matched_skills, missing_skills,
                strengths, risks, analysis_json)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(job_id) DO UPDATE SET
                score=excluded.score, decision=excluded.decision,
                matched_skills=excluded.matched_skills, missing_skills=excluded.missing_skills,
                strengths=excluded.strengths, risks=excluded.risks,
                analysis_json=excluded.analysis_json
            """,
            (
                a.job_id, a.overall_score, a.decision,
                json.dumps(a.matched_skills), json.dumps(a.missing_skills),
                json.dumps(a.resume_strengths_for_this_role), json.dumps(a.risk_factors),
                json.dumps(a.to_dict()),
            ),
        )


# ── Runs ───────────────────────────────────────────────────────────────────
def create_run(run_id: str, run_type: str) -> None:
    with _connect() as con:
        con.execute(
            "INSERT OR IGNORE INTO runs (run_id, run_type, status, started_at) VALUES (?,?,?,?)",
            (run_id, run_type, "running", datetime.now().isoformat(timespec="seconds")),
        )


def finish_run(run_id: str, status: str, stats: dict, attempts: int, errors: list[str]) -> None:
    with _connect() as con:
        con.execute(
            """
            UPDATE runs SET status=?, ended_at=?, total_jobs_found=?, new_jobs=?,
                shortlisted_jobs=?, rejected_jobs=?, needs_review_jobs=?, attempts=?, errors=?
            WHERE run_id=?
            """,
            (
                status, datetime.now().isoformat(timespec="seconds"),
                stats.get("total", 0), stats.get("new", 0), stats.get("shortlisted", 0),
                stats.get("rejected", 0), stats.get("needs_review", 0),
                attempts, json.dumps(errors), run_id,
            ),
        )


def list_runs(limit: int = 30) -> list[dict]:
    with _connect() as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()]
