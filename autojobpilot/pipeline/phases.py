"""The six idempotent, resumable pipeline phases.

Each phase reads/writes the RunState checkpoint so a retry resumes from where it
left off. None of them re-scrape if raw_jobs.json already exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .. import store
from ..collectors.ethicaljobs import EthicalJobsCollector
from ..collectors.linkedin import LinkedInCollector
from ..config import Config
from ..models import FitAnalysis, Job
from ..services import documents, notify, scorer
from ..services.llm.base import LLMAdapter
from ..services.resume_parser import load_resume, resume_text as resume_text_of
from ..utils import get_logger, read_json
from .state import RunState

log = get_logger("app")


@dataclass
class RunContext:
    cfg: Config
    llm: LLMAdapter
    state: RunState
    run_id: str
    resume_text: str
    jobs: list[Job] = field(default_factory=list)       # working set (in RAM)
    new_jobs: list[Job] = field(default_factory=list)
    shortlisted_rows: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ── 1. COLLECT ──────────────────────────────────────────────────────────────
def collect(ctx: RunContext) -> None:
    """Scrape sources -> raw_jobs.json. Skipped entirely on retry if already done."""
    if ctx.state.raw_exists():
        log.info("[collect] raw_jobs.json present — skipping scrape (resuming).")
        ctx.jobs = ctx.state.load_raw_jobs()
        return

    collected: list[Job] = []
    sources_used: list[str] = []
    src_cfg = ctx.cfg.sources

    if src_cfg.get("ethicaljobs", {}).get("enabled"):
        try:
            collected += EthicalJobsCollector(ctx.cfg).collect()
            sources_used.append("EthicalJobs")
        except Exception as e:  # one source failing must not kill the run
            log.warning("[collect] EthicalJobs failed: %s", e)
            ctx.errors.append(f"EthicalJobs collect: {e}")

    if src_cfg.get("linkedin", {}).get("enabled"):
        try:
            collected += LinkedInCollector(ctx.cfg).collect()
            sources_used.append("LinkedIn")
        except Exception as e:
            log.warning("[collect] LinkedIn failed: %s", e)
            ctx.errors.append(f"LinkedIn collect: {e}")

    # Assign stable ids before staging.
    for j in collected:
        j.job_id = j.compute_id()

    ctx.jobs = collected
    ctx.state.update_stats(sources=", ".join(sources_used), total=len(collected))
    ctx.state.mark_phase("collect")  # mark BEFORE saving so raw_exists() is truthful only after save
    ctx.state.save_raw_jobs(collected)
    log.info("[collect] %d jobs from %s", len(collected), sources_used)


# ── 2. STAGE ────────────────────────────────────────────────────────────────
def stage(ctx: RunContext) -> None:
    """Load the source-of-truth metadata file into the working set."""
    if not ctx.jobs:
        ctx.jobs = ctx.state.load_raw_jobs()
    ctx.state.mark_phase("stage")
    log.info("[stage] %d jobs staged from raw_jobs.json", len(ctx.jobs))


# ── 3. DEDUPE ───────────────────────────────────────────────────────────────
def dedupe(ctx: RunContext) -> None:
    """Determine which staged jobs are NEW (not in the cross-run ledger)."""
    cached = ctx.state.get_new_job_ids()
    by_id = {j.job_id: j for j in ctx.jobs}
    if cached is not None:
        ctx.new_jobs = [by_id[i] for i in cached if i in by_id]
        log.info("[dedupe] resumed: %d new jobs", len(ctx.new_jobs))
        return

    new_ids: list[str] = []
    seen_this_run: set[str] = set()
    for j in ctx.jobs:
        if j.job_id in seen_this_run or j.dedup_signature() in seen_this_run:
            continue
        seen_this_run.add(j.job_id)
        seen_this_run.add(j.dedup_signature())
        # Process the job unless it was already SUCCESSFULLY scored. Jobs that were
        # merely seen but errored (e.g. rate-limited) have no analysis and are retried.
        if not store.has_analysis(j.job_id):
            new_ids.append(j.job_id)
            store.upsert_job(j, ctx.run_id)  # record/refresh in the ledger

    ctx.new_jobs = [by_id[i] for i in new_ids]
    ctx.state.set_new_job_ids(new_ids)
    ctx.state.update_stats(new=len(new_ids))
    ctx.state.mark_phase("dedupe")
    log.info("[dedupe] %d to process (of %d staged; rest already scored)",
             len(new_ids), len(ctx.jobs))


# ── 4. SCORE ────────────────────────────────────────────────────────────────
def score(ctx: RunContext) -> None:
    """Fit-score each new job; resumable per job."""
    if not ctx.new_jobs:
        ctx.new_jobs = _resolve_new_jobs(ctx)
    shortlisted = needs_review = rejected = 0

    for job in ctx.new_jobs:
        if ctx.state.job_step_done(job.job_id, "scored"):
            continue  # already scored in a prior attempt; decision recounted below
        try:
            analysis = scorer.score_job(job, ctx.resume_text, ctx.llm)
            decision = scorer.decide(analysis, job, ctx.cfg)
            job.status = decision
            store.save_analysis(analysis)
            store.set_status(job.job_id, decision)
            _persist_job_artifacts(ctx, job, analysis)
            ctx.state.mark_job_step(job.job_id, "scored")
            ctx.state.mark_job_step(job.job_id, "decision", decision)
        except Exception as e:
            log.warning("[score] job %s failed: %s", job.job_id, e)
            store.set_status(job.job_id, "error")
            ctx.errors.append(f"score {job.job_id}: {e}")

    # Recount from the ledger for accuracy across resumes.
    for job in ctx.new_jobs:
        d = ctx.state.get_job_record(job.job_id).get("decision")
        if d == "shortlisted":
            shortlisted += 1
        elif d == "needs_review":
            needs_review += 1
        elif d == "rejected":
            rejected += 1
    ctx.state.update_stats(shortlisted=shortlisted, needs_review=needs_review, rejected=rejected)
    ctx.state.mark_phase("score")
    log.info("[score] shortlisted=%d needs_review=%d rejected=%d",
             shortlisted, needs_review, rejected)


# ── 5. GENERATE ─────────────────────────────────────────────────────────────
def generate(ctx: RunContext) -> None:
    """Generate CV + cover letter (+ outreach) for shortlisted jobs; resumable."""
    if not ctx.new_jobs:
        ctx.new_jobs = _resolve_new_jobs(ctx)
    cvs = covers = msgs = 0

    for job in ctx.new_jobs:
        decision = ctx.state.get_job_record(job.job_id).get("decision")
        if decision != "shortlisted":
            continue
        out_dir = ctx.state.run_dir / job.folder_name()
        out_dir.mkdir(parents=True, exist_ok=True)
        analysis = _load_analysis(ctx, job, out_dir)

        # Each document is generated independently — one failing must NOT drop the
        # job from the shortlist/CSV (the job is still a valid shortlist).
        if not ctx.state.job_step_done(job.job_id, "cv"):
            try:
                documents.generate_cv(job, ctx.resume_text, analysis, out_dir, ctx.llm)
                ctx.state.mark_job_step(job.job_id, "cv")
            except Exception as e:
                log.warning("[generate] CV failed for %s: %s", job.job_id, e)
                ctx.errors.append(f"cv {job.job_id}: {e}")

        if not ctx.state.job_step_done(job.job_id, "cover"):
            try:
                documents.generate_cover_letter(job, ctx.resume_text, analysis, out_dir, ctx.llm)
                ctx.state.mark_job_step(job.job_id, "cover")
            except Exception as e:
                log.warning("[generate] cover letter failed for %s: %s", job.job_id, e)
                ctx.errors.append(f"cover {job.job_id}: {e}")

        if not ctx.state.job_step_done(job.job_id, "outreach"):
            try:
                res = documents.generate_outreach(job, ctx.resume_text, out_dir, ctx.llm)
                ctx.state.mark_job_step(job.job_id, "outreach", bool(res))
            except Exception as e:
                log.warning("[generate] outreach failed for %s: %s", job.job_id, e)
                ctx.errors.append(f"outreach {job.job_id}: {e}")

        rec = ctx.state.get_job_record(job.job_id)
        made = []
        if rec.get("cv"):
            made.append("CV"); cvs += 1
        if rec.get("cover"):
            made.append("Cover"); covers += 1
        if rec.get("outreach"):
            made.append("Msg"); msgs += 1

        # Always record the shortlist, regardless of which docs succeeded.
        ctx.shortlisted_rows.append({
            "company": job.company, "role": job.job_title,
            "score": analysis.overall_score, "location": job.location,
            "source": job.source, "job_url": job.job_url,
            "folder": job.folder_name(), "files": ", ".join(made) or "(none)",
        })

    ctx.state.update_stats(cvs=cvs, cover_letters=covers, messages=msgs)
    ctx.state.mark_phase("generate")
    log.info("[generate] CVs=%d cover_letters=%d messages=%d", cvs, covers, msgs)


# ── 6. SUMMARIZE ────────────────────────────────────────────────────────────
def summarize(ctx: RunContext) -> None:
    if not ctx.shortlisted_rows:
        ctx.shortlisted_rows = _rebuild_shortlist_rows(ctx)
    notify.write_summary(ctx.state.run_dir, ctx.run_id, ctx.state.stats, ctx.shortlisted_rows)
    ctx.state.mark_phase("summarize")
    log.info("[summarize] report written for %s", ctx.run_id)


# ── helpers ─────────────────────────────────────────────────────────────────
def _resolve_new_jobs(ctx: RunContext) -> list[Job]:
    ids = set(ctx.state.get_new_job_ids() or [])
    jobs = ctx.jobs or ctx.state.load_raw_jobs()
    return [j for j in jobs if j.job_id in ids]


def _persist_job_artifacts(ctx: RunContext, job: Job, analysis: FitAnalysis) -> None:
    """Write job_details.json + fit_analysis.json into the per-job folder."""
    if job.status not in ("shortlisted", "needs_review"):
        return
    out_dir = ctx.state.run_dir / job.folder_name()
    out_dir.mkdir(parents=True, exist_ok=True)
    from ..utils import write_json
    write_json(out_dir / "job_details.json", job.to_dict())
    write_json(out_dir / "fit_analysis.json", analysis.to_dict())


def _load_analysis(ctx: RunContext, job: Job, out_dir: Path) -> FitAnalysis:  # noqa: F821
    fa = out_dir / "fit_analysis.json"
    if fa.exists():
        return FitAnalysis(**{k: v for k, v in read_json(fa).items()
                              if k in FitAnalysis.__dataclass_fields__})
    # Fall back to re-scoring if the artifact is missing.
    return scorer.score_job(job, ctx.resume_text, ctx.llm)


def _rebuild_shortlist_rows(ctx: RunContext) -> list[dict]:
    rows = []
    for job in _resolve_new_jobs(ctx):
        if ctx.state.get_job_record(job.job_id).get("decision") != "shortlisted":
            continue
        out_dir = ctx.state.run_dir / job.folder_name()
        score_val = ""
        fa = out_dir / "fit_analysis.json"
        if fa.exists():
            score_val = read_json(fa).get("overall_score", "")
        rows.append({
            "company": job.company, "role": job.job_title, "score": score_val,
            "location": job.location, "source": job.source, "job_url": job.job_url,
            "folder": job.folder_name(), "files": "CV, Cover",
        })
    return rows
