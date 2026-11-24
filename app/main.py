"""PostedFresh API + static frontend."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, db, feeds, letter, logos, poller
from .categorize import CATEGORIES, ROLE_GROUPS
from .normalize import JOB_TYPES, LEVELS, WORK_SETTINGS, extract_salary, key_facts
from .sources import new_client, smartrecruiters_description

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("postedfresh")
STATIC = config.ROOT / "static"


hub = poller.Hub()          # browsers connected for live updates
web = {}                    # the website's own HTTP client (job details, logos)


def _start_collector() -> subprocess.Popen | None:
    """Run the collector as its own process so collecting never slows down visitors."""
    if os.getenv("POSTEDFRESH_NO_COLLECTOR"):
        return None
    return subprocess.Popen([sys.executable, "-m", "app.worker", "--parent", str(os.getpid())],
                            cwd=str(config.ROOT), env=os.environ.copy())


async def _relay_events() -> None:
    """Pass the collector's "just posted" announcements on to connected browsers."""
    last = await asyncio.to_thread(db.latest_event_id)
    while True:
        await asyncio.sleep(2)
        try:
            for event_id, payload in await asyncio.to_thread(db.events_after, last):
                last = event_id
                hub.publish(payload)
        except Exception as e:
            log.warning("relaying events failed: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await asyncio.to_thread(db.conn)
    collector = _start_collector()
    async with new_client() as client:
        web["client"] = client
        tasks = [asyncio.create_task(_relay_events()), asyncio.create_task(_keep_counts_warm())]
        yield
        for t in tasks:
            t.cancel()
    if collector and collector.poll() is None:
        collector.terminate()


async def _keep_counts_warm() -> None:
    """Pre-compute the landing page's filter counts so the first visit is instant."""
    blank = dict(q="", category="", job_type="", level="", company="", place="", remote="any",
                 posted_within_hours=0, salary_only=False, visa=False, no_degree=False)
    while True:
        try:
            await asyncio.to_thread(db.facets, blank)
        except Exception as e:  # never let this stop the server
            log.warning("warming counts failed: %s", e)
        await asyncio.sleep(15)


app = FastAPI(title="PostedFresh", version="2.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #
@app.get("/api/jobs")
async def list_jobs(
    q: str = "", category: str = "", job_type: str = "", level: str = "", company: str = "",
    place: str = "", remote: str = "any", posted_within_hours: int = 0, salary_only: bool = False,
    visa: bool = False, no_degree: bool = False,
    sort: str = "newest", limit: int = Query(30, le=100), offset: int = 0, facets: bool = True,
):
    f = dict(q=q, category=category, job_type=job_type, level=level, company=company, place=place,
             remote=remote, posted_within_hours=posted_within_hours, salary_only=salary_only,
             visa=visa, no_degree=no_degree)
    jobs, total = await asyncio.to_thread(db.search, f, sort, limit, offset)
    out: dict[str, Any] = {"jobs": jobs, "total": total, "offset": offset, "limit": limit}
    if facets:
        out["facets"] = await asyncio.to_thread(db.facets, f)
    return out


@app.get("/api/jobs/{job_id:path}")
async def job_detail(job_id: str):
    job = await asyncio.to_thread(db.get, job_id)
    if not job:
        raise HTTPException(404, "This job is no longer available.")
    if not job.get("description") and job["source"] == "smartrecruiters" and web.get("client"):
        try:
            desc = await smartrecruiters_description(web["client"], job_id.split(":", 1)[1])
            if desc:
                job["description"] = desc
                await asyncio.to_thread(db.set_description, job_id, desc)
        except Exception as e:
            log.warning("detail fetch failed for %s: %s", job_id, e)
    if not job.get("description") and job["source"] == "workday" and web.get("client"):
        try:
            extra = await feeds.workday_description(web["client"], job_id.split(":", 1)[1], job["url"])
            if extra.get("description"):
                job["description"] = extra["description"]
                if extra.get("time_type") and "part" in extra["time_type"].lower() and job["job_type"] == "Full-time":
                    job["job_type"] = "Part-time"
                await asyncio.to_thread(db.set_description, job_id, job["description"], job["job_type"])
        except Exception as e:
            log.warning("workday detail failed for %s: %s", job_id, e)
    if not job.get("salary") and job.get("description"):
        job["salary"] = extract_salary(job["description"])
    job["facts"] = key_facts(job)
    return job


@app.get("/api/meta")
async def meta():
    return {
        "categories": CATEGORIES,
        "role_groups": ROLE_GROUPS,
        "job_types": JOB_TYPES,
        "levels": LEVELS,
        "work_settings": WORK_SETTINGS,
        "sources": [{"id": k, **v} for k, v in (await asyncio.to_thread(db.get_meta_json, "source_status", {})).items()],
        "stats": await asyncio.to_thread(db.stats),
        "server_time": time.time(),
    }


_last_manual_refresh = 0.0


@app.post("/api/refresh")
async def refresh():
    global _last_manual_refresh
    if time.time() - _last_manual_refresh < 60:
        return {"started": False, "reason": "A refresh ran less than a minute ago."}
    _last_manual_refresh = time.time()
    await asyncio.to_thread(db.set_meta_json, "refresh_requested", True)  # the collector picks this up
    return {"started": True}


@app.get("/api/stream")
async def stream(request: Request):
    """Server-Sent Events: pushes {type: new_jobs} the moment the poller finds them."""
    q = hub.subscribe()

    async def events():
        try:
            yield "retry: 5000\n\n"
            yield f"data: {json.dumps({'type': 'hello'})}\n\n"
            while not await request.is_disconnected():
                try:
                    event = await asyncio.wait_for(q.get(), timeout=20)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            hub.unsubscribe(q)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/logo/{domain}")
async def logo(domain: str):
    client = web.get("client")
    found = await logos.get_logo(client, domain) if client else None
    if not found:
        raise HTTPException(404, "No logo")
    data, ctype = found
    return Response(data, media_type=ctype, headers={"Cache-Control": "public, max-age=604800"})


# --------------------------------------------------------------------------- #
# Cover letters (free: built on this server, no outside services)
# --------------------------------------------------------------------------- #
class LetterRequest(BaseModel):
    resume: str = Field(min_length=30, max_length=60000)
    job_id: Optional[str] = None
    job_title: str = ""
    company: str = ""
    job_description: str = Field("", max_length=60000)


@app.post("/api/cover-letter")
async def cover_letter(req: LetterRequest):
    job: dict[str, Any] = {"title": req.job_title, "company": req.company, "description": req.job_description}
    if req.job_id:
        stored = await asyncio.to_thread(db.get, req.job_id)
        if stored:
            job = {**stored, "title": req.job_title or stored["title"], "company": req.company or stored["company"],
                   "description": req.job_description or stored.get("description") or ""}
    if not job["title"].strip() or not job["company"].strip():
        raise HTTPException(400, "Add the job title and company, or pick a job from the list.")
    return await asyncio.to_thread(letter.build_cover_letter, req.resume, job)


class PdfRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20000)
    filename: str = "cover-letter"


@app.post("/api/cover-letter/pdf")
async def cover_letter_pdf(req: PdfRequest):
    data = await asyncio.to_thread(letter.letter_to_pdf, req.text)
    safe = re.sub(r"[^\w\- ]+", "", req.filename).strip() or "cover-letter"
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{safe}.pdf"'})


@app.post("/api/resume/parse")
async def parse_resume(file: UploadFile = File(...)):
    data = await file.read()
    if len(data) > 8 * 1024 * 1024:
        raise HTTPException(413, "That file is too large (8 MB max).")
    try:
        text = await asyncio.to_thread(letter.extract_resume_text, file.filename or "", data)
    except Exception:
        raise HTTPException(400, "Couldn't read that file. Try a PDF, Word (.docx) or text file, or paste your resume.")
    if not text:
        raise HTTPException(400, "No text found in that file. If it's a scanned PDF, paste the text instead.")
    return {"text": text}


# --------------------------------------------------------------------------- #
# Frontend
# --------------------------------------------------------------------------- #
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")
