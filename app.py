"""AutoJobPilot dashboard — UI + manual run trigger.

Run with:  streamlit run app.py

Sections (PRD Feature 12): upload resume, configure preferences, trigger a run,
view jobs / scores / generated files, export CSV.

Automated daily runs are handled by the SEPARATE scheduler daemon
(`python -m autojobpilot.scheduler`), not by this dashboard — so they fire
whether or not the dashboard is open. This page only shows the configured
schedule and offers a manual "Run now".
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from autojobpilot import store
from autojobpilot.config import RESUME_JSON_PATH, RESUMES_DIR, ensure_dirs, load_config, save_config
from autojobpilot.pipeline import rescore_from_ledger, run_cycle
from autojobpilot.services.llm.registry import get_llm
from autojobpilot.services.resume_parser import ingest_resume, load_resume

st.set_page_config(page_title="AutoJobPilot", page_icon="🧭", layout="wide")

ensure_dirs()
store.init_db()

cfg = load_config()

st.title("🧭 AutoJobPilot")
st.caption("Lean, local-first job-search automation — generate, never auto-apply.")

tab_run, tab_resume, tab_prefs, tab_jobs, tab_runs = st.tabs(
    ["▶️ Run", "📄 Resume", "⚙️ Preferences", "📋 Jobs", "🕓 Run history"]
)

# ── Run tab ─────────────────────────────────────────────────────────────────
with tab_run:
    st.subheader("Trigger a run")
    st.write("**Scheduled (", cfg.schedule.get("timezone"), "):**",
             ", ".join(cfg.schedule.get("runs", [])) or "none")
    st.caption("Automated runs fire via the scheduler daemon "
               "(`python -m autojobpilot.scheduler`), independently of this page.")

    if load_resume() is None:
        st.warning("Upload a master resume first (Resume tab).")
    col_run, col_rescore = st.columns(2)
    with col_run:
        if st.button("Run now", type="primary", disabled=load_resume() is None):
            with st.spinner("Running cycle (collect → score → generate → summarize)…"):
                try:
                    stats = run_cycle(cfg, "manual")
                    st.success("Run complete.")
                    st.json(stats)
                except Exception as e:
                    st.error(f"Run failed: {e}")
    with col_rescore:
        if st.button("Re-score unscored jobs", disabled=load_resume() is None,
                     help="Re-score ledger jobs that were never successfully scored "
                          "(e.g. after a rate-limit failure) — no re-scraping."):
            with st.spinner("Re-scoring unscored jobs from the ledger…"):
                try:
                    stats = rescore_from_ledger(cfg)
                    st.success("Re-score complete.")
                    st.json(stats)
                except Exception as e:
                    st.error(f"Re-score failed: {e}")

# ── Resume tab ──────────────────────────────────────────────────────────────
with tab_resume:
    st.subheader("Master resume (PDF / DOCX / TXT)")
    up = st.file_uploader("Upload your master resume", type=["pdf", "docx", "txt"])
    if up is not None and st.button("Parse & save"):
        dest = RESUMES_DIR / up.name
        dest.write_bytes(up.getbuffer())
        with st.spinner("Extracting text and parsing via LLM…"):
            try:
                parsed = ingest_resume(dest, get_llm(cfg))
                st.success("Resume parsed and saved (original preserved, never overwritten).")
            except Exception as e:
                st.error(f"Parsing failed: {e}")

    parsed = load_resume()
    if parsed:
        st.markdown("**Parsed resume** (edit `data/resume.json` directly to correct):")
        st.json({k: v for k, v in parsed.items() if not k.startswith("_")})

# ── Preferences tab ─────────────────────────────────────────────────────────
with tab_prefs:
    st.subheader("Search preferences")
    s = cfg.search
    cats = st.text_area("Job categories (one per line)", "\n".join(s.get("job_categories", [])))
    locs = st.text_area("Locations (one per line)", "\n".join(s.get("locations", [])))
    excl = st.text_area("Excluded keywords (one per line)", "\n".join(s.get("excluded_keywords", [])))
    minimum = st.slider("Minimum shortlist score", 0.0, 10.0,
                        float(s.get("minimum_score", 5)), 0.5)

    st.markdown("**LinkedIn** runs in always-on logged-in browser mode — it searches "
                "the categories + locations above automatically (no URLs to paste). "
                "On the first run a browser window opens: log in once and the session "
                "is reused. Requires `playwright install chromium`.")

    if st.button("Save preferences"):
        cfg.search["job_categories"] = [x.strip() for x in cats.splitlines() if x.strip()]
        cfg.search["locations"] = [x.strip() for x in locs.splitlines() if x.strip()]
        cfg.search["excluded_keywords"] = [x.strip() for x in excl.splitlines() if x.strip()]
        cfg.search["minimum_score"] = minimum
        save_config(cfg)
        st.success("Saved to config.yaml.")

# ── Jobs tab ────────────────────────────────────────────────────────────────
with tab_jobs:
    st.subheader("Jobs")
    status_filter = st.selectbox(
        "Filter by status",
        ["all", "shortlisted", "needs_review", "rejected", "applied", "saved", "error"],
    )
    rows = store.list_jobs(None if status_filter == "all" else status_filter)
    if rows:
        df = pd.DataFrame(rows)[
            [c for c in ["created_at", "source", "company", "job_title", "location",
                         "score", "status", "job_url"] if c in rows[0]]
        ]
        st.dataframe(df, use_container_width=True, hide_index=True)
        st.download_button("Export CSV", df.to_csv(index=False).encode("utf-8"),
                           "jobs.csv", "text/csv")

        st.markdown("**Update a job's status**")
        ids = {f"{r['company']} — {r['job_title']} ({r['job_id'][:6]})": r["job_id"] for r in rows}
        pick = st.selectbox("Job", list(ids.keys()))
        new_status = st.selectbox("New status", ["applied", "saved", "rejected", "shortlisted"])
        if st.button("Apply status"):
            store.set_status(ids[pick], new_status)
            st.success("Updated. Refresh to see it.")
    else:
        st.info("No jobs yet. Run a cycle from the Run tab.")

# ── Run history tab ─────────────────────────────────────────────────────────
with tab_runs:
    st.subheader("Run history")
    runs = store.list_runs()
    if runs:
        st.dataframe(pd.DataFrame(runs), use_container_width=True, hide_index=True)
        latest = runs[0]
        st.markdown(f"**Latest run:** {latest['run_id']} — {latest['status']}")
    else:
        st.info("No runs yet.")
