"""Background poller: re-checks every source on its own interval and pushes
newly discovered jobs to connected browsers over Server-Sent Events."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from . import config, db
from .sources import SOURCES, new_client

log = logging.getLogger("jobradar.poller")

# Seconds between checks for each source. Company boards are a single cheap
# request each, so they are checked often; paginated feeds less often.
INTERVALS = {
    "greenhouse": 45, "lever": 45, "ashby": 45,
    "remotive": 120, "remoteok": 120, "jobicy": 120, "himalayas": 120,
    "arbeitnow": 180, "smartrecruiters": 300,
}


class Hub:
    """Fan-out of new-job events to every open SSE connection."""

    def __init__(self) -> None:
        self.subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    def publish(self, event: dict[str, Any]) -> None:
        for q in list(self.subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass  # slow client; it will catch up on its next refresh


hub = Hub()

status: dict[str, dict[str, Any]] = {
    name: {"label": s["label"], "kind": s["kind"], "boards": s.get("boards"), "last_run": None,
           "last_ok": None, "count": 0, "new_last_run": 0, "error": None, "interval": INTERVALS.get(name, 300),
           "next_run": None, "running": False}
    for name, s in SOURCES.items()
}


async def poll_source(name: str, client) -> int:
    st = status[name]
    st["running"] = True
    started = time.time()
    try:
        result = await SOURCES[name]["fetch"](client)
        jobs, alive = result if isinstance(result, tuple) else (result, {})
        jobs = [j for j in jobs if j.get("url") and j.get("title")]
        if name not in db.CLOSURE_TRACKED:  # feeds: skip stale postings the janitor would prune
            age_cutoff = (datetime.now(timezone.utc) - timedelta(days=config.MAX_JOB_AGE_DAYS)).isoformat()
            jobs = [j for j in jobs if j["posted_at"] >= age_cutoff]
        # Only announce jobs as "new" once we have a baseline for this source,
        # otherwise the very first run would announce thousands of old jobs.
        had_baseline = st["last_ok"] is not None or _source_has_rows(name)
        new = await asyncio.to_thread(db.upsert_jobs, jobs)
        closed = 0
        for prefix, ids in alive.items():
            if ids:  # an empty list usually means a hiccup, not "every job closed"
                closed += await asyncio.to_thread(db.remove_closed, prefix, ids)
        st["closed_last_run"] = closed
        st.update(count=sum(len(v) for v in alive.values()) if alive else len(jobs), new_last_run=len(new), error=None, last_ok=time.time())
        # Don't push old postings we merely (re)discovered, e.g. after downtime.
        recent_cutoff = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        new = [j for j in new if j["posted_at"] >= recent_cutoff] if had_baseline else new
        if new and had_baseline:
            new.sort(key=lambda j: j["posted_at"], reverse=True)
            hub.publish({
                "type": "new_jobs",
                "source": name,
                "count": len(new),
                "jobs": [_brief(j) for j in new[:50]],
            })
        log.info("%s: %d jobs, %d new (%.1fs)", name, len(jobs), len(new), time.time() - started)
        return len(new)
    except Exception as e:
        st["error"] = f"{type(e).__name__}: {e}"[:300]
        log.warning("%s failed: %s", name, e)
        return 0
    finally:
        st["running"] = False
        st["last_run"] = time.time()


def _source_has_rows(name: str) -> bool:
    with db._lock:
        return db.conn().execute("SELECT 1 FROM jobs WHERE source=? LIMIT 1", (name,)).fetchone() is not None


def _brief(j: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "source", "title", "company", "department", "location", "remote", "category",
            "job_type", "level", "salary", "url", "logo", "posted_at", "first_seen_at")
    return {k: j.get(k) for k in keys}


async def _loop(name: str, client) -> None:
    while True:
        await poll_source(name, client)
        interval = INTERVALS.get(name, config.POLL_INTERVAL_SECONDS)
        status[name]["next_run"] = time.time() + interval
        await asyncio.sleep(interval)


async def _janitor() -> None:
    while True:
        removed = await asyncio.to_thread(db.prune, config.MAX_JOB_AGE_DAYS)
        if removed:
            log.info("pruned %d jobs older than %d days", removed, config.MAX_JOB_AGE_DAYS)
        await asyncio.sleep(3600)


async def run_forever() -> None:
    async with new_client() as client:
        app_client["client"] = client
        await asyncio.gather(*(_loop(n, client) for n in SOURCES), _janitor())


# Shared HTTP client, also used for on-demand detail fetches.
app_client: dict[str, Any] = {}


async def refresh_all() -> int:
    client = app_client.get("client")
    if client is None:
        return 0
    results = await asyncio.gather(*(poll_source(n, client) for n in SOURCES if not status[n]["running"]))
    return sum(results)
