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

log = logging.getLogger("postedfresh.poller")

# A job counts as "just posted" (and is pushed to browsers) only if it was
# published recently, so old postings found by a deep sweep aren't announced.
ANNOUNCE_WITHIN = timedelta(hours=6)


def _quick_every(name: str) -> int:
    s = SOURCES[name]
    return s.get("quick_every") or s.get("full_every") or config.POLL_INTERVAL_SECONDS


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
           "last_ok": None, "count": 0, "new_last_run": 0, "error": None, "interval": s.get("quick_every") or s.get("full_every"),
           "full_every": s.get("full_every"), "last_full": None, "next_run": None, "running": False}
    for name, s in SOURCES.items()
}


async def poll_source(name: str, client, mode: str = "full") -> int:
    st = status[name]
    st["running"] = True
    started = time.time()
    try:
        fetch = SOURCES[name]["quick"] if mode == "quick" and SOURCES[name].get("quick") else SOURCES[name]["fetch"]
        result = await fetch(client)
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
        if alive:
            st["count"] = sum(len(v) for v in alive.values())
        else:
            st["count"] = await asyncio.to_thread(_source_count, name)
        st.update(new_last_run=len(new), error=None, last_ok=time.time())
        if mode == "full":
            st["last_full"] = time.time()
        # A day-only job that appears during a quick check is genuinely new: we saw it
        # within about a minute of it going up, so its time becomes the moment we saw it.
        if mode == "quick" and had_baseline:
            fresh_day = [j for j in new if j.get("posted_precision") == "day"]
            await asyncio.to_thread(db.confirm_fresh, [j["id"] for j in fresh_day])
            for j in fresh_day:
                j["posted_at"], j["posted_precision"] = j["first_seen_at"], "time"
        # Only announce jobs that were actually published recently, with a real time.
        recent_cutoff = (datetime.now(timezone.utc) - ANNOUNCE_WITHIN).isoformat()
        new = ([j for j in new if j["posted_at"] >= recent_cutoff and j.get("posted_precision", "time") == "time"]
               if had_baseline else new)
        if new and had_baseline:
            new.sort(key=lambda j: j["posted_at"], reverse=True)
            await asyncio.to_thread(db.add_event, {
                "type": "new_jobs",
                "source": name,
                "count": len(new),
                "jobs": [_brief(j) for j in new[:50]],
            })
        log.info("%s %s: %d jobs, %d new (%.1fs)", name, mode, len(jobs), len(new), time.time() - started)
        return len(new)
    except Exception as e:
        st["error"] = f"{type(e).__name__}: {e}"[:300]
        log.warning("%s failed: %s", name, e)
        return 0
    finally:
        st["running"] = False
        st["last_run"] = time.time()
        await asyncio.to_thread(_save_status)


def _save_status() -> None:
    """Share each source's health with the website (it runs in another process)."""
    try:
        db.set_meta_json("source_status", {k: {kk: vv for kk, vv in v.items() if kk != "running"}
                                           for k, v in status.items()})
    except Exception as e:  # status is nice-to-have; never let it break collecting
        log.warning("saving status failed: %s", e)


def _source_count(name: str) -> int:
    with db._lock:
        return db.conn().execute("SELECT COUNT(*) FROM jobs WHERE source=?", (name,)).fetchone()[0]


def _source_has_rows(name: str) -> bool:
    with db._lock:
        return db.conn().execute("SELECT 1 FROM jobs WHERE source=? LIMIT 1", (name,)).fetchone() is not None


def _brief(j: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "source", "title", "company", "department", "location", "location_display", "remote", "workplace",
            "visa", "no_degree", "posted_precision", "category",
            "job_type", "level", "salary", "url", "logo", "posted_at", "first_seen_at")
    return {k: j.get(k) for k in keys}


async def _loop(name: str, client) -> None:
    """Quick checks for the newest jobs on a short cycle; a full sweep whenever it's due."""
    src = SOURCES[name]
    while True:
        st = status[name]
        due_full = not src.get("quick") or st["last_full"] is None or time.time() - st["last_full"] >= src["full_every"]
        await poll_source(name, client, "full" if due_full else "quick")
        interval = _quick_every(name)
        st["next_run"] = time.time() + interval
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
