"""SQLite storage for normalized job postings."""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from . import locations, logos
from .config import DB_PATH

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,        -- "<source>:<native id>"
    source        TEXT NOT NULL,           -- e.g. "greenhouse", "remotive"
    title         TEXT NOT NULL,
    company       TEXT NOT NULL,
    department    TEXT,
    location      TEXT,
    remote        INTEGER NOT NULL DEFAULT 0,
    category      TEXT NOT NULL,           -- normalized (see categorize.py)
    job_type      TEXT NOT NULL,           -- Full-time, Part-time, Internship, Seasonal, ...
    level         TEXT NOT NULL,           -- Internship, Entry, Mid, Senior, Manager, Executive
    salary        TEXT,
    tags          TEXT,                    -- JSON array
    url           TEXT NOT NULL,
    logo          TEXT,
    posted_at     TEXT NOT NULL,           -- ISO-8601 UTC
    first_seen_at TEXT NOT NULL,           -- when PostedFresh first saw it
    description   TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_posted   ON jobs(posted_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_seen     ON jobs(first_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_category ON jobs(category);
CREATE INDEX IF NOT EXISTS idx_jobs_company  ON jobs(company);
CREATE INDEX IF NOT EXISTS idx_jobs_type     ON jobs(job_type);

-- Full-text index for keyword search, kept in sync by triggers.
CREATE VIRTUAL TABLE IF NOT EXISTS jobs_fts USING fts5(
    title, company, department, tags, description,
    content='jobs', content_rowid='rowid', tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS jobs_ai AFTER INSERT ON jobs BEGIN
    INSERT INTO jobs_fts(rowid, title, company, department, tags, description)
    VALUES (new.rowid, new.title, new.company, new.department, new.tags, new.description);
END;
CREATE TRIGGER IF NOT EXISTS jobs_ad AFTER DELETE ON jobs BEGIN
    INSERT INTO jobs_fts(jobs_fts, rowid, title, company, department, tags, description)
    VALUES ('delete', old.rowid, old.title, old.company, old.department, old.tags, old.description);
END;
CREATE TRIGGER IF NOT EXISTS jobs_au AFTER UPDATE ON jobs BEGIN
    INSERT INTO jobs_fts(jobs_fts, rowid, title, company, department, tags, description)
    VALUES ('delete', old.rowid, old.title, old.company, old.department, old.tags, old.description);
    INSERT INTO jobs_fts(rowid, title, company, department, tags, description)
    VALUES (new.rowid, new.title, new.company, new.department, new.tags, new.description);
END;

-- Clean places for each job (city, state/region, country, area), for the location filter.
CREATE TABLE IF NOT EXISTS job_places (
    job_id TEXT NOT NULL,
    place  TEXT NOT NULL,
    kind   TEXT NOT NULL,                  -- country | region | city | area
    PRIMARY KEY (job_id, place)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_places_place ON job_places(place);
CREATE TRIGGER IF NOT EXISTS jobs_places_ad AFTER DELETE ON jobs BEGIN
    DELETE FROM job_places WHERE job_id = old.id;
END;
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

# Bump when the location parser changes so stored places are rebuilt.
LOCATIONS_VERSION = "1"

FIELDS = [
    "title", "company", "department", "location", "remote", "category", "job_type",
    "level", "salary", "tags", "url", "logo", "posted_at", "description",
]

LIST_COLUMNS = (
    "id, source, title, company, department, location, location_display, remote, category, job_type, level, "
    "salary, tags, url, logo, posted_at, first_seen_at, substr(description, 1, 240) AS excerpt"
)

# Filters that accept a comma-separated list of exact values.
MULTI_FILTERS = ("category", "job_type", "level", "source", "company", "department")


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        fresh_fts = _conn.execute("SELECT 1 FROM sqlite_master WHERE name='jobs_fts'").fetchone() is None
        _conn.executescript(SCHEMA)
        cols = {r[1] for r in _conn.execute("PRAGMA table_info(jobs)")}
        if "location_display" not in cols:
            _conn.execute("ALTER TABLE jobs ADD COLUMN location_display TEXT")
        ver = _conn.execute("SELECT value FROM meta WHERE key='locations_version'").fetchone()
        if not ver or ver[0] != LOCATIONS_VERSION:
            _rebuild_places(_conn)
        _fill_logos(_conn)
        if fresh_fts:  # index jobs stored before full-text search existed
            _conn.execute("INSERT INTO jobs_fts(jobs_fts) VALUES ('rebuild')")
            _conn.commit()
    return _conn


def _set_places(c: sqlite3.Connection, job_id: str, location: str | None, remote: bool) -> str:
    places = locations.parse(location)
    c.execute("UPDATE jobs SET location_display=? WHERE id=?", (locations.display(places, remote, location), job_id))
    c.execute("DELETE FROM job_places WHERE job_id=?", (job_id,))
    c.executemany(
        "INSERT OR IGNORE INTO job_places (job_id, place, kind) VALUES (?,?,?)",
        [(job_id, label, kind) for label, kind in locations.filter_entries(places, remote)],
    )
    return locations.display(places, remote, location)


def _rebuild_places(c: sqlite3.Connection) -> None:
    """Re-parse every stored job's location (first run, or after the parser improves)."""
    for job_id, location, remote in c.execute("SELECT id, location, remote FROM jobs").fetchall():
        _set_places(c, job_id, location, bool(remote))
    c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('locations_version', ?)", (LOCATIONS_VERSION,))
    c.commit()


def _fill_logos(c: sqlite3.Connection) -> None:
    """Give stored company-board jobs a logo link (new jobs get one when they arrive)."""
    rows = c.execute("SELECT id FROM jobs WHERE logo IS NULL OR logo = ''").fetchall()
    updates = [(url, jid) for (jid,) in rows if (url := logos.logo_url(jid, None))]
    if updates:
        c.executemany("UPDATE jobs SET logo=? WHERE id=?", updates)
        c.commit()


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _row(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    d["remote"] = bool(d["remote"])
    d["tags"] = json.loads(d["tags"] or "[]")
    return d


def count() -> int:
    with _lock:
        return conn().execute("SELECT COUNT(*) FROM jobs").fetchone()[0]


def _values(j: dict[str, Any]) -> list[Any]:
    out = []
    for f in FIELDS:
        v = j.get(f)
        if f == "tags":
            v = json.dumps(v or [])
        elif f == "remote":
            v = int(bool(v))
        out.append(v)
    return out


def upsert_jobs(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Insert new jobs; refresh fields on known ones. Returns only the new ones."""
    seen = now_iso()
    new: list[dict[str, Any]] = []
    sets = ", ".join(f"{f}=?" for f in FIELDS)
    # Keep an already-fetched description if this source only gives one lazily.
    sets = sets.replace("description=?", "description=COALESCE(?, description)")
    cols = ", ".join(FIELDS)
    with _lock:
        c = conn()
        ids = [j["id"] for j in jobs]
        existing: set[str] = set()
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            existing.update(
                r[0] for r in c.execute(f"SELECT id FROM jobs WHERE id IN ({','.join('?' * len(chunk))})", chunk)
            )
        for j in jobs:
            vals = _values(j)
            if j["id"] in existing:
                c.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*vals, j["id"]))
            else:
                c.execute(
                    f"INSERT INTO jobs ({cols}, id, source, first_seen_at) VALUES ({','.join('?' * (len(FIELDS) + 3))})",
                    (*vals, j["id"], j["source"], seen),
                )
                existing.add(j["id"])
                new.append({**j, "first_seen_at": seen})
            new_display = _set_places(c, j["id"], j.get("location"), bool(j.get("remote")))
            if new and new[-1]["id"] == j["id"]:
                new[-1]["location_display"] = new_display
        c.commit()
    return new


def set_description(job_id: str, description: str) -> None:
    with _lock:
        conn().execute("UPDATE jobs SET description=? WHERE id=?", (description, job_id))
        conn().commit()


# Company boards tell us when a job closes (it disappears from the board), so
# their jobs stay as long as they're listed. Feed sources get an age cutoff.
CLOSURE_TRACKED = ("greenhouse", "lever", "ashby")


def prune(max_age_days: int) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
    with _lock:
        cur = conn().execute(
            f"DELETE FROM jobs WHERE posted_at < ? AND source NOT IN ({','.join('?' * len(CLOSURE_TRACKED))})",
            (cutoff, *CLOSURE_TRACKED),
        )
        conn().commit()
        return cur.rowcount


def _build_where(filters: dict[str, Any]) -> tuple[str, list[Any]]:
    where, args = [], []
    terms = [re.sub(r'["*^():]', " ", t).strip() for t in (filters.get("q") or "").split()]
    terms = [t for t in terms if t]
    if terms:
        # Every word must appear somewhere in the posting.
        match = " AND ".join(f'"{t}"' for t in terms)
        where.append("rowid IN (SELECT rowid FROM jobs_fts WHERE jobs_fts MATCH ?)")
        args.append(match)
    places = [v for v in (filters.get("place") or "").split("|") if v]
    if places:
        where.append(f"id IN (SELECT job_id FROM job_places WHERE place IN ({','.join('?' * len(places))}))")
        args += places
    for key in MULTI_FILTERS:
        vals = [v for v in (filters.get(key) or "").split("|") if v]
        if vals:
            where.append(f"{key} IN ({','.join('?' * len(vals))})")
            args += vals
    remote = filters.get("remote") or "any"
    if remote == "remote":
        where.append("remote = 1")
    elif remote == "onsite":
        where.append("remote = 0")
    hours = int(filters.get("posted_within_hours") or 0)
    if hours > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        where.append("posted_at >= ?")
        args.append(cutoff)
    if filters.get("salary_only"):
        where.append("salary IS NOT NULL AND salary != ''")
    return (("WHERE " + " AND ".join(where)) if where else ""), args


def search(filters: dict[str, Any], sort: str = "newest", limit: int = 50, offset: int = 0):
    clause, args = _build_where(filters)
    if sort == "relevance" and (filters.get("q") or "").strip():
        # bm25 with title weighted highest, then company/department, then body text.
        terms = [re.sub(r'["*^():]', " ", t).strip() for t in filters["q"].split()]
        match = " AND ".join(f'"{t}"' for t in terms if t)
        with _lock:
            c = conn()
            total = c.execute(f"SELECT COUNT(*) FROM jobs {clause}", args).fetchone()[0]
            rows = c.execute(
                f"SELECT {LIST_COLUMNS} FROM jobs JOIN (SELECT rowid AS rid, bm25(jobs_fts, 12.0, 6.0, 4.0, 3.0, 1.0) AS rank "
                f"FROM jobs_fts WHERE jobs_fts MATCH ?) r ON r.rid = jobs.rowid {clause} "
                f"ORDER BY r.rank + (julianday('now') - julianday(posted_at)) * 0.05 LIMIT ? OFFSET ?",
                [match, *args, limit, offset],
            ).fetchall()
        return [_row(r) for r in rows], total
    order = {
        "discovered": "first_seen_at DESC, posted_at DESC",
        "company": "company COLLATE NOCASE ASC, posted_at DESC",
        "title": "title COLLATE NOCASE ASC",
    }.get(sort, "posted_at DESC")
    with _lock:
        c = conn()
        total = c.execute(f"SELECT COUNT(*) FROM jobs {clause}", args).fetchone()[0]
        rows = c.execute(
            f"SELECT {LIST_COLUMNS} FROM jobs {clause} ORDER BY {order} LIMIT ? OFFSET ?",
            [*args, limit, offset],
        ).fetchall()
    return [_row(r) for r in rows], total


def get(job_id: str) -> dict[str, Any] | None:
    with _lock:
        r = conn().execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return _row(r) if r else None


def facets(filters: dict[str, Any]) -> dict[str, list[list[Any]]]:
    """Value counts for each filter, computed with all *other* filters applied."""
    out: dict[str, list[list[Any]]] = {}
    limits = {"company": 600}
    with _lock:
        c = conn()
        others = {k: v for k, v in filters.items() if k != "place"}
        clause, args = _build_where(others)
        rows = c.execute(
            f"SELECT p.place, p.kind, COUNT(*) n FROM job_places p JOIN jobs ON jobs.id = p.job_id {clause} "
            f"GROUP BY p.place, p.kind ORDER BY n DESC LIMIT 4000",
            args,
        ).fetchall()
        out["place"] = [[r[0], r[2], r[1]] for r in rows]
        for key in ("category", "job_type", "level", "company"):
            others = {k: v for k, v in filters.items() if k != key}
            clause, args = _build_where(others)
            extra = f"{'AND' if clause else 'WHERE'} {key} IS NOT NULL AND {key} != ''"
            rows = c.execute(
                f"SELECT {key}, COUNT(*) n FROM jobs {clause} {extra} GROUP BY {key} ORDER BY n DESC LIMIT ?",
                [*args, limits.get(key, 100)],
            ).fetchall()
            out[key] = [[r[0], r[1]] for r in rows]
    return out


def stats() -> dict[str, int]:
    hour = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    day = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    with _lock:
        c = conn()
        return {
            "total": c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
            "companies": c.execute("SELECT COUNT(DISTINCT company) FROM jobs").fetchone()[0],
            "last_hour": c.execute("SELECT COUNT(*) FROM jobs WHERE posted_at >= ?", (hour,)).fetchone()[0],
            "last_24h": c.execute("SELECT COUNT(*) FROM jobs WHERE posted_at >= ?", (day,)).fetchone()[0],
        }


def ids_with_prefix(prefix: str) -> set[str]:
    with _lock:
        return {r[0] for r in conn().execute("SELECT id FROM jobs WHERE id >= ? AND id < ?", (prefix, prefix + "\uffff"))}


def remove_closed(prefix: str, alive: set[str]) -> int:
    """Delete jobs from one company board that are no longer listed."""
    gone = ids_with_prefix(prefix) - alive
    with _lock:
        c = conn()
        for jid in gone:
            c.execute("DELETE FROM jobs WHERE id = ?", (jid,))
        c.commit()
    return len(gone)
