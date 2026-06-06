"""Per-run checkpoint layer — the crash-resilience mechanism.

Two files per run, in outputs/<date>/<label>/:
  raw_jobs.json    — THE metadata file: all scraped listings, written ONCE after a
                     successful scrape. On any retry, if this exists the COLLECT
                     phase is skipped entirely (no re-scraping).
  run_state.json   — mutable progress: which phases completed, the deduped set of
                     new job ids, and per-job sub-step completion (scored/cv/cover/
                     outreach) so a retry resumes from the first incomplete job.

The whole working set still lives in RAM during a run; these files are flushed at
phase boundaries so a hard crash can resume cleanly.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ..config import OUTPUTS_DIR
from ..models import Job
from ..utils import read_json, write_json

PHASES = ["collect", "stage", "dedupe", "score", "generate", "summarize"]


def make_run_id(run_type: str, tz: str) -> tuple[str, str, Path]:
    """Return (run_id, label, run_dir) for a new or resumed run.

    run_type: 'afternoon' | 'night' | 'manual'. The id encodes the date + label so
    the same scheduled slot resumes the same folder within the day.
    """
    now = datetime.now(ZoneInfo(tz))
    date = now.strftime("%Y-%m-%d")
    label = {
        "afternoon": "afternoon_2pm",
        "night": "night_10pm",
    }.get(run_type, f"{run_type}_{now.strftime('%H%M')}")  # manual_HHMM, rescore_HHMM, …
    run_id = f"{date}_{label}"
    run_dir = OUTPUTS_DIR / date / label
    return run_id, label, run_dir


class RunState:
    def __init__(self, run_id: str, run_dir: Path):
        self.run_id = run_id
        self.run_dir = run_dir
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.raw_path = run_dir / "raw_jobs.json"
        self.state_path = run_dir / "run_state.json"
        self._state = self._load_state()

    def _load_state(self) -> dict:
        if self.state_path.exists():
            return read_json(self.state_path)
        return {
            "run_id": self.run_id,
            "phases": {},          # phase -> "done"
            "new_job_ids": None,   # set by dedupe
            "jobs": {},            # job_id -> {scored, cv, cover, outreach}
            "stats": {},
        }

    def _flush(self) -> None:
        write_json(self.state_path, self._state)

    # ── phases ──
    def phase_done(self, phase: str) -> bool:
        return self._state["phases"].get(phase) == "done"

    def mark_phase(self, phase: str) -> None:
        self._state["phases"][phase] = "done"
        self._flush()

    # ── raw_jobs.json (the metadata source of truth) ──
    def raw_exists(self) -> bool:
        return self.raw_path.exists() and self.phase_done("collect")

    def save_raw_jobs(self, jobs: list[Job]) -> None:
        write_json(self.raw_path, [j.to_dict() for j in jobs])

    def load_raw_jobs(self) -> list[Job]:
        return [Job.from_dict(d) for d in read_json(self.raw_path)]

    # ── dedupe result ──
    def set_new_job_ids(self, ids: list[str]) -> None:
        self._state["new_job_ids"] = ids
        self._flush()

    def get_new_job_ids(self) -> list[str] | None:
        return self._state.get("new_job_ids")

    # ── per-job sub-step progress ──
    def job_step_done(self, job_id: str, step: str) -> bool:
        return self._state["jobs"].get(job_id, {}).get(step, False)

    def mark_job_step(self, job_id: str, step: str, value=True) -> None:
        self._state["jobs"].setdefault(job_id, {})[step] = value
        self._flush()

    def get_job_record(self, job_id: str) -> dict:
        return self._state["jobs"].get(job_id, {})

    # ── stats ──
    def update_stats(self, **kw) -> None:
        self._state["stats"].update(kw)
        self._flush()

    @property
    def stats(self) -> dict:
        return self._state["stats"]
