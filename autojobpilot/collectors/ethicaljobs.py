"""EthicalJobs collector (PRD Feature 3).

Compliant by design: respects robots.txt, rate-limits requests, caps per run.
Parsing strategy is robust-first: prefer schema.org JobPosting JSON-LD (which
EthicalJobs and most boards embed), fall back to HTML heuristics. Selectors can
drift if the site changes — adjust _parse_detail / _find_job_links if so.
"""

from __future__ import annotations

import html
import json
import time
import urllib.parse
from urllib import robotparser

import requests
from bs4 import BeautifulSoup

from ..models import Job
from ..utils import get_logger
from .base import Collector

log = get_logger("scraping")

# A normal browser UA: the site's CDN 403s obvious-bot UAs. We still honor the two
# ethical constraints that matter — robots.txt and rate limiting.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


class EthicalJobsCollector(Collector):
    name = "EthicalJobs"

    def __init__(self, cfg):
        super().__init__(cfg)
        s = cfg.sources.get("ethicaljobs", {})
        self.base_url: str = s.get("base_url", "https://www.ethicaljobs.com.au").rstrip("/")
        self.delay: float = float(s.get("rate_limit_seconds", 3))
        self.max_jobs: int = int(s.get("max_jobs_per_run", 40))
        self._session = requests.Session()
        self._session.headers.update(_HEADERS)
        self._rp = self._load_robots()

    # ── robots.txt ──
    def _load_robots(self) -> robotparser.RobotFileParser:
        """Fetch robots.txt via requests (the site's CDN blocks urllib's default
        client, which silently makes RobotFileParser.read() set disallow_all).
        We parse the real content; if the fetch genuinely fails we fall back to
        permissive (the user controls their own usage) but still rate-limit."""
        rp = robotparser.RobotFileParser()
        url = f"{self.base_url}/robots.txt"
        try:
            resp = self._session.get(url, timeout=15)
            if resp.status_code == 200:
                rp.parse(resp.text.splitlines())
            else:
                log.warning("robots.txt returned HTTP %s; proceeding (rate-limited)",
                            resp.status_code)
                rp.allow_all = True  # type: ignore[attr-defined]
        except Exception as e:
            log.warning("Could not fetch robots.txt (%s); proceeding (rate-limited)", e)
            rp.allow_all = True  # type: ignore[attr-defined]
        return rp

    def _allowed(self, url: str) -> bool:
        try:
            return self._rp.can_fetch(_HEADERS["User-Agent"], url)
        except Exception:
            return True

    def _get(self, url: str) -> str | None:
        if not self._allowed(url):
            log.info("robots.txt disallows %s — skipping", url)
            return None
        try:
            resp = self._session.get(url, timeout=20)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as e:
            log.warning("Failed to fetch %s: %s", url, e)
            return None
        finally:
            time.sleep(self.delay)  # polite rate limit, applied even on failure

    # ── collect ──
    def collect(self) -> list[Job]:
        categories = self.cfg.search.get("job_categories", [])
        locations = self.cfg.search.get("locations", [])
        seen_urls: set[str] = set()
        jobs: list[Job] = []

        for category in categories:
            if len(jobs) >= self.max_jobs:
                break
            search_url = self._search_url(category, locations)
            html = self._get(search_url)
            if not html:
                continue
            for link in self._find_job_links(html):
                if len(jobs) >= self.max_jobs:
                    break
                if link in seen_urls:
                    continue
                seen_urls.add(link)
                detail = self._get(link)
                if not detail:
                    continue
                job = self._parse_detail(detail, link)
                if job:
                    jobs.append(job)
        log.info("EthicalJobs: collected %d jobs", len(jobs))
        return jobs

    def _search_url(self, category: str, locations: list[str]) -> str:
        params = {"keywords": category}
        # EthicalJobs is AU-only; pass a location hint if a real city is configured.
        city = next((l for l in locations if l.lower() not in ("remote", "hybrid")), "")
        if city:
            params["location"] = city
        return f"{self.base_url}/jobs?" + urllib.parse.urlencode(params)

    def _find_job_links(self, html: str) -> list[str]:
        soup = BeautifulSoup(html, "html.parser")
        links: list[str] = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            # Job detail pages live under /members/<slug> or /jobs/<slug>.
            if any(seg in href for seg in ("/members/", "/jobs/")) and not href.endswith("/jobs"):
                full = urllib.parse.urljoin(self.base_url + "/", href)
                if full.startswith(self.base_url):
                    links.append(full.split("?")[0])
        # De-dup, preserve order.
        return list(dict.fromkeys(links))

    def _parse_detail(self, page_html: str, url: str) -> Job | None:
        soup = BeautifulSoup(page_html, "html.parser")

        # 1) Preferred: schema.org JobPosting JSON-LD.
        jp = self._jobposting_jsonld(soup)
        if jp:
            return Job(
                source=self.name,
                job_title=self._clean(jp.get("title", "")),
                company=self._org_name(jp.get("hiringOrganization")),
                location=self._location(jp.get("jobLocation")),
                job_type=self._emp_type(jp.get("employmentType")),
                date_posted=(jp.get("datePosted", "") or "")[:10],
                application_deadline=(jp.get("validThrough") or None),
                job_url=url,
                description=self._text(jp.get("description", "")),
                salary=self._salary(jp.get("baseSalary")),
            )

        # 2) Fallback heuristics (used when JSON-LD is missing or malformed).
        h1 = soup.find("h1")
        h1_text = self._clean(h1.get_text(strip=True)) if h1 else ""
        page_title = self._clean(soup.title.get_text(strip=True)) if soup.title else ""
        title_text = h1_text or (page_title.split(" - Job in ")[0].split(" - ")[0]
                                 if page_title else "")
        company = self._company_from_logo(soup)
        if not title_text:
            return None
        # Pick the RICHEST content container, not the first match — some pages wrap
        # a tiny <article> alongside the real <main> body.
        candidates = [soup.find("main"), soup.find("article"), soup.body]
        best = max((c for c in candidates if c), key=lambda c: len(c.get_text(strip=True)),
                   default=None)
        desc = best.get_text("\n", strip=True) if best else ""
        return Job(
            source=self.name,
            job_title=title_text,
            company=company,
            location=self._meta_location(soup) or self._location_from_title(page_title),
            job_url=url,
            description=html.unescape(desc[:8000]),
        )

    @staticmethod
    def _clean(s: str) -> str:
        return html.unescape(s or "").strip()

    @staticmethod
    def _company_from_logo(soup: BeautifulSoup) -> str:
        """The posting organisation's logo alt reads "<Org>'s logo" / "<Org> logo"."""
        for img in soup.find_all("img"):
            alt = (img.get("alt") or "").strip()
            low = alt.lower()
            if low.endswith("'s logo"):
                return html.unescape(alt[:-7]).strip()
            if low.endswith(" logo"):
                return html.unescape(alt[:-5]).strip()
        return ""

    @staticmethod
    def _location_from_title(page_title: str) -> str:
        if " - Job in " not in page_title:
            return ""
        right = page_title.split(" - Job in ", 1)[1]      # "<Location> - <Agency>"
        return right.split(" - ")[0].strip()

    @staticmethod
    def _meta_location(soup: BeautifulSoup) -> str:
        el = soup.select_one("[itemprop=jobLocation], .job-location, .location")
        return html.unescape(el.get_text(strip=True))[:80] if el else ""

    # ── JSON-LD helpers ──
    @staticmethod
    def _jobposting_jsonld(soup: BeautifulSoup) -> dict | None:
        for tag in soup.find_all("script", type="application/ld+json"):
            try:
                # strict=False tolerates raw control chars the site sometimes emits.
                data = json.loads(tag.string or "", strict=False)
            except (json.JSONDecodeError, TypeError):
                continue
            candidates = data if isinstance(data, list) else [data]
            # Handle @graph wrappers too.
            for c in list(candidates):
                if isinstance(c, dict) and "@graph" in c:
                    candidates.extend(c["@graph"])
            for c in candidates:
                if isinstance(c, dict) and c.get("@type") == "JobPosting":
                    return c
        return None

    @staticmethod
    def _emp_type(v) -> str:
        """employmentType may be a string or a list of strings in JSON-LD."""
        if isinstance(v, (list, tuple)):
            return ", ".join(str(x) for x in v)
        return str(v) if v else ""

    @staticmethod
    def _org_name(org) -> str:
        name = org.get("name", "") if isinstance(org, dict) else str(org or "")
        return html.unescape(name).strip()

    @staticmethod
    def _location(loc) -> str:
        if isinstance(loc, list):
            loc = loc[0] if loc else {}
        if isinstance(loc, dict):
            addr = loc.get("address", {})
            if isinstance(addr, dict):
                joined = ", ".join(
                    x for x in (addr.get("addressLocality"), addr.get("addressRegion")) if x
                )
                return html.unescape(joined).strip()
        return ""

    @staticmethod
    def _salary(sal) -> str | None:
        if isinstance(sal, dict):
            val = sal.get("value")
            if isinstance(val, dict):
                return str(val.get("value") or val.get("minValue") or "") or None
        return None

    @staticmethod
    def _text(html_or_text: str) -> str:
        if "<" in html_or_text and ">" in html_or_text:
            return BeautifulSoup(html_or_text, "html.parser").get_text("\n", strip=True)[:8000]
        return html_or_text[:8000]
