"""Helpers that turn each source's raw fields into one consistent shape."""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from typing import Any

from .categorize import categorize
from .logos import logo_url

JOB_TYPES = [
    "Full-time", "Part-time", "Internship", "Apprenticeship",
    "Seasonal", "Temporary", "Contract",
]
LEVELS = ["Internship", "Entry", "Mid", "Senior", "Manager", "Executive"]

# First match wins, so the most specific kinds come first. A "Summer Intern"
# is an internship, a "Seasonal Part-time Associate" is seasonal.
_TYPE_RULES = [
    ("Apprenticeship", r"apprentic|ausbildung|azubi|alternance|aprendiz"),
    ("Internship", r"\bintern\b|internship|co-?op\b|werkstudent|working student|praktik|stagiaire|\bstage (de|d'|en)\b|trainee|placement year|summer analyst|summer associate"),
    ("Seasonal", r"seasonal|holiday (help|season)|summer (job|camp|staff|season)|winter season|christmas"),
    ("Part-time", r"part[- ]?time|teilzeit|minijob|temps partiel|\bpt\b"),
    ("Temporary", r"\btemp\b|temporary|fixed[- ]term|befristet|interim|\blocum\b"),
    ("Contract", r"contract|contractor|freelanc|consultant \(contract\)|1099"),
    ("Full-time", r"full[- ]?time|permanent|vollzeit|\bft\b|fulltime|regular"),
]
_TYPE_RX = [(t, re.compile(p, re.I)) for t, p in _TYPE_RULES]

_LEVEL_RULES = [
    ("Internship", r"\bintern\b|internship|apprentic|werkstudent|working student|trainee|co-?op\b"),
    ("Executive", r"\bchief\b|\bc[etofm]o\b|\bvp\b|vice president|\bsvp\b|\bevp\b|president|general manager|managing director"),
    ("Manager", r"\bhead of\b|director|manager|\blead\b|team lead|supervisor"),
    ("Senior", r"senior|\bsr\.?\b|staff|principal|expert|\blevel (iii|iv|3|4)\b|\biii\b|\biv\b"),
    ("Entry", r"junior|\bjr\.?\b|entry|graduate|new grad|associate|assistant|\blevel (i|1)\b|crew member|team member|cashier|barista"),
]
_LEVEL_RX = [(t, re.compile(p, re.I)) for t, p in _LEVEL_RULES]

_BLOCK_TAGS = re.compile(r"</?(p|div|br|li|ul|ol|h[1-6]|tr|section|article)[^>]*>", re.I)
_LI_OPEN = re.compile(r"<li[^>]*>", re.I)
_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t ]+")
_NL = re.compile(r"\n\s*\n\s*\n+")


def html_to_text(raw: str | None, limit: int = 20000) -> str:
    """Strip HTML into readable plain text (keeps paragraph breaks and bullets)."""
    if not raw:
        return ""
    s = html.unescape(raw) if "&lt;" in raw else raw  # Greenhouse double-escapes
    s = _LI_OPEN.sub("\n• ", s)
    s = _BLOCK_TAGS.sub("\n", s)
    s = _TAGS.sub("", s)
    s = html.unescape(s)
    s = _WS.sub(" ", s)
    s = "\n".join(line.strip() for line in s.splitlines())
    s = _NL.sub("\n\n", s).strip()
    return s[:limit]


def job_type(*hints: Any, title: str = "") -> str:
    """Pick the most specific job type from the title and any source type fields."""
    text = " ".join([title] + [_flat(h) for h in hints])
    for t, rx in _TYPE_RX:
        if rx.search(text):
            return t
    return "Full-time"


def level(title: str, *hints: Any) -> str:
    for t, rx in _LEVEL_RX:
        if rx.search(title or ""):
            return t
    text = " ".join(_flat(h) for h in hints)
    for t, rx in _LEVEL_RX:
        if rx.search(text):
            return t
    return "Mid"


def to_iso(value: Any) -> str:
    """Accept epoch seconds/ms or ISO strings; return ISO-8601 UTC."""
    if value in (None, ""):
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
            ts = float(value)
            if ts > 1e12:
                ts /= 1000
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        else:
            s = str(value).replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()
    except (ValueError, OSError):
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def is_remote(*hints: Any) -> bool:
    return bool(re.search(r"\bremote\b|anywhere|work from home|\bwfh\b|distributed", " ".join(_flat(h) for h in hints), re.I))


def salary_range(lo: Any, hi: Any, currency: str | None = "USD", period: str | None = None) -> str | None:
    try:
        lo_f, hi_f = float(lo or 0), float(hi or 0)
    except (TypeError, ValueError):
        return None
    if not lo_f and not hi_f:
        return None
    sym = {"USD": "$", "EUR": "€", "GBP": "£", "CAD": "CA$", "AUD": "A$"}.get((currency or "USD").upper(), (currency or "") + " ")

    def fmt(v: float) -> str:
        return f"{sym}{v / 1000:.0f}k" if v >= 1000 else f"{sym}{v:,.0f}"

    out = fmt(lo_f) if lo_f and not hi_f else fmt(hi_f) if hi_f and not lo_f else f"{fmt(lo_f)}–{fmt(hi_f)}"
    if period and period.lower() in ("hour", "hourly"):
        out += "/hr"
    return out


def make_job(
    *,
    source: str,
    native_id: Any,
    title: str,
    company: str,
    url: str,
    posted_at: Any,
    location: str | None = None,
    remote: bool | None = None,
    department: str | None = None,
    type_hints: list[Any] | None = None,
    level_hints: list[Any] | None = None,
    category_hints: list[Any] | None = None,
    salary: str | None = None,
    tags: list[str] | None = None,
    logo: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    title = (title or "").strip()
    dept = (department or "").strip() or None
    tags = [str(t).strip() for t in (tags or []) if str(t).strip()][:12]
    location = (location or "").strip() or None
    if not salary and description:
        salary = extract_salary(description)
    if remote is None:
        remote = is_remote(location, title)
    job_id = f"{source}:{native_id}"
    return {
        "id": job_id,
        "source": source,
        "title": title,
        "company": (company or "Unknown").strip(),
        "department": dept,
        "location": location or ("Remote" if remote else None),
        "remote": bool(remote),
        "category": categorize(title, [_flat(h) for h in (category_hints or [])] + [dept or ""] + tags),
        "job_type": job_type(*(type_hints or []), title=title),
        "level": level(title, *(level_hints or [])),
        "salary": salary or None,
        "tags": tags,
        "url": url,
        "logo": logo_url(job_id, logo or None),
        "posted_at": to_iso(posted_at),
        "description": description,
    }


def _flat(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, tuple, set)):
        return " ".join(_flat(x) for x in v)
    if isinstance(v, dict):
        return " ".join(_flat(x) for x in v.values())
    return str(v)


# --------------------------------------------------------------------------- #
# Key facts pulled out of the description text
# --------------------------------------------------------------------------- #
_MONEY = r"(?:US\$|CA\$|A\$|\$|€|£)\s?\d{1,3}(?:[,.]\d{3})*(?:\.\d+)?\s?[kK]?"
_SALARY_RX = re.compile(
    rf"({_MONEY})(?:\s*(?:-|–|—|to)\s*({_MONEY}))?(?:\s*(?:USD|CAD|EUR|GBP))?"
    r"(?:\s*(?:/|per|an|a)\s*(hour|hr|year|yr|annum|annually|month|week))?",
    re.I,
)


def _money_value(s: str) -> float:
    num = re.sub(r"[^\d.,kK]", "", s)
    mult = 1000 if num[-1:] in "kK" else 1
    num = num.rstrip("kK").replace(",", "")
    if num.count(".") == 1 and len(num.split(".")[1]) == 3:  # "120.000" European style
        num = num.replace(".", "")
    try:
        return float(num) * mult
    except ValueError:
        return 0


def extract_salary(text: str) -> str | None:
    """Find a pay range like "$120,000 - $150,000" or "$22/hour" in posting text."""
    best = None
    for m in _SALARY_RX.finditer(text or ""):
        lo, hi, per = m.group(1), m.group(2), (m.group(3) or "").lower()
        v = _money_value(lo)
        hourly = per in ("hour", "hr") or (v and v < 200)
        if not v or (not hourly and v < 15000) or v > 2_000_000:
            continue
        # Ignore funding rounds, revenue etc.
        ctx = text[max(0, m.start() - 60) : m.end() + 30].lower()
        if re.search(r"raised|funding|valuation|revenue|series [a-f]|billion|million|bn\b|\bm\b", ctx):
            continue
        out = re.sub(r"\s+", " ", m.group(0).strip())
        if hourly and not per:
            out += "/hr"
        if hi or best is None:
            best = out
            if hi:
                break
    return best


_FACT_RULES = [
    ("Experience", r"(\d{1,2}\+?\s*(?:-|–|to)?\s*\d{0,2}\+?\s*years?(?:\s+of)?(?:\s+\w+){0,4}\s+experience)"),
    ("Education", r"(\b(?:bachelor'?s|master'?s|ph\.?d|mba|high school diploma|\bged\b|bs/ba|b\.s\.|m\.s\.)\b[^.\n]{0,80})"),
    ("Visa sponsorship", r"([^.\n]{0,60}(?:visa sponsorship|sponsor (?:a |an )?(?:visa|work authorization)|h-?1b)[^.\n]{0,60})"),
    ("Schedule", r"([^.\n]{0,40}(?:weekends?|night shifts?|overnight|flexible hours|\d+\s*hours? per week|shift)[^.\n]{0,40})"),
    ("Start date", r"((?:start date|starting|begins?)\s*(?:in|on|:)?\s*(?:january|february|march|april|may|june|july|august|september|october|november|december|summer|fall|spring|winter)[^.\n]{0,30})"),
    ("Deadline", r"((?:apply by|application deadline|applications close)[^.\n]{0,40})"),
]
_FACT_RX = [(k, re.compile(p, re.I)) for k, p in _FACT_RULES]
_BENEFITS = [
    ("Health insurance", r"health (insurance|coverage|benefits|plan)|medical(, | and )dental|medical insurance"),
    ("Dental", r"\bdental\b"),
    ("Vision", r"(dental|medical)[, ]+(and )?vision|vision (insurance|coverage|plan)"),
    ("401(k)", r"401\(?k\)?"),
    ("Equity", r"\bequity\b|stock options|\brsus?\b"),
    ("PTO", r"\bpto\b|paid time off|vacation days|unlimited vacation"),
    ("Parental leave", r"parental leave|maternity|paternity"),
    ("Bonus", r"\bbonus\b"),
    ("Tuition / learning", r"tuition|learning (budget|stipend)|education (budget|stipend)"),
    ("Remote stipend", r"(home office|remote work|wfh) stipend"),
    ("Relocation", r"relocation (assistance|support|package)"),
    ("Employee discount", r"employee discount"),
    ("Pension", r"\bpension\b"),
    ("Meals", r"free (meals|lunch)|catered|meal (allowance|stipend)"),
]
_BENEFITS_RX = [(n, re.compile(p, re.I)) for n, p in _BENEFITS]


def key_facts(job: dict[str, Any]) -> list[dict[str, str]]:
    """Short "at a glance" facts for the job detail panel."""
    text = job.get("description") or ""
    facts: list[dict[str, str]] = []
    for label, rx in _FACT_RX:
        m = rx.search(text)
        if m:
            val = m.group(1)
            start = m.start(1)
            if start > 0 and text[start - 1].isalnum() and " " in val:
                val = val.split(" ", 1)[1]  # don't start mid-word
            end = m.end(1)
            if end < len(text) and text[end].isalnum() and " " in val:
                val = val.rsplit(" ", 1)[0] + "…"  # don't stop mid-word
            if start > 0 and text[start - 1].isalnum():
                val = "…" + val
            val = re.sub(r"\s+", " ", val).strip(" ,;:-•")
            facts.append({"label": label, "value": val[:140]})
    perks = [name for name, rx in _BENEFITS_RX if rx.search(text)]
    if perks:
        facts.append({"label": "Benefits mentioned", "value": ", ".join(perks)})
    return facts
