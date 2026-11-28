"""SQLite storage for normalized job postings."""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from . import locations, logos
from .normalize import title_key
from .config import DB_PATH

_lock = threading.RLock()          # serializes writes
_conn: sqlite3.Connection | None = None
_local = threading.local()          # one read connection per worker thread
_facet_cache: dict[str, tuple[float, Any]] = {}
FACET_TTL = 20.0                    # seconds; counts refresh often enough for a live board


def reader() -> sqlite3.Connection:
    """A read-only connection for this thread. With WAL, reads run in parallel
    with each other and with the poller's writes."""
    c = getattr(_local, "conn", None)
    if c is None:
        conn()  # make sure the schema exists
        c = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA query_only=1")
        _tune(c)
        _local.conn = c
    return c

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,        -- "<source>:<native id>"
    source        TEXT NOT NULL,           -- e.g. "greenhouse", "workday"
    is_dup        INTEGER NOT NULL DEFAULT 0,  -- hidden duplicate of another listing
    category      TEXT NOT NULL,           -- role (see categorize.py)
    job_type      TEXT NOT NULL,           -- Full-time, Part-time, Internship, Seasonal, ...
    level         TEXT NOT NULL,           -- Internship, Entry, Mid, Senior, Manager, Executive
    workplace     TEXT,                    -- Remote, Hybrid, On-site
    remote        INTEGER NOT NULL DEFAULT 0,
    visa          TEXT,                    -- Yes / No / unknown
    no_degree     INTEGER NOT NULL DEFAULT 0,
    posted_at     TEXT NOT NULL,           -- ISO-8601 UTC
    posted_precision TEXT NOT NULL DEFAULT 'time',  -- 'day' when the source only gives a date
    first_seen_at TEXT NOT NULL,           -- when PostedFresh first saw it
    company       TEXT NOT NULL,
    title         TEXT NOT NULL,
    department    TEXT,
    salary        TEXT,
    location      TEXT,
    location_display TEXT,
    url           TEXT NOT NULL,
    logo          TEXT,
    ckey          TEXT,                    -- company + title, for spotting duplicates
    dkey          TEXT,                    -- company + title + place
    tags          TEXT,                    -- JSON array
    description   TEXT                     -- long text last, so filtering never reads it
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

-- Company name -> website, found once by name lookup ('' = looked up, no confident match).
CREATE TABLE IF NOT EXISTS company_domains (
    company  TEXT PRIMARY KEY,          -- lowercase company name as shown on jobs
    domain   TEXT NOT NULL,
    checked  REAL NOT NULL
);

-- "Just posted" announcements from the collector, read by the website and pushed to browsers.
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    created REAL NOT NULL,
    payload TEXT NOT NULL
);
"""

# Bump when the location parser changes so stored places are rebuilt.
LOCATIONS_VERSION = "3"
# Bump when the role / job type / level rules change so stored jobs are re-checked.
CLASSIFY_VERSION = "10"

FIELDS = [
    "title", "company", "department", "location", "remote", "category", "job_type",
    "level", "salary", "tags", "url", "logo", "posted_at", "description",
    "workplace", "visa", "no_degree", "posted_precision",
]

LIST_COLUMNS = (
    "id, source, title, company, department, location, location_display, remote, workplace, visa, no_degree, posted_precision, category, job_type, level, "
    "salary, tags, url, logo, posted_at, first_seen_at, substr(description, 1, 240) AS excerpt"
)

# Filters that accept a comma-separated list of exact values.
MULTI_FILTERS = ("category", "job_type", "level", "source", "company", "department")


_COLUMN_ORDER = [
    "id", "source", "is_dup", "category", "job_type", "level", "workplace", "remote", "visa", "no_degree",
    "posted_at", "posted_precision", "first_seen_at", "company", "title", "department", "salary", "location",
    "location_display", "url", "logo", "ckey", "dkey", "tags", "description",
]


def _tune(c: sqlite3.Connection) -> None:
    c.execute("PRAGMA cache_size=-65536")      # 64 MB page cache per connection
    c.execute("PRAGMA mmap_size=536870912")    # read through memory mapping
    c.execute("PRAGMA temp_store=MEMORY")


def _ensure_layout(c: sqlite3.Connection) -> None:
    """Older databases stored descriptions in the middle of each row and added
    filter columns after them, so every count had to read past the long text.
    Rebuild once with small columns first and descriptions last (row ids are
    kept, so the full-text index stays valid)."""
    cols = [r[1] for r in c.execute("PRAGMA table_info(jobs)")]
    if not cols or cols[-1] == "description" or set(_COLUMN_ORDER) - set(cols):
        return
    keep = ", ".join(_COLUMN_ORDER)
    ddl = SCHEMA.split("CREATE TABLE IF NOT EXISTS jobs", 1)[1].split(";", 1)[0]
    c.executescript(f"""
        DROP TRIGGER IF EXISTS jobs_ai; DROP TRIGGER IF EXISTS jobs_ad; DROP TRIGGER IF EXISTS jobs_au;
        DROP TRIGGER IF EXISTS jobs_places_ad;
        CREATE TABLE jobs_new {ddl};
        INSERT INTO jobs_new (rowid, {keep}) SELECT rowid, {keep} FROM jobs;
        DROP TABLE jobs;
        ALTER TABLE jobs_new RENAME TO jobs;
    """)
    c.executescript(SCHEMA)  # indexes and triggers
    c.commit()


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _tune(_conn)
        fresh_fts = _conn.execute("SELECT 1 FROM sqlite_master WHERE name='jobs_fts'").fetchone() is None
        _conn.executescript(SCHEMA)
        cols = {r[1] for r in _conn.execute("PRAGMA table_info(jobs)")}
        if "location_display" not in cols:
            _conn.execute("ALTER TABLE jobs ADD COLUMN location_display TEXT")
        for col, decl in (("workplace", "TEXT"), ("visa", "TEXT"), ("no_degree", "INTEGER NOT NULL DEFAULT 0"),
                          ("posted_precision", "TEXT NOT NULL DEFAULT 'time'"), ("ckey", "TEXT"),
                          ("dkey", "TEXT"), ("is_dup", "INTEGER NOT NULL DEFAULT 0")):
            if col not in cols:
                _conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {decl}")
        _ensure_layout(_conn)
        _conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_workplace ON jobs(workplace)")
        _conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_ckey ON jobs(ckey)")
        _conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_company_lower ON jobs(lower(company))")
        _conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_visible ON jobs(is_dup, posted_at DESC)")
        # Compact index with just the filter columns: counting reads this instead of whole rows.
        _conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_filters ON jobs(is_dup, job_type, category, level, workplace, "
                      "visa, no_degree, posted_at, company)")
        _fill_logos(_conn)
        if fresh_fts:  # index jobs stored before full-text search existed
            _conn.execute("INSERT INTO jobs_fts(jobs_fts) VALUES ('rebuild')")
            _conn.commit()
    return _conn


def _set_places(c: sqlite3.Connection, job_id: str, location: str | None, remote: bool) -> str:
    places = locations.parse(location)
    shown = locations.display(places, remote, location)
    row = c.execute("SELECT company, title FROM jobs WHERE id=?", (job_id,)).fetchone()
    ckey = f"{(row[0] or '').strip().lower()}|{title_key(row[1])}" if row else None
    c.execute("UPDATE jobs SET location_display=?, ckey=?, dkey=? WHERE id=?",
              (shown, ckey, f"{ckey}|{shown.lower()}" if ckey else None, job_id))
    c.execute("DELETE FROM job_places WHERE job_id=?", (job_id,))
    c.executemany(
        "INSERT OR IGNORE INTO job_places (job_id, place, kind) VALUES (?,?,?)",
        [(job_id, label, kind) for label, kind in locations.filter_entries(places, remote)],
    )
    return locations.display(places, remote, location)


def _rebuild_places(c: sqlite3.Connection) -> None:
    """Re-parse every stored job's location (after the parser improves), in batches."""
    with _lock:
        rows = c.execute("SELECT id, location, remote FROM jobs").fetchall()
    for i in range(0, len(rows), 2000):
        with _lock:
            for job_id, location, remote in rows[i : i + 2000]:
                _set_places(c, job_id, location, bool(remote))
            c.commit()
    with _lock:
        c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('locations_version', ?)", (LOCATIONS_VERSION,))
        c.commit()


def reclassify_if_needed() -> None:
    """Re-check stored jobs after the rules change. Runs in the background so the
    site starts instantly; it works in batches so the poller keeps writing."""
    c = conn()
    ver = c.execute("SELECT value FROM meta WHERE key='locations_version'").fetchone()
    if not ver or ver[0] != LOCATIONS_VERSION:
        _rebuild_places(c)
    ver = c.execute("SELECT value FROM meta WHERE key='classify_version'").fetchone()
    if not ver or ver[0] != CLASSIFY_VERSION:
        _reclassify(c)


def _reclassify(c: sqlite3.Connection) -> None:
    """Re-check every stored job's role, job type and experience level with the current rules."""
    from .categorize import categorize
    from .normalize import job_type, level, no_degree_required, visa_sponsorship, work_setting

    with _lock:
        ids = [r[0] for r in c.execute("SELECT id FROM jobs").fetchall()]
    for i in range(0, len(ids), 2000):
        chunk = ids[i : i + 2000]
        with _lock:
            rows = c.execute(
                "SELECT id, title, department, tags, job_type, substr(description, 1, 15000), remote, location "
                f"FROM jobs WHERE id IN ({','.join('?' * len(chunk))})", chunk).fetchall()
        updates = []
        for jid, title, dept, tags, old_type, desc, remote, location in rows:
            jt = job_type(old_type or "", title=title, description=desc)
            updates.append((categorize(title, [dept or ""] + json.loads(tags or "[]")), jt, level(title, job_type=jt),
                            work_setting(bool(remote), location, title), visa_sponsorship(desc),
                            int(no_degree_required(desc)), jid))
        with _lock:
            c.executemany("UPDATE jobs SET category=?, job_type=?, level=?, workplace=?, visa=?, no_degree=? WHERE id=?", updates)
            c.commit()
    _facet_cache.clear()
    _place_cache.clear()
    with _lock:
        keyrows = c.execute("SELECT id, company, title, location_display FROM jobs").fetchall()
        upd = []
        for jid, company, title, shown in keyrows:
            ck = f"{(company or '').strip().lower()}|{title_key(title)}"
            upd.append((ck, f"{ck}|{(shown or '').lower()}", jid))
        c.executemany("UPDATE jobs SET ckey=?, dkey=? WHERE id=?", upd)
        _refresh_dups(c, {u[0] for u in upd})
        c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('classify_version', ?)", (CLASSIFY_VERSION,))
        c.commit()
    _facet_cache.clear()
    _place_cache.clear()


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
        elif f in ("remote", "no_degree"):
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
    # Keep the earliest posting time we've seen, and never replace a confirmed
    # time with a day-only date from a later sweep.
    sets = sets.replace("posted_at=?", "posted_at=CASE WHEN posted_precision='time' THEN posted_at ELSE MIN(posted_at, ?) END")
    sets = sets.replace("posted_precision=?", "posted_precision=CASE WHEN posted_precision='time' THEN 'time' ELSE ? END")
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
        # Fields that come from a job's own page (for sources whose lists have no
        # description): a later list check must not overwrite them with blanks.
        page_fields = ("location", "job_type", "level", "workplace", "visa", "no_degree", "description")
        light = [f for f in FIELDS if f not in page_fields]
        light_sets = ", ".join(f"{f}=?" for f in light)
        light_sets = light_sets.replace("posted_at=?", "posted_at=CASE WHEN posted_precision='time' THEN posted_at ELSE MIN(posted_at, ?) END")
        light_sets = light_sets.replace("posted_precision=?", "posted_precision=CASE WHEN posted_precision='time' THEN 'time' ELSE ? END")
        for j in jobs:
            vals = _values(j)
            if j["id"] in existing:
                if j.get("description") is None:
                    c.execute(f"UPDATE jobs SET {light_sets} WHERE id=?",
                              (*[v for f, v in zip(FIELDS, vals) if f in light], j["id"]))
                    continue  # location and places already set from the job's page (or its first sighting)
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
        keys = {r[0] for chunk_start in range(0, len(ids), 500)
                for r in c.execute(f"SELECT ckey FROM jobs WHERE id IN ({','.join('?' * len(ids[chunk_start:chunk_start + 500]))})",
                                   ids[chunk_start:chunk_start + 500]) if r[0]}
        _refresh_dups(c, keys)
        c.commit()
        dup_ids = _dup_ids(c, [j["id"] for j in new])
        new = [j for j in new if j["id"] not in dup_ids]
    return new


def add_event(payload: dict) -> None:
    with _lock:
        c = conn()
        c.execute("INSERT INTO events (created, payload) VALUES (?, ?)", (time.time(), json.dumps(payload)))
        c.execute("DELETE FROM events WHERE created < ?", (time.time() - 3600,))  # keep the last hour
        c.commit()


def latest_event_id() -> int:
    row = reader().execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()
    return int(row[0])


def events_after(event_id: int) -> list[tuple[int, dict]]:
    rows = reader().execute("SELECT id, payload FROM events WHERE id > ? ORDER BY id", (event_id,)).fetchall()
    return [(r[0], json.loads(r[1])) for r in rows]


def confirm_fresh(job_ids: list[str]) -> None:
    """Jobs we saw appear during a minute-by-minute check: their posting time is
    when they appeared (within about a minute), not just "today"."""
    if not job_ids:
        return
    with _lock:
        c = conn()
        c.executemany("UPDATE jobs SET posted_at=first_seen_at, posted_precision='time' WHERE id=?", [(i,) for i in job_ids])
        c.commit()


def jobs_missing_details(limit: int) -> list[tuple[str, str, str]]:
    """Newest visible jobs whose source only gives details on the job's own page."""
    return [tuple(r) for r in reader().execute(
        "SELECT id, source, url FROM jobs WHERE description IS NULL AND is_dup=0 "
        "AND source IN ('workday', 'smartrecruiters') ORDER BY posted_at DESC LIMIT ?", (limit,)).fetchall()]


def apply_details(job_id: str, description: str, location: str | None = None, time_type: str | None = None) -> None:
    """Store details read from a job's own page and refresh everything that depends on them."""
    from .normalize import job_type as _job_type, level as _level, no_degree_required, visa_sponsorship, work_setting

    with _lock:
        c = conn()
        row = c.execute("SELECT title, job_type, remote, location FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return
        title, old_type, remote, old_location = row
        loc = location or old_location
        jt = _job_type(time_type or "", old_type, title=title, description=description)
        c.execute("UPDATE jobs SET description=?, location=?, job_type=?, level=?, workplace=?, visa=?, no_degree=? WHERE id=?",
                  (description or "", loc, jt, _level(title, job_type=jt), work_setting(bool(remote), loc, title),
                   visa_sponsorship(description), int(no_degree_required(description)), job_id))
        _set_places(c, job_id, loc, bool(remote))
        ck = c.execute("SELECT ckey FROM jobs WHERE id=?", (job_id,)).fetchone()[0]
        _refresh_dups(c, {ck})
        c.commit()


def share_logos() -> int:
    """Give logo-less jobs a logo: from another job of the same company, or from a
    website found by name lookup. Returns how many jobs got one."""
    with _lock:
        c = conn()
        a = c.execute("""
            UPDATE jobs SET logo = (SELECT j2.logo FROM jobs j2 WHERE lower(j2.company) = lower(jobs.company)
                                    AND j2.logo IS NOT NULL AND j2.logo != '' LIMIT 1)
            WHERE (logo IS NULL OR logo = '') AND lower(company) IN
                  (SELECT lower(company) FROM jobs WHERE logo IS NOT NULL AND logo != '')""").rowcount
        b = c.execute("""
            UPDATE jobs SET logo = (SELECT '/api/logo/' || d.domain FROM company_domains d WHERE d.company = lower(jobs.company))
            WHERE (logo IS NULL OR logo = '') AND lower(company) IN (SELECT company FROM company_domains WHERE domain != '')""").rowcount
        c.commit()
    return (a or 0) + (b or 0)


def companies_needing_lookup(limit: int) -> list[str]:
    """Companies with visible jobs but no logo, not looked up yet (biggest first)."""
    return [r[0] for r in reader().execute("""
        SELECT company FROM jobs WHERE is_dup = 0 AND (logo IS NULL OR logo = '')
          AND lower(company) NOT IN (SELECT company FROM company_domains)
        GROUP BY lower(company) ORDER BY COUNT(*) DESC LIMIT ?""", (limit,)).fetchall()]


def remember_domain(company: str, domain: str) -> None:
    with _lock:
        conn().execute("INSERT OR REPLACE INTO company_domains (company, domain, checked) VALUES (?, ?, ?)",
                       (company.strip().lower(), domain, time.time()))
        conn().commit()


def logo_domains() -> list[str]:
    """Every website we show a logo for (to download them ahead of time)."""
    return [r[0][len("/api/logo/"):] for r in reader().execute(
        "SELECT DISTINCT logo FROM jobs WHERE logo LIKE '/api/logo/%' AND is_dup = 0").fetchall()]


def get_meta_json(key: str, default=None):
    with _lock:
        row = conn().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    try:
        return json.loads(row[0]) if row else default
    except (TypeError, ValueError):
        return default


def set_meta_json(key: str, value) -> None:
    with _lock:
        conn().execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, json.dumps(value)))
        conn().commit()


AGGREGATORS = ('himalayas', 'jobicy', 'remotive', 'remoteok', 'arbeitnow')


def _refresh_dups(c: sqlite3.Connection, ckeys: set) -> None:
    """Show one listing per job. Within a company + title:
      * a listing straight from the company's careers page beats a job-board copy;
      * the same title in the same place, listed more than once, keeps only the newest."""
    ckeys = [k for k in ckeys if k]
    for i in range(0, len(ckeys), 400):
        chunk = ckeys[i : i + 400]
        rows = c.execute(f"SELECT id, ckey, dkey, source, posted_at FROM jobs WHERE ckey IN ({','.join('?' * len(chunk))})",
                         chunk).fetchall()
        groups: dict[str, list] = {}
        for r in rows:
            groups.setdefault(r[1], []).append(r)
        updates = []
        for members in groups.values():
            has_direct = any(m[3] not in AGGREGATORS for m in members)
            best: dict[str, tuple] = {}
            for m in members:
                if has_direct and m[3] in AGGREGATORS:
                    continue
                k = m[2]
                if k not in best or m[4] > best[k][4]:
                    best[k] = m
            keep = {m[0] for m in best.values()}
            updates += [(0 if m[0] in keep else 1, m[0]) for m in members]
        c.executemany("UPDATE jobs SET is_dup=? WHERE id=?", updates)


def _dup_ids(c: sqlite3.Connection, ids: list[str]) -> set:
    out = set()
    for i in range(0, len(ids), 500):
        chunk = ids[i : i + 500]
        out |= {r[0] for r in c.execute(f"SELECT id FROM jobs WHERE is_dup=1 AND id IN ({','.join('?' * len(chunk))})", chunk)}
    return out


def set_description(job_id: str, description: str, job_type: str | None = None) -> None:
    with _lock:
        conn().execute("UPDATE jobs SET description=?, job_type=COALESCE(?, job_type) WHERE id=?",
                       (description, job_type, job_id))
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
    where, args = ["is_dup = 0"], []
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
    setting = {"remote": "Remote", "hybrid": "Hybrid", "onsite": "On-site"}.get(filters.get("remote") or "any")
    if setting:
        where.append("workplace = ?")
        args.append(setting)
    if filters.get("visa"):
        where.append("visa = 'Yes'")
    if filters.get("no_degree"):
        where.append("no_degree = 1")
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
    # Counting jobs in a place is quickest starting from that place's own list.
    count_clause = clause.replace("is_dup = 0", "+is_dup = 0", 1) if filters.get("place") else clause
    if sort == "relevance" and (filters.get("q") or "").strip():
        # bm25 with title weighted highest, then company/department, then body text.
        terms = [re.sub(r'["*^():]', " ", t).strip() for t in filters["q"].split()]
        match = " AND ".join(f'"{t}"' for t in terms if t)
        if True:
            c = reader()
            total = c.execute(f"SELECT COUNT(*) FROM jobs {count_clause}", args).fetchone()[0]
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
    if True:
        c = reader()
        total = c.execute(f"SELECT COUNT(*) FROM jobs {count_clause}", args).fetchone()[0]
        rows = c.execute(
            f"SELECT {LIST_COLUMNS} FROM jobs {clause} ORDER BY {order} LIMIT ? OFFSET ?",
            [*args, limit, offset],
        ).fetchall()
    return [_row(r) for r in rows], total


def get(job_id: str) -> dict[str, Any] | None:
    r = reader().execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return _row(r) if r else None


def facets(filters: dict[str, Any]) -> dict[str, list[list[Any]]]:
    """Value counts for each filter, computed with all *other* filters applied.
    Cached briefly: many visitors ask for the same counts."""
    key = json.dumps(filters, sort_keys=True, default=str)
    hit = _facet_cache.get(key)
    if hit and time.time() - hit[0] < FACET_TTL:
        return hit[1]
    result = _facets(filters)
    if len(_facet_cache) > 500:
        _facet_cache.clear()
    _facet_cache[key] = (time.time(), result)
    return result


_pool = None


def _facets(filters: dict[str, Any]) -> dict[str, list[list[Any]]]:
    """All filter counts at once, each query on its own thread (SQLite reads run in parallel)."""
    global _pool
    if _pool is None:
        from concurrent.futures import ThreadPoolExecutor
        _pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="counts")
    jobs = {"place": _pool.submit(_place_counts, filters)}
    for key in ("category", "job_type", "level", "company"):
        jobs[key] = _pool.submit(_value_counts, filters, key)
    return {k: f.result() for k, f in jobs.items()}


_place_cache: dict[str, tuple[float, list]] = {}


def _place_counts(filters: dict[str, Any]) -> list[list[Any]]:
    # Place counts ignore the chosen place, so picking a city reuses the counts
    # already worked out for the same other filters.
    others = {k: v for k, v in filters.items() if k != "place"}
    key = json.dumps(others, sort_keys=True)
    hit = _place_cache.get(key)
    if hit and time.time() - hit[0] < FACET_TTL:
        return hit[1]
    clause, args = _build_where(others)
    rows = reader().execute(
        f"SELECT p.place, p.kind, COUNT(*) n FROM job_places p JOIN jobs ON jobs.id = p.job_id {clause} "
        f"GROUP BY p.place, p.kind ORDER BY n DESC LIMIT 4000", args).fetchall()
    result = [[r[0], r[2], r[1]] for r in rows]
    if len(_place_cache) > 200:
        _place_cache.clear()
    _place_cache[key] = (time.time(), result)
    return result


def _value_counts(filters: dict[str, Any], key: str) -> list[list[Any]]:
    others = {k: v for k, v in filters.items() if k != key}
    clause, args = _build_where(others)
    if others.get("place"):
        clause = clause.replace("is_dup = 0", "+is_dup = 0", 1)  # start from the place's own list
    rows = reader().execute(
        f"SELECT {key}, COUNT(*) n FROM jobs {clause} AND {key} IS NOT NULL AND {key} != '' "
        f"GROUP BY {key} ORDER BY n DESC LIMIT ?", [*args, 600 if key == "company" else 100]).fetchall()
    return [[r[0], r[1]] for r in rows]


def stats() -> dict[str, int]:
    hour = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    day = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    if True:
        c = reader()
        return {
            "total": c.execute("SELECT COUNT(*) FROM jobs WHERE is_dup=0").fetchone()[0],
            "companies": c.execute("SELECT COUNT(DISTINCT company) FROM jobs WHERE is_dup=0").fetchone()[0],
            "last_hour": c.execute("SELECT COUNT(*) FROM jobs WHERE is_dup=0 AND posted_at >= ?", (hour,)).fetchone()[0],
            "last_24h": c.execute("SELECT COUNT(*) FROM jobs WHERE is_dup=0 AND posted_at >= ?", (day,)).fetchone()[0],
        }


def known_ids(ids: list[str]) -> set[str]:
    """Which of these jobs are already stored."""
    if not ids:
        return set()
    q = f"SELECT id FROM jobs WHERE id IN ({','.join('?' * len(ids))})"
    return {r[0] for r in reader().execute(q, ids)}


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
