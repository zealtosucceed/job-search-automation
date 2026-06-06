"""End-to-end smoke test of the pipeline with NO network / NO real LLM.

Drives a full run_cycle using:
  - a fake LLM adapter returning canned JSON,
  - fake collectors returning two canned jobs,
  - a pre-seeded resume.json,
into a temp working tree. Verifies dedup, scoring, doc generation, checkpoints,
and the summary report. Also unit-checks the pure helpers.

Run:  python tests/smoke_test.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

# Use a temp tree so we never touch real data/outputs.
_TMP = Path(tempfile.mkdtemp(prefix="ajp_smoke_"))

import autojobpilot.config as config  # noqa: E402

# Redirect all runtime dirs into the temp tree BEFORE anything uses them.
config.DATA_DIR = _TMP / "data"
config.OUTPUTS_DIR = _TMP / "outputs"
config.LOGS_DIR = _TMP / "logs"
config.RESUMES_DIR = _TMP / "resumes"
config.DB_PATH = config.DATA_DIR / "app.db"
config.RESUME_JSON_PATH = config.DATA_DIR / "resume.json"

# Re-point modules that imported the path constants by value.
import autojobpilot.store as store  # noqa: E402
store.DB_PATH = config.DB_PATH
import autojobpilot.utils as utils  # noqa: E402
utils.LOGS_DIR = config.LOGS_DIR
import autojobpilot.pipeline.state as pstate  # noqa: E402
pstate.OUTPUTS_DIR = config.OUTPUTS_DIR
import autojobpilot.services.resume_parser as rp  # noqa: E402
rp.RESUME_JSON_PATH = config.RESUME_JSON_PATH
rp.RESUMES_DIR = config.RESUMES_DIR

from autojobpilot.config import Config  # noqa: E402
from autojobpilot.models import Job  # noqa: E402
from autojobpilot.services.llm.base import LLMAdapter  # noqa: E402
import autojobpilot.pipeline.phases as phases  # noqa: E402
import autojobpilot.pipeline.runner as runner  # noqa: E402

config.ensure_dirs()

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
results: list[tuple[bool, str]] = []


def check(cond: bool, msg: str):
    results.append((bool(cond), msg))
    print(f"  {PASS if cond else FAIL}  {msg}")


# ── pure-helper unit checks ──────────────────────────────────────────────────
def test_helpers():
    print("\n[helpers]")
    check(utils.extract_json('```json\n{"a": 1}\n```') == {"a": 1}, "extract_json strips fences")
    check(utils.extract_json('blah {"x": [1,2]} tail') == {"x": [1, 2]}, "extract_json finds span")
    j = Job(source="X", job_title="HR Lead", company="ACME", job_url="https://x/jobs/1")
    check(len(j.compute_id()) == 16, "Job.compute_id is stable hash")
    check(" " not in j.folder_name() and "ACME" in j.folder_name(), "folder_name is fs-safe")


# ── fakes ────────────────────────────────────────────────────────────────────
class FakeLLM(LLMAdapter):
    def __init__(self):
        super().__init__(model="fake", max_tokens=100, thinking=False, api_key="x")

    def complete(self, prompt, system=None):
        if "fit analysis" in prompt.lower() or "fit_scorer" in prompt.lower() or "matched_skills" in prompt:
            # High score for ACME, low for ZED, so we exercise both branches.
            score = 8 if "ACME" in prompt else 3
            decision = "shortlist" if score >= 5 else "reject"
            return json.dumps({
                "overall_score": score, "decision": decision, "reason": "test",
                "matched_skills": ["hr"], "missing_skills": ["payroll"],
                "resume_strengths_for_this_role": ["comms"], "risk_factors": [],
                "recommended_cv_angle": "hr ops", "recommended_cover_letter_angle": "ops",
            })
        if "customized resume" in prompt.lower() or '"professional_summary"' in prompt:
            return json.dumps({
                "name": "Pat Candidate", "contact": "pat@x.com",
                "professional_summary": "HR ops pro.", "skills": ["HR", "Comms"],
                "experience": [{"company": "ACME", "role": "HR", "duration": "2y",
                                "bullets": ["Did HR things"]}],
                "education": ["BBA"], "certifications": [], "additional_sections": [],
            })
        if "cover letter" in prompt.lower():
            return json.dumps({"cover_letter": "Dear ACME,\n\nI am a great fit.\n\nRegards"})
        if "outreach" in prompt.lower() or "linkedin_message" in prompt:
            return json.dumps({"linkedin_message": "Hi recruiter, great role."})
        return json.dumps({"summary_markdown": "ok"})


class FakeEthical:
    def __init__(self, cfg):
        pass

    def collect(self):
        return [
            Job(source="EthicalJobs", job_title="HR Operations Associate", company="ACME",
                job_url="https://ej/jobs/1", location="Sydney",
                description="HR operations and coordination role.",
                recruiter_name="Priya Sharma"),
            Job(source="EthicalJobs", job_title="Warehouse Picker", company="ZED",
                job_url="https://ej/jobs/2", location="Sydney",
                description="Heavy lifting warehouse role."),
        ]


class FakeLinkedIn:
    def __init__(self, cfg):
        pass

    def collect(self):
        return []


def make_cfg() -> Config:
    data = {
        "profile": {"name": "Pat", "email": "pat@x.com"},
        "search": {"job_categories": ["HR Operations"], "locations": ["Sydney"],
                   "experience_level": [], "job_type": [], "excluded_keywords": ["warehouse"],
                   "minimum_score": 5, "needs_review_band": [4.5, 5]},
        "sources": {"ethicaljobs": {"enabled": True}, "linkedin": {"enabled": True, "job_urls": []}},
        "schedule": {"timezone": "Australia/Sydney", "runs": ["14:00"],
                     "retries": 2, "backoff_seconds": [1, 1]},
        "llm": {"provider": "anthropic", "model": "fake", "max_tokens": 100, "thinking": False},
        "output": {"formats": ["docx", "md"], "notify": "local_markdown"},
    }
    return Config(data, _TMP / "config.yaml")


# ── full pipeline run ────────────────────────────────────────────────────────
def test_pipeline(monkeypatch_targets):
    print("\n[pipeline]")
    cfg = make_cfg()

    # Seed a master resume (skip PDF parsing).
    config.RESUME_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.RESUME_JSON_PATH.write_text(json.dumps({
        "name": "Pat", "_raw_text": "HR operations, coordination, communication, 2 years."
    }), encoding="utf-8")

    # Swap in fakes.
    phases.EthicalJobsCollector = FakeEthical
    phases.LinkedInCollector = FakeLinkedIn
    runner.get_llm = lambda c: FakeLLM()

    stats = runner.run_cycle(cfg, "manual")
    print("  stats:", stats)

    check(stats.get("total") == 2, "collected 2 jobs")
    check(stats.get("new") == 2, "both new")
    # ZED is excluded by keyword "warehouse" -> rejected; ACME scores 8 -> shortlisted.
    check(stats.get("shortlisted") == 1, "1 shortlisted (ACME)")
    check(stats.get("rejected") == 1, "1 rejected (ZED via excluded keyword)")
    check(stats.get("cvs") == 1, "1 CV generated")
    check(stats.get("cover_letters") == 1, "1 cover letter generated")
    check(stats.get("messages") == 1, "1 outreach message (recruiter present)")

    # Checkpoint + artifacts on disk.
    run_dirs = list((config.OUTPUTS_DIR).rglob("raw_jobs.json"))
    check(len(run_dirs) == 1, "raw_jobs.json checkpoint written")
    job_folder = next((config.OUTPUTS_DIR).rglob("custom_cv.docx"), None)
    check(job_folder is not None, "custom_cv.docx exists")
    check(next((config.OUTPUTS_DIR).rglob("cover_letter.docx"), None) is not None,
          "cover_letter.docx exists")
    check(next((config.OUTPUTS_DIR).rglob("linkedin_message.txt"), None) is not None,
          "linkedin_message.txt exists")
    check(next((config.OUTPUTS_DIR).rglob("summary_report.md"), None) is not None,
          "summary_report.md exists")

    # Idempotent re-run: same slot resumes, no new jobs (dedup against ledger).
    stats2 = runner.run_cycle(cfg, "manual")
    check(stats2.get("shortlisted") == 1, "re-run resumes checkpoint (still 1 shortlisted)")


def main() -> int:
    test_helpers()
    test_pipeline(None)
    print("\n──────── results ────────")
    failed = [m for ok, m in results if not ok]
    print(f"{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        print("FAILURES:")
        for m in failed:
            print("  -", m)
        return 1
    print("ALL SMOKE CHECKS PASSED ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
