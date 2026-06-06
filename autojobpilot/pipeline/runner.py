"""Run orchestration: phase sequencing, session-level retries with exponential
backoff, and resume-from-checkpoint.

A run executes COLLECT -> STAGE -> DEDUPE -> SCORE -> GENERATE -> SUMMARIZE.
If any phase raises, the whole attempt fails and is retried (up to `retries`)
after a backoff. Because every phase flushes its progress to run_state.json (and
the scrape result to raw_jobs.json), each retry RESUMES from the last completed
phase / job rather than starting over — no re-scraping, no re-scoring done work.
"""

from __future__ import annotations

import time

from .. import store
from ..config import Config, ensure_dirs
from ..services.llm.registry import get_llm
from ..services.resume_parser import load_resume, resume_text as resume_text_of
from ..utils import get_logger
from . import phases
from .phases import RunContext
from .state import RunState, make_run_id

log = get_logger("scheduler")

_PHASE_FNS = [
    ("collect", phases.collect),
    ("stage", phases.stage),
    ("dedupe", phases.dedupe),
    ("score", phases.score),
    ("generate", phases.generate),
    ("summarize", phases.summarize),
]


def run_cycle(cfg: Config, run_type: str = "manual") -> dict:
    """Execute one full run cycle. Returns the final stats dict.

    run_type: 'afternoon' | 'night' | 'manual'.
    """
    ensure_dirs()
    store.init_db()

    parsed_resume = load_resume()
    if not parsed_resume:
        raise RuntimeError("No master resume found. Upload one in the dashboard first.")
    rtext = resume_text_of(parsed_resume)

    tz = cfg.schedule.get("timezone", "Australia/Sydney")
    run_id, _label, run_dir = make_run_id(run_type, tz)
    state = RunState(run_id, run_dir)
    store.create_run(run_id, run_type)

    retries = int(cfg.schedule.get("retries", 3))
    backoffs = list(cfg.schedule.get("backoff_seconds", [30, 120, 480]))
    max_attempts = retries  # `retries` total attempts

    errors: list[str] = []
    for attempt in range(1, max_attempts + 1):
        log.info("=== Run %s — attempt %d/%d ===", run_id, attempt, max_attempts)
        ctx = RunContext(cfg=cfg, llm=get_llm(cfg), state=state, run_id=run_id, resume_text=rtext)
        try:
            for name, fn in _PHASE_FNS:
                if state.phase_done(name):
                    log.info("[%s] already complete — skipping", name)
                    # Still re-hydrate the working set the later phases need.
                    if name == "collect" and not ctx.jobs:
                        ctx.jobs = state.load_raw_jobs()
                    continue
                fn(ctx)
            errors += ctx.errors
            store.finish_run(run_id, "success", state.stats, attempt, errors)
            log.info("=== Run %s complete: %s ===", run_id, state.stats)
            return state.stats
        except Exception as e:
            errors += ctx.errors
            errors.append(f"attempt {attempt}: {e!r}")
            log.exception("Run %s attempt %d failed", run_id, attempt)
            if attempt < max_attempts:
                delay = backoffs[min(attempt - 1, len(backoffs) - 1)]
                log.info("Retrying in %ds (resume from checkpoint)…", delay)
                store.finish_run(run_id, "retrying", state.stats, attempt, errors)
                time.sleep(delay)
            else:
                store.finish_run(run_id, "failed", state.stats, attempt, errors)
                log.error("Run %s failed after %d attempts", run_id, max_attempts)
                raise

    return state.stats
