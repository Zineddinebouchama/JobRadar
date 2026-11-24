"""Fetchers for each job source. Every fetcher returns normalized job dicts.

Two kinds of sources:
  * Job board APIs (Remotive, RemoteOK, Jobicy, Himalayas, Arbeitnow) — many
    companies, many categories.
  * Company applicant-tracking systems (Greenhouse, Lever, Ashby,
    SmartRecruiters) — a role shows up here the moment the company publishes
    it, usually before it is syndicated to the big job sites.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

import httpx

from . import config, db, feeds
from .normalize import html_to_text, make_job, salary_range

log = logging.getLogger("postedfresh.sources")

Fetcher = Callable[[httpx.AsyncClient], Awaitable[list[dict[str, Any]]]]


async def _json(client: httpx.AsyncClient, url: str, **params: Any) -> Any:
    r = await client.get(url, params=params or None)
    r.raise_for_status()
    return r.json()


# --------------------------------------------------------------------------- #
# Job board APIs
# --------------------------------------------------------------------------- #
async def remotive(client: httpx.AsyncClient) -> list[dict]:
    data = await _json(client, "https://remotive.com/api/remote-jobs", limit=1000)
    return [
        make_job(
            source="remotive", native_id=j["id"], title=j["title"], company=j["company_name"],
            url=j["url"], posted_at=j.get("publication_date"),
            location=j.get("candidate_required_location") or "Remote", remote=True,
            type_hints=[j.get("job_type")], category_hints=[j.get("category")],
            salary=j.get("salary") or None, tags=j.get("tags"), logo=j.get("company_logo"),
            description=html_to_text(j.get("description")),
        )
        for j in data.get("jobs", [])
    ]


async def remoteok(client: httpx.AsyncClient) -> list[dict]:
    data = await _json(client, "https://remoteok.com/api")
    return [
        make_job(
            source="remoteok", native_id=j["id"], title=j.get("position", ""), company=j.get("company", ""),
            url=j.get("url") or j.get("apply_url"), posted_at=j.get("epoch") or j.get("date"),
            location=j.get("location") or "Remote", remote=True,
            type_hints=j.get("tags"), category_hints=j.get("tags"),
            salary=salary_range(j.get("salary_min"), j.get("salary_max")),
            tags=j.get("tags"), logo=j.get("company_logo") or j.get("logo"),
            description=html_to_text(j.get("description")),
        )
        for j in data
        if isinstance(j, dict) and j.get("id") and j.get("position")
    ]


# --------------------------------------------------------------------------- #
# Company applicant-tracking systems
# --------------------------------------------------------------------------- #
PRETTY = {
    "openai": "OpenAI", "posthog": "PostHog", "clickup": "ClickUp", "zapier": "Zapier", "ro": "Ro",
    "matchgroup": "Match Group", "scaleai": "Scale AI", "epicgames": "Epic Games", "riotgames": "Riot Games",
    "khanacademy": "Khan Academy", "cockroachlabs": "Cockroach Labs", "grafanalabs": "Grafana Labs",
    "newrelic": "New Relic", "pagerduty": "PagerDuty", "sumologic": "Sumo Logic", "seatgeek": "SeatGeek",
    "stitchfix": "Stitch Fix", "tripadvisor": "Tripadvisor", "sofi": "SoFi", "gitlab": "GitLab",
    "mongodb": "MongoDB", "spacex": "SpaceX", "justworks": "Justworks", "wealthfront": "Wealthfront",
}


def company_name(board: str) -> str:
    return PRETTY.get(board.lower(), board[:1].upper() + board[1:])


def _gh_job(board: str, j: dict) -> dict:
    depts = [d["name"] for d in j.get("departments") or []]
    meta = [m.get("value") for m in j.get("metadata") or [] if m.get("value")]
    return make_job(
        source="greenhouse", native_id=f"{board}/{j['id']}", title=j["title"],
        company=j.get("company_name") or company_name(board), url=j["absolute_url"],
        posted_at=j.get("first_published") or j.get("updated_at"),
        location=(j.get("location") or {}).get("name"),
        department=depts[0] if depts else None, type_hints=meta, category_hints=depts,
        description=html_to_text(j.get("content")),
    )


async def _greenhouse_board(client: httpx.AsyncClient, board: str):
    """First run: one bulk request with descriptions. After that: a light list
    request every minute, fetching full details only for brand-new job ids."""
    base = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs"
    known = await asyncio.to_thread(db.ids_with_prefix, f"greenhouse:{board}/")
    data = await _json(client, base, **({} if known else {"content": "true"}))
    listing = data.get("jobs", [])
    alive = {f"greenhouse:{board}/{j['id']}" for j in listing}
    fresh = [j for j in listing if f"greenhouse:{board}/{j['id']}" not in known]
    if not known:
        return [_gh_job(board, j) for j in listing], alive
    if len(fresh) > 25:  # catching up after downtime: one bulk request beats many small ones
        data = await _json(client, base, content="true")
        ids = {str(j["id"]) for j in fresh}
        return [_gh_job(board, j) for j in data.get("jobs", []) if str(j["id"]) in ids], alive
    sem = asyncio.Semaphore(6)

    async def detail(j: dict) -> dict:
        async with sem:
            try:
                return _gh_job(board, await _json(client, f"{base}/{j['id']}"))
            except Exception:
                return _gh_job(board, j)

    jobs = await asyncio.gather(*(detail(j) for j in fresh))
    return list(jobs), alive


async def _lever_board(client: httpx.AsyncClient, board: str) -> list[dict]:
    data = await _json(client, f"https://api.lever.co/v0/postings/{board}", mode="json")
    out = []
    for j in data:
        cats = j.get("categories") or {}
        sal = j.get("salaryRange") or {}
        desc = (j.get("descriptionPlain") or "") + "\n\n" + "\n\n".join(
            f"{l.get('text', '')}\n{html_to_text(l.get('content'))}" for l in j.get("lists") or []
        ) + "\n\n" + (j.get("additionalPlain") or "")
        out.append(make_job(
            source="lever", native_id=f"{board}/{j['id']}", title=j["text"], company=company_name(board),
            url=j.get("hostedUrl"), posted_at=j.get("createdAt"), location=cats.get("location"),
            remote=(j.get("workplaceType") == "remote") or None, department=cats.get("team") or cats.get("department"),
            workplace_hint=j.get("workplaceType"),
            type_hints=[cats.get("commitment")], category_hints=[cats.get("team"), cats.get("department")],
            salary=salary_range(sal.get("min"), sal.get("max"), sal.get("currency"), sal.get("interval")),
            description=desc.strip()[:20000],
        ))
    return out, {j["id"] for j in out}


async def _ashby_board(client: httpx.AsyncClient, board: str) -> list[dict]:
    data = await _json(client, f"https://api.ashbyhq.com/posting-api/job-board/{board}", includeCompensation="true")
    out = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        comp = (j.get("compensation") or {}).get("compensationTierSummary")
        out.append(make_job(
            source="ashby", native_id=f"{board}/{j['id']}", title=j["title"], company=company_name(board),
            url=j.get("jobUrl"), posted_at=j.get("publishedAt"), location=j.get("location"),
            remote=(j.get("isRemote") or j.get("workplaceType") == "Remote") or None,
            department=j.get("department"), type_hints=[j.get("employmentType")], workplace_hint=j.get("workplaceType"),
            category_hints=[j.get("department"), j.get("team")], salary=comp,
            description=(j.get("descriptionPlain") or "")[:20000],
        ))
    return out, {j["id"] for j in out}


async def _smartrecruiters_company(client: httpx.AsyncClient, company: str) -> list[dict]:
    out: list[dict] = []
    offset = 0
    while offset < config.SMARTRECRUITERS_MAX_PER_COMPANY:
        data = await _json(
            client, f"https://api.smartrecruiters.com/v1/companies/{company}/postings", limit=100, offset=offset
        )
        rows = data.get("content", [])
        for j in rows:
            loc = j.get("location") or {}
            dept = (j.get("department") or {}).get("label") or (j.get("function") or {}).get("label")
            out.append(make_job(
                source="smartrecruiters", native_id=f"{company}/{j['id']}", title=j["name"],
                company=(j.get("company") or {}).get("name") or company,
                url=f"https://jobs.smartrecruiters.com/{company}/{j['id']}",
                posted_at=j.get("releasedDate"), location=loc.get("fullLocation") or loc.get("city"),
                remote=bool(loc.get("remote")) or None, department=dept,
            workplace_hint="hybrid" if loc.get("hybrid") else None,
                type_hints=[(j.get("typeOfEmployment") or {}).get("label")],
                level_hints=[(j.get("experienceLevel") or {}).get("label")],
                category_hints=[(j.get("function") or {}).get("label"), (j.get("industry") or {}).get("label")],
                description=None,  # fetched on demand, see smartrecruiters_description()
            ))
        offset += len(rows)
        if not rows or offset >= data.get("totalFound", 0):
            break
    return out, None  # capped pagination: don't treat unseen ids as closed


async def _smartrecruiters_newest(client: httpx.AsyncClient, company: str):
    data = await _json(client, f"https://api.smartrecruiters.com/v1/companies/{company}/postings", limit=100, offset=0)
    out = []
    for j in data.get("content", []):
        loc = j.get("location") or {}
        dept = (j.get("department") or {}).get("label") or (j.get("function") or {}).get("label")
        out.append(make_job(
            source="smartrecruiters", native_id=f"{company}/{j['id']}", title=j["name"],
            company=(j.get("company") or {}).get("name") or company,
            url=f"https://jobs.smartrecruiters.com/{company}/{j['id']}",
            posted_at=j.get("releasedDate"), location=loc.get("fullLocation") or loc.get("city"),
            remote=bool(loc.get("remote")) or None, department=dept,
            workplace_hint="hybrid" if loc.get("hybrid") else None,
            type_hints=[(j.get("typeOfEmployment") or {}).get("label")],
            level_hints=[(j.get("experienceLevel") or {}).get("label")],
            category_hints=[(j.get("function") or {}).get("label"), (j.get("industry") or {}).get("label")],
            description=None,
        ))
    return out, None


async def smartrecruiters_description(client: httpx.AsyncClient, native_id: str) -> str:
    company, _, posting = native_id.partition("/")
    d = await _json(client, f"https://api.smartrecruiters.com/v1/companies/{company}/postings/{posting}")
    sections = (d.get("jobAd") or {}).get("sections") or {}
    parts = [
        f"{s.get('title', '')}\n{html_to_text(s.get('text'))}"
        for key in ("companyDescription", "jobDescription", "qualifications", "additionalInformation")
        if (s := sections.get(key)) and s.get("text")
    ]
    return "\n\n".join(parts)[:20000]


def _per_company(fn, names: list[str], source: str) -> Fetcher:
    """Run one fetcher per company board. Returns the jobs and, per board, the set
    of ids still live so the poller can drop postings a company has closed."""
    async def run(client: httpx.AsyncClient):
        sem = asyncio.Semaphore(10)
        alive: dict[str, set] = {}

        async def one(name: str) -> list[dict]:
            async with sem:
                try:
                    jobs, ids = await fn(client, name)
                    if ids is not None:
                        alive[f"{source}:{name}/"] = ids
                    return jobs
                except Exception as e:  # one dead board shouldn't sink the rest
                    log.warning("%s/%s failed: %s", source, name, e)
                    return []

        results = await asyncio.gather(*(one(n) for n in names))
        return [j for r in results for j in r], alive

    return run


# quick: newest jobs only, run every `quick_every` seconds.
# fetch: the full sweep, run every `full_every` seconds (company boards also
# report which jobs are still open, so closed ones are removed).
SOURCES: dict[str, dict[str, Any]] = {
    "greenhouse": {"label": "Greenhouse", "kind": "Company career pages", "boards": len(config.GREENHOUSE_BOARDS),
                   "fetch": _per_company(_greenhouse_board, config.GREENHOUSE_BOARDS, "greenhouse"), "full_every": 45},
    "lever": {"label": "Lever", "kind": "Company career pages", "boards": len(config.LEVER_BOARDS),
              "fetch": _per_company(_lever_board, config.LEVER_BOARDS, "lever"), "full_every": 45},
    "ashby": {"label": "Ashby", "kind": "Company career pages", "boards": len(config.ASHBY_BOARDS),
              "fetch": _per_company(_ashby_board, config.ASHBY_BOARDS, "ashby"), "full_every": 45},
    "smartrecruiters": {"label": "SmartRecruiters", "kind": "Company career pages", "boards": len(config.SMARTRECRUITERS_COMPANIES),
                        "quick": _per_company(_smartrecruiters_newest, config.SMARTRECRUITERS_COMPANIES, "smartrecruiters"),
                        "fetch": _per_company(_smartrecruiters_company, config.SMARTRECRUITERS_COMPANIES, "smartrecruiters"),
                        "quick_every": 60, "full_every": 1200},
    "workday": {"label": "Big employers (Workday)", "kind": "Company career pages", "boards": len(feeds.WORKDAY),
                "quick": feeds.workday_quick, "fetch": feeds.workday_full, "quick_every": 90, "full_every": 180},
    "amazon": {"label": "Amazon", "kind": "Company career pages", "boards": 1,
               "quick": feeds.amazon_quick, "fetch": feeds.amazon_full, "quick_every": 60, "full_every": 180},
    "himalayas": {"label": "Himalayas", "kind": "Job board", "quick": feeds.himalayas_quick, "fetch": feeds.himalayas_full,
                  "quick_every": 60, "full_every": 120},
    "arbeitnow": {"label": "Arbeitnow", "kind": "Job board", "quick": feeds.arbeitnow_quick, "fetch": feeds.arbeitnow_full,
                  "quick_every": 60, "full_every": 900},
    "jobicy": {"label": "Jobicy", "kind": "Job board", "quick": feeds.jobicy_quick, "fetch": feeds.jobicy_full,
               "quick_every": 120, "full_every": 1800},
    "remotive": {"label": "Remotive", "kind": "Job board", "fetch": remotive, "full_every": 120},
    "remoteok": {"label": "Remote OK", "kind": "Job board", "fetch": remoteok, "full_every": 120},
}


def new_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": config.USER_AGENT, "Accept": "application/json"},
        timeout=httpx.Timeout(45.0, connect=10.0),
        follow_redirects=True,
        limits=httpx.Limits(max_connections=24),
    )
