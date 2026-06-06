# AutoJobPilot 🧭

A lean, **local-first** job-search automation tool. It scrapes jobs (EthicalJobs +
LinkedIn, compliantly), scores each against your master resume with an LLM,
shortlists strong fits, and generates a tailored CV, cover letter, and recruiter
outreach message — then saves everything in organized per-run folders.

**It never auto-applies and never auto-sends anything.** You stay in control of
every application.

## Design at a glance

- **Single process.** Streamlit UI + in-process APScheduler + the run pipeline.
  No FastAPI, no Redis, no Celery, no message broker.
- **In-memory pipeline, one SQLite file.** Each run holds its working set in RAM;
  the only durable store is `data/app.db` (an embedded file — not a cache/server)
  for cross-run dedup, job status, and run history.
- **Crash-resilient with checkpoints.** Every run writes `raw_jobs.json` (the
  scraped *source of truth*) and `run_state.json` (phase + per-job progress). On a
  retry, scraping is **skipped** if `raw_jobs.json` exists, and scoring/generation
  resume from the first incomplete job — no re-scraping, no wasted tokens.
- **3 retries with exponential backoff** at the session level (configurable).
- **LLM adapter registry.** Anthropic/Claude by default; swap providers with a
  one-line change (`llm.provider`) once another adapter is added.
- **DOCX + Markdown output** (no PDF dependencies).

```
collect ─▶ stage ─▶ dedupe ─▶ score ─▶ generate ─▶ summarize
   │ (writes raw_jobs.json once; skipped on retry)
   └─ EthicalJobs (requests/BS4, robots.txt-aware) + LinkedIn (Playwright, Mode C)
```

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. (Optional, for LinkedIn Mode C) install the browser
playwright install chromium

# 3. Configure secrets
cp .env.example .env        # then add your ANTHROPIC_API_KEY

# 4. Configure preferences (the single master config)
#    Edit config.yaml — roles, locations, schedule, LLM, sources.
```

## Run

```bash
# Dashboard (also starts the scheduler):
streamlit run app.py

# Or a one-off run from the CLI / external cron:
python run.py                 # manual run now
python run.py --type night    # run as the night slot
```

In the dashboard: **Resume** tab → upload your master resume (PDF/DOCX/TXT) →
**Preferences** tab → set roles/locations/LinkedIn URLs → **Run** tab → *Run now*,
or let the scheduler fire at the configured AEST times.

## The master config (`config.yaml`)

One file holds everything tunable — roles, locations, excluded keywords, the
shortlist threshold, source toggles, LinkedIn URLs, schedule, and the LLM
provider/model. Secrets stay in `.env`.

### Scheduling (AEST)

```yaml
schedule:
  timezone: "Australia/Sydney"   # AEST/AEDT, DST-aware (wall-clock 2pm/10pm year-round)
  runs: ["14:00", "22:00"]
  retries: 3
  backoff_seconds: [30, 120, 480]
```

Use `Australia/Brisbane` instead if you want **fixed UTC+10 with no daylight
saving** (true AEST, Queensland).

### LinkedIn (Mode C — user-driven)

You log in yourself in a real Playwright browser window (the session persists
under `data/.pw_linkedin`), and the tool only reads job pages from URLs you paste
into `sources.linkedin.job_urls`. No automated search, no anti-bot bypass. Set
`sources.linkedin.headless: false` so the login window is visible the first time.

### Swapping the LLM provider

Default is Anthropic Claude (`claude-opus-4-8`). For high-volume scoring you can
switch to a cheaper model by editing `llm.model` (e.g. `claude-sonnet-4-6`). To
add a different provider entirely, implement an adapter in
`autojobpilot/services/llm/`, register it in `registry.py`, and set
`llm.provider` — that's the only config change needed.

## Compliance

- Generates materials only — **never applies, never sends** messages or emails.
- EthicalJobs: respects `robots.txt`, rate-limits, caps jobs per run.
- LinkedIn: user-initiated logged-in browser only; no CAPTCHA/login-wall bypass.
- CVs use only truthful source material — the prompts forbid fabricating
  experience, tools, degrees, or achievements.
- Secrets live in `.env` (gitignored); generated docs and personal data stay
  local and are gitignored.

## Project layout

```
app.py                       # Streamlit UI + scheduler (single entry point)
run.py                       # CLI: one run cycle
config.yaml                  # the master config
autojobpilot/
  config.py  store.py  models.py  utils.py  scheduler.py
  pipeline/   runner.py (retries/backoff) · state.py (checkpoints) · phases.py
  collectors/ ethicaljobs.py · linkedin.py
  services/   resume_parser.py · scorer.py · documents.py · notify.py
              llm/ base.py · registry.py · anthropic_adapter.py
  prompts/    resume_parser · fit_scorer · cv_customizer · cover_letter · linkedin_message · summary_report
data/  outputs/  logs/  resumes/   # runtime (gitignored)
tests/smoke_test.py          # end-to-end pipeline test (no network/LLM)
```

## Tests

```bash
PYTHONPATH="$PWD" python tests/smoke_test.py
```

Drives a full run cycle with a fake LLM and fake collectors (no network, no API
key) and verifies dedup, scoring, document generation, checkpoints, and the
summary report.

## Output structure

```
outputs/2026-06-06/night_10pm/
  raw_jobs.json            # scraped source-of-truth (checkpoint)
  run_state.json           # phase + per-job progress (checkpoint)
  summary_report.md
  shortlisted_jobs.csv
  ACME_HR_Operations_Associate/
    job_details.json  fit_analysis.json
    custom_cv.docx  custom_cv.md
    cover_letter.docx  cover_letter.md
    linkedin_message.txt
```
