"""The collector: gathers jobs from every source and saves them.

It runs as its own process next to the website, so the work of reading and
sorting thousands of postings never slows down a visitor's search. The website
starts it automatically and reads what it saves:
  * new jobs and filters -> the jobs table,
  * "just posted" announcements -> the events table (pushed to browsers),
  * each source's health -> a status record shown on the "How it works" page.

Run on its own with:  python -m app.worker
"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import logging
import os
import sys
import time

from . import config, db, feeds, logos, poller
from .sources import new_client, smartrecruiters_description

log = logging.getLogger("postedfresh.worker")


def _single_instance() -> object | None:
    """Only one collector may run at a time (two would just double the work)."""
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = open(config.DB_PATH.parent / "collector.lock", "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return None
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


async def _watch_parent(parent: int | None) -> None:
    """Stop when the website stops, so no collector is ever left running on its own."""
    while True:
        if parent and os.getppid() != parent:
            log.info("website stopped; collector exiting")
            os._exit(0)
        await asyncio.sleep(5)


async def _watch_refresh_requests() -> None:
    """The website can ask for an immediate check of every source."""
    while True:
        if db.get_meta_json("refresh_requested"):
            db.set_meta_json("refresh_requested", False)
            asyncio.create_task(poller.refresh_all())
        await asyncio.sleep(5)


async def _fill_details() -> None:
    """Some sources (Workday, SmartRecruiters) only give a job's description and full
    list of locations on its own page. Read those pages gradually, newest jobs first,
    so filters like location, visa sponsorship and degree work for them too."""
    async with new_client() as client:
        sem = asyncio.Semaphore(4)

        async def one(job_id: str, source: str, url: str) -> None:
            async with sem:
                try:
                    native = job_id.split(":", 1)[1]
                    if source == "workday":
                        d = await feeds.workday_description(client, native, url)
                        await asyncio.to_thread(db.apply_details, job_id, d.get("description") or "",
                                                d.get("location"), d.get("time_type"))
                    else:
                        desc = await smartrecruiters_description(client, native)
                        await asyncio.to_thread(db.apply_details, job_id, desc or "")
                except Exception as e:  # skip this one; it's retried later only if still missing
                    log.debug("details for %s failed: %s", job_id, e)
                    await asyncio.to_thread(db.apply_details, job_id, "")

        while True:
            batch = await asyncio.to_thread(db.jobs_missing_details, 16)
            if batch:
                await asyncio.gather(*(one(*b) for b in batch))
                await asyncio.sleep(3)
            else:
                await asyncio.sleep(30)


async def _fill_logos() -> None:
    """Find logos for companies that came without one, gently (about one lookup a
    second), and download every logo ahead of time so cards never sit blank."""
    async with new_client() as client:
        last_share = 0.0
        while True:
            names = []
            try:
                if time.time() - last_share > 120:  # sharing touches many rows; do it every couple of minutes
                    shared = await asyncio.to_thread(db.share_logos)
                    last_share = time.time()
                    if shared:
                        log.info("logos: %d jobs got a logo", shared)
                names = await asyncio.to_thread(db.companies_needing_lookup, 120)
                for name in names:
                    try:
                        domain = await logos.lookup_domain(client, name)
                    except Exception as e:  # lookup unavailable: try again next round
                        log.debug("logo lookup failed for %s: %s", name, e)
                        break
                    await asyncio.to_thread(db.remember_domain, name, domain)
                    await asyncio.sleep(0.5)
                for domain in await asyncio.to_thread(db.logo_domains):
                    if not any((logos.LOGO_DIR / f"{domain}.{ext}").exists() for ext in ("png", "ico", "none")):
                        await logos.get_logo(client, domain)
                        await asyncio.sleep(0.3)
            except Exception as e:
                log.warning("logo filling failed: %s", e)
            await asyncio.sleep(5 if names else 300)


async def run(parent: int | None) -> None:
    db.conn()
    asyncio.create_task(asyncio.to_thread(db.reclassify_if_needed))
    await asyncio.gather(poller.run_forever(), _fill_details(), _fill_logos(), _watch_parent(parent),
                         _watch_refresh_requests())


def main() -> None:
    parser = argparse.ArgumentParser(description="PostedFresh job collector")
    parser.add_argument("--parent", type=int, default=None, help="exit when this process (the website) exits")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    lock = _single_instance()
    if lock is None:
        log.info("another collector is already running; exiting")
        sys.exit(0)
    asyncio.run(run(args.parent))


if __name__ == "__main__":
    main()
