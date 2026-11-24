"""JobRadar API + static frontend."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import config, db, poller
from .categorize import CATEGORIES
from .normalize import JOB_TYPES, LEVELS, extract_salary, key_facts
from .sources import SOURCES, smartrecruiters_description

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
STATIC = config.ROOT / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.conn()
    task = asyncio.create_task(poller.run_forever())
    yield
    task.cancel()


app = FastAPI(title="JobRadar", version="1.0", lifespan=lifespan)


def _filters(
    q: str = "", category: str = "", job_type: str = "", level: str = "", source: str = "",
    company: str = "", department: str = "", location: str = "", remote: str = "any",
    posted_within_hours: int = 0, salary_only: bool = False,
) -> dict[str, Any]:
    return dict(q=q, category=category, job_type=job_type, level=level, source=source, company=company,
                department=department, location=location, remote=remote,
                posted_within_hours=posted_within_hours, salary_only=salary_only)


@app.get("/api/jobs")
async def list_jobs(
    q: str = "", category: str = "", job_type: str = "", level: str = "", source: str = "",
    company: str = "", department: str = "", location: str = "", remote: str = "any",
    posted_within_hours: int = 0, salary_only: bool = False,
    sort: str = "newest", limit: int = Query(40, le=200), offset: int = 0, facets: bool = True,
):
    f = _filters(q, category, job_type, level, source, company, department, location, remote,
                 posted_within_hours, salary_only)
    jobs, total = await asyncio.to_thread(db.search, f, sort, limit, offset)
    out: dict[str, Any] = {"jobs": jobs, "total": total, "offset": offset, "limit": limit}
    if facets:
        out["facets"] = await asyncio.to_thread(db.facets, f)
    return out


@app.get("/api/jobs/{job_id:path}")
async def job_detail(job_id: str):
    job = await asyncio.to_thread(db.get, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if not job.get("description") and job["source"] == "smartrecruiters" and poller.app_client.get("client"):
        try:
            desc = await smartrecruiters_description(poller.app_client["client"], job_id.split(":", 1)[1])
            if desc:
                job["description"] = desc
                await asyncio.to_thread(db.set_description, job_id, desc)
        except Exception as e:
            logging.getLogger("jobradar").warning("detail fetch failed for %s: %s", job_id, e)
    if not job.get("salary") and job.get("description"):
        job["salary"] = extract_salary(job["description"])
    job["facts"] = key_facts(job)
    return job


@app.get("/api/meta")
async def meta():
    return {
        "categories": CATEGORIES,
        "job_types": JOB_TYPES,
        "levels": LEVELS,
        "sources": [{"id": k, **{kk: vv for kk, vv in v.items() if kk != "running"}} for k, v in poller.status.items()],
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
    asyncio.create_task(poller.refresh_all())
    return {"started": True}


@app.get("/api/stream")
async def stream(request: Request):
    """Server-Sent Events: pushes {type: new_jobs} the moment the poller finds them."""
    q = poller.hub.subscribe()

    async def events():
        try:
            yield "retry: 5000\n\n"
            yield f"data: {json.dumps({'type': 'hello', 'stats': db.stats()})}\n\n"
            while not await request.is_disconnected():
                try:
                    event = await asyncio.wait_for(q.get(), timeout=20)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            poller.hub.unsubscribe(q)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ------------------------------- Frontend ---------------------------------- #
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")
