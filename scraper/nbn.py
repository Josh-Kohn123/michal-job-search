"""Fetch job listings from the Nefesh B'Nefesh job board.

The board at https://www.nbn.org.il/jobboard/ is a WordPress site running the
WP Job Manager plugin. Listings are the `job_listing` custom post type, which
is exposed on the site's own public REST API under the `job-listings` base.
That API is the canonical source (same database rows the HTML page renders),
so we read it directly instead of parsing markup.
"""

from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

import requests

API = "https://www.nbn.org.il/jobboard/wp-json/wp/v2/job-listings"
USER_AGENT = "nbn-jobboard-telegram-bot/1.0 (+https://github.com/)"

# The board publishes roughly 6 listings a day, so a single page of 100 covers
# about two weeks. Two pages gives ample headroom for a 30-minute poll even if
# the workflow is paused for a while.
PAGES_TO_SCAN = 2
PER_PAGE = 100


@dataclass
class Job:
    id: int
    title: str
    url: str
    posted: str            # ISO-8601, site local time (Asia/Jerusalem)
    company: str = ""
    company_website: str = ""
    apply_to: str = ""
    regions: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    job_types: list[str] = field(default_factory=list)
    remote: bool = False
    filled: bool = False
    salary: str = ""
    description: str = ""

    @property
    def location(self) -> str:
        return ", ".join(self.regions)

    def haystack(self) -> str:
        return " ".join(
            [self.title, self.company, self.description, self.location]
            + self.categories
            + self.job_types
        ).lower()


# Every listing ends with the same board-wide footer; it adds nothing to an alert.
BOILERPLATE = re.compile(
    r"\s*Tell them you heard about the position from Nefesh B[\u2019']Nefesh\.?"
    r"(\s*Please do not repost position\.?)?\s*$",
    re.IGNORECASE,
)


def _clean(raw: str) -> str:
    """Strip HTML tags and decode entities from a WordPress rendered field."""
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw or "")
    text = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</div>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", "", text)
    text = html.unescape(text).replace(" ", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return BOILERPLATE.sub("", text).strip()


def _terms(item: dict[str, Any], taxonomy: str) -> list[str]:
    """Read embedded taxonomy term names for one taxonomy of a listing."""
    names: list[str] = []
    for group in item.get("_embedded", {}).get("wp:term", []) or []:
        for term in group or []:
            if term.get("taxonomy") == taxonomy and term.get("name"):
                names.append(html.unescape(term["name"]))
    return names


def _parse(item: dict[str, Any]) -> Job:
    meta = item.get("meta") or {}
    salary_bits = [
        str(meta.get("_job_salary") or "").strip(),
        str(meta.get("_job_salary_currency") or "").strip(),
        str(meta.get("_job_salary_unit") or "").strip(),
    ]
    return Job(
        id=int(item["id"]),
        title=_clean(item.get("title", {}).get("rendered", "")) or "(untitled)",
        url=item.get("link", ""),
        posted=item.get("date", ""),
        company=_clean(str(meta.get("_company_name") or "")),
        company_website=str(meta.get("_company_website") or "").strip(),
        apply_to=str(meta.get("_application") or "").strip(),
        regions=_terms(item, "job_listing_region"),
        categories=_terms(item, "job_listing_category"),
        job_types=_terms(item, "job_listing_type"),
        remote=bool(meta.get("_remote_position")),
        filled=bool(meta.get("_filled")),
        salary=" ".join(b for b in salary_bits if b),
        description=_clean(item.get("content", {}).get("rendered", "")),
    )


def _get(session: requests.Session, params: dict[str, Any]) -> requests.Response:
    """GET with a short retry on transient errors."""
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            resp = session.get(API, params=params, timeout=30)
            if resp.status_code < 500 and resp.status_code != 429:
                resp.raise_for_status()
                return resp
            last_error = requests.HTTPError(f"HTTP {resp.status_code}", response=resp)
        except requests.RequestException as exc:  # network hiccup, DNS, timeout
            last_error = exc
        time.sleep(2 ** attempt)
    raise RuntimeError(f"NBN job board API unreachable: {last_error}")


def fetch_recent(pages: int = PAGES_TO_SCAN, per_page: int = PER_PAGE) -> list[Job]:
    """Return the most recently published listings, newest first.

    Only `publish` status is returned by the API, so filled/expired listings
    that WP Job Manager has taken down drop out on their own.
    """
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

    jobs: list[Job] = []
    for page in range(1, pages + 1):
        resp = _get(
            session,
            {
                "per_page": per_page,
                "page": page,
                "orderby": "date",
                "order": "desc",
                "_embed": "wp:term",
            },
        )
        batch = resp.json()
        if not isinstance(batch, list) or not batch:
            break
        jobs.extend(_parse(item) for item in batch)
        if page >= int(resp.headers.get("X-WP-TotalPages") or 1):
            break
    return jobs


def matches(job: Job, *, keywords: Iterable[str], exclude: Iterable[str],
            regions: Iterable[str], categories: Iterable[str],
            job_types: Iterable[str], remote_only: bool) -> bool:
    """Apply the optional user filters. Empty filter lists mean "allow all"."""
    hay = job.haystack()
    keywords, exclude = list(keywords), list(exclude)
    regions, categories, job_types = list(regions), list(categories), list(job_types)

    if remote_only and not job.remote:
        return False
    if keywords and not any(k in hay for k in keywords):
        return False
    if any(k in hay for k in exclude):
        return False

    def tax_ok(wanted: list[str], have: list[str]) -> bool:
        if not wanted:
            return True
        have_l = [h.lower() for h in have]
        return any(w in h or w == h.replace(" ", "-") for w in wanted for h in have_l)

    return (
        tax_ok(regions, job.regions)
        and tax_ok(categories, job.categories)
        and tax_ok(job_types, job.job_types)
    )
