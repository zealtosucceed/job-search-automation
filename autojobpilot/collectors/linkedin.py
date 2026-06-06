"""LinkedIn collector — sign-in-first, then headless search (Mode C).

Flow on each run:
  1. Headless probe: is there already a valid logged-in session? (checks the
     `li_at` auth cookie in the persistent profile).
  2. If NOT logged in: open a VISIBLE browser window and wait for you to sign in
     (up to login_timeout_seconds). Nothing is bypassed — you log in yourself.
  3. Once a session exists, the WHOLE scrape runs HEADLESS for the rest of the
     run, searching your configured job_categories + locations.

The session is stored in the persistent profile (data/.pw_linkedin) and reused
across runs, so the sign-in window only appears when the session is missing or
expired. Polite rate limiting + a per-run cap apply; a mid-run login/checkpoint
wall stops LinkedIn cleanly (never bypassed).

First-time setup: pip install playwright && playwright install chromium
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse

from ..config import DATA_DIR
from ..models import Job
from ..utils import get_logger
from .base import Collector

log = get_logger("scraping")

_USER_DATA_DIR = DATA_DIR / ".pw_linkedin"
_SEARCH = "https://www.linkedin.com/jobs/search/?{}"
_VIEW = "https://www.linkedin.com/jobs/view/{}/"
_FEED = "https://www.linkedin.com/feed/"
_LOGIN = "https://www.linkedin.com/login"


class LinkedInCollector(Collector):
    name = "LinkedIn"

    def __init__(self, cfg):
        super().__init__(cfg)
        s = cfg.sources.get("linkedin", {})
        self.rate_limit_ms: int = int(float(s.get("rate_limit_seconds", 4)) * 1000)
        self.max_jobs: int = int(s.get("max_jobs_per_run", 10))
        self.login_timeout: int = int(s.get("login_timeout_seconds", 240))
        self.categories: list[str] = cfg.search.get("job_categories", [])
        self.locations: list[str] = cfg.search.get("locations", [])

    # ── collect ──
    def collect(self) -> list[Job]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            log.warning("Playwright not installed; skipping LinkedIn. "
                        "Run: pip install playwright && playwright install chromium")
            return []

        _USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as p:
            # 1) Probe for an existing session (headless, no window).
            if not self._has_session(p):
                # 2) Prompt sign-in in a visible window (only when needed).
                if not self._prompt_login(p):
                    log.warning("LinkedIn sign-in not completed; skipping LinkedIn this run.")
                    return []
            # 3) Run the whole scrape headless using the saved session.
            return self._scrape(p)

    # ── login lifecycle ──
    def _has_session(self, p) -> bool:
        ctx = p.chromium.launch_persistent_context(str(_USER_DATA_DIR), headless=True)
        try:
            page = ctx.new_page()
            try:
                page.goto(_FEED, wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass
            ok = self._has_li_at(ctx)
            log.info("LinkedIn session check: %s", "logged in" if ok else "not logged in")
            return ok
        finally:
            ctx.close()

    def _prompt_login(self, p) -> bool:
        log.info("Opening a browser window for LinkedIn sign-in — please log in "
                 "(waiting up to %ds)…", self.login_timeout)
        ctx = p.chromium.launch_persistent_context(str(_USER_DATA_DIR), headless=False)
        try:
            page = ctx.new_page()
            try:
                page.goto(_LOGIN, wait_until="domcontentloaded", timeout=45000)
            except Exception:
                pass
            deadline = time.time() + self.login_timeout
            while time.time() < deadline:
                if self._has_li_at(ctx):
                    log.info("LinkedIn sign-in detected — continuing headless.")
                    try:
                        page.wait_for_timeout(1500)
                    except Exception:
                        pass
                    return True
                try:
                    page.wait_for_timeout(2000)
                except Exception:
                    # User closed the window — honor whatever session state exists.
                    return self._has_li_at(ctx)
            log.warning("LinkedIn sign-in timed out after %ds.", self.login_timeout)
            return False
        finally:
            ctx.close()

    @staticmethod
    def _has_li_at(ctx) -> bool:
        try:
            return any(c.get("name") == "li_at" and c.get("value") for c in ctx.cookies())
        except Exception:
            return False

    # ── scrape (headless) ──
    def _scrape(self, p) -> list[Job]:
        location = next((l for l in self.locations if l.lower() not in ("remote", "hybrid")), "")
        jobs: list[Job] = []
        seen_ids: set[str] = set()
        ctx = p.chromium.launch_persistent_context(str(_USER_DATA_DIR), headless=True)
        page = ctx.new_page()
        try:
            for category in self.categories:
                if len(jobs) >= self.max_jobs:
                    break
                job_ids = self._search_job_ids(page, category, location)
                if job_ids is None:  # session died / checkpoint — stop entirely
                    break
                for jid in job_ids:
                    if len(jobs) >= self.max_jobs:
                        break
                    if jid in seen_ids:
                        continue
                    seen_ids.add(jid)
                    job = self._extract(page, jid)
                    if job:
                        jobs.append(job)
                    page.wait_for_timeout(self.rate_limit_ms)  # polite delay
        finally:
            ctx.close()
        log.info("LinkedIn: collected %d jobs", len(jobs))
        return jobs

    # ── search ──
    def _search_job_ids(self, page, category: str, location: str) -> list[str] | None:
        params = {"keywords": category}
        if location:
            params["location"] = location
        url = _SEARCH.format(urllib.parse.urlencode(params))
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            log.warning("LinkedIn search nav failed for %r: %s", category, e)
            return []
        if self._blocked(page):
            log.warning("LinkedIn session expired mid-run (sign-in/checkpoint). "
                        "Re-run to sign in again. Stopping LinkedIn.")
            return None

        page.wait_for_timeout(2500)
        for _ in range(3):
            page.mouse.wheel(0, 4000)
            page.wait_for_timeout(1200)

        hrefs = page.eval_on_selector_all(
            "a[href*='/jobs/view/']", "els => els.map(e => e.getAttribute('href'))"
        )
        ids: list[str] = []
        for h in hrefs:
            jid = self._job_id_from_href(h)
            if jid and jid not in ids:
                ids.append(jid)
        log.info("LinkedIn: %r -> %d job cards", category, len(ids))
        return ids

    @staticmethod
    def _job_id_from_href(href: str | None) -> str | None:
        if not href:
            return None
        m = re.search(r"/jobs/view/(\d+)", href) or re.search(r"-(\d{6,})(?:[/?]|$)", href)
        return m.group(1) if m else None

    @staticmethod
    def _blocked(page) -> bool:
        u = page.url
        return ("linkedin.com/login" in u or "authwall" in u or "checkpoint" in u
                or "/uas/login" in u)

    # ── extract one job ──
    def _extract(self, page, job_id: str) -> Job | None:
        url = _VIEW.format(job_id)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            log.warning("LinkedIn job %s nav failed: %s", job_id, e)
            return None
        if self._blocked(page):
            return None
        page.wait_for_timeout(1500)

        jp = self._jsonld(page)
        if jp:
            return Job(
                source=self.name,
                job_title=jp.get("title", "").strip(),
                company=self._org(jp.get("hiringOrganization")),
                location=self._loc(jp.get("jobLocation")),
                job_type=", ".join(jp["employmentType"]) if isinstance(jp.get("employmentType"), list)
                else (jp.get("employmentType") or ""),
                date_posted=(jp.get("datePosted", "") or "")[:10],
                job_url=url,
                description=self._text(jp.get("description", "")),
            )

        title = self._first_text(page, ["h1", ".top-card-layout__title", ".job-title"])
        company = self._first_text(page, [".topcard__org-name-link",
                                          ".top-card-layout__company",
                                          "a[data-tracking-control-name*='company']"])
        location = self._first_text(page, [".topcard__flavor--bullet",
                                           ".top-card-layout__first-subline"])
        desc = self._first_text(page, [".description__text", ".show-more-less-html__markup",
                                       "#job-details", "article"])
        recruiter = self._first_text(page, [".message-the-recruiter__name",
                                            ".hirer-card__hirer-information a"])
        if not title:
            return None
        return Job(
            source=self.name, job_title=title, company=company, location=location,
            job_url=url, description=(desc or "")[:8000], recruiter_name=recruiter or None,
        )

    # ── helpers ──
    @staticmethod
    def _jsonld(page) -> dict | None:
        for raw in page.eval_on_selector_all(
            "script[type='application/ld+json']", "els => els.map(e => e.textContent)"
        ):
            try:
                data = json.loads(raw, strict=False)
            except (json.JSONDecodeError, TypeError):
                continue
            items = data if isinstance(data, list) else [data]
            for it in items:
                if isinstance(it, dict) and it.get("@type") == "JobPosting":
                    return it
        return None

    @staticmethod
    def _first_text(page, selectors: list[str]) -> str:
        for sel in selectors:
            try:
                el = page.query_selector(sel)
                if el:
                    txt = (el.inner_text() or "").strip()
                    if txt:
                        return txt
            except Exception:
                continue
        return ""

    @staticmethod
    def _org(org) -> str:
        return org.get("name", "") if isinstance(org, dict) else str(org or "")

    @staticmethod
    def _loc(loc) -> str:
        if isinstance(loc, list):
            loc = loc[0] if loc else {}
        if isinstance(loc, dict):
            addr = loc.get("address", {})
            if isinstance(addr, dict):
                return ", ".join(x for x in (addr.get("addressLocality"),
                                             addr.get("addressRegion")) if x)
        return ""

    @staticmethod
    def _text(html_or_text: str) -> str:
        if "<" in html_or_text and ">" in html_or_text:
            from bs4 import BeautifulSoup
            return BeautifulSoup(html_or_text, "html.parser").get_text("\n", strip=True)[:8000]
        return html_or_text[:8000]
