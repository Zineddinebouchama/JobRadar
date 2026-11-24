"""Large, paginated sources: Himalayas, Arbeitnow, Jobicy, Amazon and big
employers that publish their openings through Workday.

Every source here has two ways to run:
  * quick(): only the newest page or two, run about once a minute, so a new
    posting shows up almost as soon as it's published;
  * full(): a deeper sweep. For the biggest feeds the sweep is done in
    chunks, continuing where the last one stopped, so tens of thousands of
    jobs are filled in gradually without hammering anyone's servers.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from . import db
from .normalize import html_to_text, make_job, salary_range

log = logging.getLogger("postedfresh.feeds")


async def _get(client: httpx.AsyncClient, url: str, **params: Any) -> Any:
    r = await client.get(url, params=params or None)
    r.raise_for_status()
    return r.json()


# Progress of chunked sweeps survives restarts (kept in the database).
def _progress(key: str) -> dict:
    return db.get_meta_json(f"sweep:{key}", {})


def _save_progress(key: str, value: dict) -> None:
    db.set_meta_json(f"sweep:{key}", value)


def _relative_posted(text: str | None) -> str | None:
    """'Posted Today' / 'Posted Yesterday' / 'Posted 3 Days Ago' / 'Posted 30+ Days Ago' -> ISO time."""
    if not text:
        return None
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    t = text.lower()
    if "today" in t or "just" in t or "hour" in t or "minute" in t:
        return today.isoformat()
    if "yesterday" in t:
        return (today - timedelta(days=1)).isoformat()
    m = re.search(r"(\d+)\+?\s*day", t)
    if m:
        return (today - timedelta(days=int(m.group(1)))).isoformat()
    return None


# --------------------------------------------------------------------------- #
# Himalayas (remote jobs, ~120k): newest pages every minute, the rest in chunks
# --------------------------------------------------------------------------- #
HIMALAYAS_URL = "https://himalayas.app/jobs/api"


def _himalayas_job(j: dict) -> dict:
    locs = j.get("locationRestrictions") or []
    return make_job(
        source="himalayas", native_id=j.get("guid"), title=j["title"], company=j["companyName"],
        url=j.get("applicationLink") or j.get("guid"), posted_at=j.get("pubDate"),
        location=("Remote — " + ", ".join(locs[:3])) if locs else "Remote (Worldwide)", remote=True,
        type_hints=[j.get("employmentType")], level_hints=j.get("seniority"),
        category_hints=(j.get("parentCategories") or []) + (j.get("categories") or []),
        department=", ".join(j.get("parentCategories") or []) or None,
        salary=salary_range(j.get("minSalary"), j.get("maxSalary"), j.get("currency"), j.get("salaryPeriod")),
        tags=[c.replace("-", " ") for c in j.get("categories") or []], logo=j.get("companyLogo"),
        description=html_to_text(j.get("description"), limit=12000),
    )


async def _himalayas_pages(client, cursor: str | None, pages: int) -> tuple[list[dict], str | None]:
    out: list[dict] = []
    for _ in range(pages):
        data = await _get(client, HIMALAYAS_URL, limit=20, **({"cursor": cursor} if cursor else {}))
        out += [_himalayas_job(j) for j in data.get("jobs", []) if j.get("title")]
        cursor = data.get("nextCursor")
        if not cursor:
            break
        await asyncio.sleep(0.25)  # be polite
    return out, cursor


async def himalayas_quick(client) -> list[dict]:
    jobs, _ = await _himalayas_pages(client, None, 3)
    return jobs


async def himalayas_full(client) -> list[dict]:
    p = _progress("himalayas")
    if p.get("done_at") and time.time() - p["done_at"] < 12 * 3600:
        return await himalayas_quick(client)  # finished recently; just stay current
    jobs, cursor = await _himalayas_pages(client, p.get("cursor"), 60)
    _save_progress("himalayas", {"cursor": cursor} if cursor else {"done_at": time.time()})
    return jobs


# --------------------------------------------------------------------------- #
# Arbeitnow (Europe): newest page every minute, every page on the full sweep
# --------------------------------------------------------------------------- #
def _arbeitnow_job(j: dict) -> dict:
    return make_job(
        source="arbeitnow", native_id=j["slug"], title=j["title"], company=j["company_name"],
        url=j["url"], posted_at=j.get("created_at"), location=j.get("location"),
        remote=bool(j.get("remote")) or None, type_hints=j.get("job_types"),
        category_hints=j.get("tags"), tags=j.get("tags"),
        description=html_to_text(j.get("description"), limit=12000),
    )


async def _arbeitnow_pages(client, first: int, last: int) -> tuple[list[dict], int, bool]:
    """Returns (jobs, next page to read, reached the end)."""
    out: list[dict] = []
    page = first
    while page <= last:
        try:
            data = await _get(client, "https://www.arbeitnow.com/api/job-board-api", page=page)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:  # asked to slow down: keep what we have, resume later
                return out, page, False
            raise
        out += [_arbeitnow_job(j) for j in data.get("data", [])]
        if not (data.get("links") or {}).get("next"):
            return out, 1, True
        page += 1
        await asyncio.sleep(2.0)
    return out, page, False


async def arbeitnow_quick(client) -> list[dict]:
    jobs, _, _ = await _arbeitnow_pages(client, 1, 1)
    return jobs


async def arbeitnow_full(client) -> list[dict]:
    p = _progress("arbeitnow")
    if p.get("done_at") and time.time() - p["done_at"] < 3 * 3600:
        return await arbeitnow_quick(client)
    start = p.get("page", 1)
    jobs, nxt, done = await _arbeitnow_pages(client, start, start + 9)
    _save_progress("arbeitnow", {"done_at": time.time()} if done else {"page": nxt})
    return jobs


# --------------------------------------------------------------------------- #
# Jobicy (remote): newest 100 every few minutes, every industry on the sweep
# --------------------------------------------------------------------------- #
JOBICY_INDUSTRIES = [
    "accounting-finance", "admin", "business", "copywriting", "data-science", "design-multimedia", "dev",
    "education", "engineering", "healthcare", "hr", "legal", "management", "marketing", "seller",
    "supporting", "technical-support", "web-app-design", "seo", "smm", "product", "project-management",
]


def _jobicy_job(j: dict) -> dict:
    return make_job(
        source="jobicy", native_id=j["id"], title=html_to_text(j["jobTitle"]), company=j["companyName"],
        url=j["url"], posted_at=j.get("pubDate"), location=j.get("jobGeo") or "Remote", remote=True,
        type_hints=j.get("jobType"), level_hints=[j.get("jobLevel")], category_hints=j.get("jobIndustry"),
        department=", ".join(j.get("jobIndustry") or []) or None,
        salary=salary_range(j.get("annualSalaryMin"), j.get("annualSalaryMax"), j.get("salaryCurrency")),
        logo=j.get("companyLogo"), description=html_to_text(j.get("jobDescription"), limit=12000),
    )


async def jobicy_quick(client) -> list[dict]:
    data = await _get(client, "https://jobicy.com/api/v2/remote-jobs", count=100)
    return [_jobicy_job(j) for j in data.get("jobs", [])]


async def jobicy_full(client) -> list[dict]:
    out = await jobicy_quick(client)
    for ind in JOBICY_INDUSTRIES:
        try:
            data = await _get(client, "https://jobicy.com/api/v2/remote-jobs", count=100, industry=ind)
            out += [_jobicy_job(j) for j in data.get("jobs", [])]
        except (httpx.HTTPError, ValueError):
            continue  # an industry name the feed doesn't use
        await asyncio.sleep(0.5)
    return out


# --------------------------------------------------------------------------- #
# Amazon (~10k listed, newest first)
# --------------------------------------------------------------------------- #
AMAZON_URL = "https://www.amazon.jobs/en/search.json"


def _amazon_company(entity: str, business: str) -> str:
    if (business or "").lower() == "aws" or "web services" in (entity or "").lower():
        return "Amazon Web Services"
    return "Amazon"


def _amazon_job(j: dict) -> dict:
    desc = "\n\n".join(x for x in (
        html_to_text(j.get("description")),
        "Basic qualifications\n" + html_to_text(j.get("basic_qualifications")) if j.get("basic_qualifications") else "",
        "Preferred qualifications\n" + html_to_text(j.get("preferred_qualifications")) if j.get("preferred_qualifications") else "",
    ) if x)
    company = _amazon_company(j.get("company_name", ""), j.get("business_category", ""))
    return make_job(
        source="amazon", native_id=j.get("id_icims") or j.get("id"), title=j.get("title", ""), company=company,
        url="https://www.amazon.jobs" + (j.get("job_path") or ""), posted_at=j.get("posted_date"),
        location=j.get("normalized_location") or j.get("location"),
        type_hints=[j.get("job_schedule_type"), "Internship" if j.get("is_intern") else ""],
        category_hints=[j.get("job_category"), j.get("job_family")], department=j.get("job_category"),
        logo="/api/logo/amazon.com", description=desc[:12000], posted_precision="day",
    )


async def _amazon_pages(client, offset: int, pages: int) -> tuple[list[dict], int, int]:
    out: list[dict] = []
    hits = 0
    for _ in range(pages):
        data = await _get(client, AMAZON_URL, result_limit=100, sort="recent", offset=offset)
        hits = int(data.get("hits") or 0)
        batch = data.get("jobs") or []
        out += [_amazon_job(j) for j in batch if j.get("title")]
        offset += len(batch)
        if not batch or offset >= hits:
            break
        await asyncio.sleep(0.5)
    return out, offset, hits


async def amazon_quick(client) -> list[dict]:
    jobs, _, _ = await _amazon_pages(client, 0, 2)
    return jobs


async def amazon_full(client) -> list[dict]:
    p = _progress("amazon")
    if p.get("done_at") and time.time() - p["done_at"] < 6 * 3600:
        return await amazon_quick(client)
    jobs, offset, hits = await _amazon_pages(client, p.get("offset", 0), 15)
    _save_progress("amazon", {"offset": offset} if offset < hits and jobs else {"done_at": time.time()})
    return jobs


# --------------------------------------------------------------------------- #
# Big employers on Workday (Target, CVS Health, Home Depot, Nike, ...)
# --------------------------------------------------------------------------- #
# (display name, tenant, data center, career site, website)
WORKDAY = [
    ("CVS Health", "cvshealth", "wd1", "CVS_Health_Careers", "cvshealth.com"),
    ("Thermo Fisher", "thermofisher", "wd5", "ThermoFisherCareers", "thermofisher.com"),
    ("Capital One", "capitalone", "wd12", "Capital_One", "capitalone.com"),
    ("Target", "target", "wd5", "targetcareers", "target.com"),
    ("NVIDIA", "nvidia", "wd5", "NVIDIAExternalCareerSite", "nvidia.com"),
    ("Accenture", "accenture", "wd103", "AccentureCareers", "accenture.com"),
    ("T-Mobile", "tmobile", "wd1", "External", "t-mobile.com"),
    ("Citi", "citi", "wd5", "2", "citi.com"),
    ("Bank of America", "ghr", "wd1", "lateral-us", "bankofamerica.com"),
    ("Salesforce", "salesforce", "wd12", "External_Career_Site", "salesforce.com"),
    ("Wells Fargo", "wf", "wd1", "WellsFargoJobs", "wellsfargo.com"),
    ("Verizon", "verizon", "wd12", "verizon-careers", "verizon.com"),
    ("Morgan Stanley", "ms", "wd5", "External", "morganstanley.com"),
    ("Home Depot", "homedepot", "wd5", "CareerDepot", "homedepot.com"),
    ("Intel", "intel", "wd1", "External", "intel.com"),
    ("HP", "hp", "wd5", "ExternalCareerSite", "hp.com"),
    ("Nordstrom", "nordstrom", "wd501", "nordstrom_careers", "nordstrom.com"),
    ("Fidelity Investments", "fmr", "wd1", "FidelityCareers", "fidelity.com"),
    ("Medtronic", "medtronic", "wd1", "MedtronicCareers", "medtronic.com"),
    ("Boeing", "boeing", "wd1", "EXTERNAL_CAREERS", "boeing.com"),
    ("Cardinal Health", "cardinalhealth", "wd1", "EXT", "cardinalhealth.com"),
    ("3M", "3m", "wd1", "Search", "3m.com"),
    ("Pfizer", "pfizer", "wd1", "PfizerCareers", "pfizer.com"),
    ("Mastercard", "mastercard", "wd1", "CorporateCareers", "mastercard.com"),
    ("Nike", "nike", "wd1", "nke", "nike.com"),
    ("General Motors", "generalmotors", "wd5", "Careers_GM", "gm.com"),
    ("Broadcom", "broadcom", "wd1", "External_Career", "broadcom.com"),
    ("PayPal", "paypal", "wd1", "jobs", "paypal.com"),
    ("Workday", "workday", "wd5", "Workday", "workday.com"),
    ("eBay", "ebay", "wd5", "apply", "ebay.com"),
    ("Adobe", "adobe", "wd5", "external_experienced", "adobe.com"),
    ("Zillow", "zillow", "wd5", "Zillow_Group_External", "zillow.com"),
    ("Snap", "snapchat", "wd1", "snap", "snap.com"),
    ("Chevron", "chevron", "wd5", "jobs", "chevron.com"),
    ("Sony", "sonyglobal", "wd1", "SonyGlobalCareers", "sony.com"),
]
WORKDAY_BY_TENANT = {t: (name, t, wd, site, dom) for name, t, wd, site, dom in WORKDAY}
_WD_PAGE = 20


def _wd_base(t: str, wd: str, site: str) -> str:
    return f"https://{t}.{wd}.myworkdayjobs.com/wday/cxs/{t}/{site}"


def _workday_job(cfg: tuple, p: dict) -> dict:
    name, t, wd, site, domain = cfg
    path = p.get("externalPath") or ""
    req = (p.get("bulletFields") or [""])[0] or path.rsplit("_", 1)[-1]
    location = p.get("locationsText") or ""  # "3 Locations" until the job's own page is read
    return make_job(
        source="workday", native_id=f"{t}/{req or path}", title=p.get("title", ""), company=name,
        url=f"https://{t}.{wd}.myworkdayjobs.com/en-US/{site}{path}",
        posted_at=_relative_posted(p.get("postedOn")), location=location,
        logo=f"/api/logo/{domain}", description=None,  # fetched when the job is opened
        tags=[], type_hints=[], category_hints=[], posted_precision="day",
    )


async def _workday_pages(client, cfg: tuple, offset: int, pages: int) -> tuple[list[dict], int, int]:
    name, t, wd, site, _ = cfg
    out: list[dict] = []
    total = 0
    for _ in range(pages):
        r = await client.post(f"{_wd_base(t, wd, site)}/jobs",
                              json={"appliedFacets": {}, "limit": _WD_PAGE, "offset": offset, "searchText": ""},
                              timeout=30)
        r.raise_for_status()
        data = r.json()
        total = data.get("total") or total
        batch = data.get("jobPostings") or []
        out += [_workday_job(cfg, p) for p in batch if p.get("title")]
        offset += len(batch)
        if not batch or offset >= total:
            break
        await asyncio.sleep(0.3)
    return out, offset, total


async def workday_quick(client) -> list[dict]:
    sem = asyncio.Semaphore(6)

    async def one(cfg):
        async with sem:
            try:
                jobs, _, _ = await _workday_pages(client, cfg, 0, 1)
                return jobs
            except (httpx.HTTPError, ValueError) as e:
                log.warning("workday %s quick failed: %s", cfg[0], e)
                return []

    return [j for r in await asyncio.gather(*(one(c) for c in WORKDAY)) for j in r]


async def workday_full(client) -> list[dict]:
    """Continue each employer's sweep by up to 15 pages (300 jobs)."""
    sem = asyncio.Semaphore(4)

    async def one(cfg):
        key = f"workday:{cfg[1]}"
        p = _progress(key)
        if p.get("done_at") and time.time() - p["done_at"] < 6 * 3600:
            return []
        async with sem:
            try:
                jobs, offset, total = await _workday_pages(client, cfg, p.get("offset", 0), 15)
            except (httpx.HTTPError, ValueError) as e:
                log.warning("workday %s sweep failed: %s", cfg[0], e)
                return []
        _save_progress(key, {"offset": offset} if offset < total and jobs else {"done_at": time.time()})
        return jobs

    quick = await workday_quick(client)
    return quick + [j for r in await asyncio.gather(*(one(c) for c in WORKDAY)) for j in r]


async def workday_description(client, native_id: str, url: str) -> dict:
    """Full description and schedule for one Workday job (loaded when the job is opened)."""
    tenant = native_id.split("/", 1)[0]
    cfg = WORKDAY_BY_TENANT.get(tenant)
    if not cfg:
        return {}
    m = re.search(r"/job/.*$", url)
    if not m:
        return {}
    data = await _get(client, _wd_base(cfg[1], cfg[2], cfg[3]) + m.group(0))
    info = data.get("jobPostingInfo") or {}
    places = [info.get("location")] + list(info.get("additionalLocations") or [])
    return {
        "description": html_to_text(info.get("jobDescription"), limit=12000),
        "time_type": info.get("timeType"),
        "start_date": info.get("startDate"),
        "location": "; ".join(p for p in places if p) or None,
    }
