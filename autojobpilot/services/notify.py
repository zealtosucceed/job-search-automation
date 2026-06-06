"""Run summary + notification (PRD Features 11, 14).

Writes summary_report.md and shortlisted_jobs.csv into the run folder. Email is
a future option; default is a local markdown report.
"""

from __future__ import annotations

import csv
from pathlib import Path

from ..utils import get_logger

log = get_logger("app")


def write_summary(run_dir: Path, run_id: str, stats: dict,
                  shortlisted: list[dict]) -> Path:
    """shortlisted: list of {company, role, score, location, source, files} dicts."""
    md = run_dir / "summary_report.md"
    lines = [
        f"# AutoJobPilot Run Summary — {run_id}",
        "",
        f"- Sources checked: {stats.get('sources', '')}",
        f"- Total jobs found: {stats.get('total', 0)}",
        f"- New jobs: {stats.get('new', 0)}",
        f"- Shortlisted: {stats.get('shortlisted', 0)}",
        f"- Needs review: {stats.get('needs_review', 0)}",
        f"- Rejected: {stats.get('rejected', 0)}",
        f"- CVs generated: {stats.get('cvs', 0)}",
        f"- Cover letters generated: {stats.get('cover_letters', 0)}",
        f"- LinkedIn messages generated: {stats.get('messages', 0)}",
        "",
        "## Shortlisted jobs",
        "",
        "| Company | Role | Score | Location | Source | Files |",
        "|---|---|---|---|---|---|",
    ]
    for j in shortlisted:
        lines.append(
            f"| {j.get('company','')} | {j.get('role','')} | {j.get('score','')} "
            f"| {j.get('location','')} | {j.get('source','')} | {j.get('files','')} |"
        )
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # CSV export (PRD: shortlisted_jobs.csv).
    csv_path = run_dir / "shortlisted_jobs.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["company", "role", "score", "location", "source", "job_url", "folder"])
        for j in shortlisted:
            w.writerow([j.get("company", ""), j.get("role", ""), j.get("score", ""),
                        j.get("location", ""), j.get("source", ""),
                        j.get("job_url", ""), j.get("folder", "")])
    log.info("Summary written: %s", md)
    return md
