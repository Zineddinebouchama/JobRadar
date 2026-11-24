"""Helpers that turn each source's raw fields into one consistent shape."""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from typing import Any

from .categorize import categorize
from .logos import logo_url

JOB_TYPES = [
    "Full-time", "Part-time", "Internship", "Apprenticeship", "Fellowship",
    "Seasonal", "Temporary", "Contract", "Freelance", "Volunteer",
]
WORK_SETTINGS = ["Remote", "Hybrid", "On-site"]
LEVELS = ["Internship", "Entry", "Mid", "Senior", "Manager", "Executive"]

# Words that make a job an internship, in the languages our sources use.
_INTERN = re.compile(
    r"\bintern\b|\binterns\b|internship|\bco-?op\b|werkstudent|working student|studentische|praktik|praktyk|"
    r"stagiaire|\bstage\b(?! \d)(?!.* (manager|engineer|lead|director))|estágio|estagi[aá]rio|pr[aá]cticas|practicante|"
    r"pasante|becario|tirocin|stagiair|gyakornok|\bstaż\b|\bstaz\b|placement year|industrial placement|"
    r"summer (analyst|associate|student)|student (assistant|worker|job|position)|thesis|abschlussarbeit|tesista|"
    r"vacation scheme|externship|\b(masters?|mba|phd|undergrad\w*|student)?\s?summer 20\d\d\b",
    re.I,
)
# Words that mean an early-career job that is NOT an internship.
_EARLY_CAREER = re.compile(
    r"new grad|new graduate|recent grad|early career|graduate (program|programme|scheme|engineer|analyst|trainee|"
    r"specialist|associate|role|position|hire)|\bgraduate\b(?!.*intern)|trainee|management trainee|junior managers? program|"
    r"rotational program|rotation program|entry[- ]level|university grad|college grad|campus hire|\bnew college\b",
    re.I,
)
_APPRENTICE = re.compile(r"apprentic|ausbildung|azubi|alternance|aprendiz|lehrling|duales studium|dual study", re.I)

# Other job types: checked in the title first, then the source's own fields.
_TYPE_RULES = [
    ("Volunteer", r"\bvolunteer|ehrenamt|voluntari|bénévol|unpaid"),
    ("Fellowship", r"fellowship|fellows program|residency program|\bresidency\b(?!.*(medical|physician|clinical))"),
    ("Freelance", r"freelanc|independent contractor|\bgig\b|1099|self[- ]employed|auftragnehmer"),
    ("Seasonal", r"seasonal|holiday (help|season|staff)|summer (job|camp|staff|season|seasonal)|winter season|christmas|saisonal|temporada"),
    ("Part-time", r"part[- ]?time|teilzeit|minijob|temps partiel|tiempo parcial|deeltijd|meio período|\bpt\b|per diem|prn\b"),
    ("Temporary", r"\btemp\b|temporary|fixed[- ]term|befristet|interim|\blocum\b|\bcdd\b|maternity cover|parental leave cover"),
    ("Contract", r"\bcontract\b|contractor|\bcontrato\b|\bcdi\b"),
]
_TYPE_RX = [(t, re.compile(p, re.I)) for t, p in _TYPE_RULES]
_SOURCE_INTERN = re.compile(r"intern|internship|praktikum|stage|estágio|trainee", re.I)
_SOURCE_APPRENTICE = re.compile(r"apprentic", re.I)
# A source's "internship" label is only trusted if the posting itself backs it up.
_DESC_INTERN = re.compile(r"\bintern(ship)?s?\b|working student|werkstudent|praktik|estágio|\bstage\b|stagiaire|"
                          r"gyakornok|staż|thesis|placement|current(ly)? (enrolled|student)|pursuing a (bachelor|master|degree)|"
                          r"\bstudents?\b", re.I)

_EXEC = re.compile(r"\bchief\b|\bc[etofm]o\b|\bcxo\b|\bvp\b|vice president|\bsvp\b|\bevp\b|\bpresident\b|managing director|"
                   r"general manager|^(managing |general |senior |equity )?partner\b(?! (solutions|programs?|manager|marketing|success|development|sales|product|ecosystem|engineer))|"
                   r"managing partner|founder", re.I)
_MANAGER = re.compile(r"\bhead of\b|\bhead,|director|engineering manager|people manager|team lead|people lead|"
                      r"supervisor|store manager|restaurant manager|shift manager|office manager|"
                      r"\bmanager\b(?!.*\b(product|project|program|programme|account|relationship|customer success|partner|"
                      r"case|portfolio|community|social media|content|campaign|category|territory)\b)(?<!product manager)"
                      r"(?<!project manager)(?<!program manager)(?<!account manager)(?<!success manager)(?<!relationship manager)"
                      r"(?<!partner manager)(?<!case manager)(?<!community manager)", re.I)
_SENIOR = re.compile(r"senior|\bsr\.?\b|\bstaff\b|\blead\b(?!er)|tech lead|principal|\bexpert\b|\bdistinguished\b|\blevel (iii|iv|v|3|4|5)\b|"
                     r"\b(iii|iv)\b|\bl[5-7]\b|\bsenior\b|leitend|s[eê]nior", re.I)
_MID = re.compile(r"mid[- ]level|\bmid\b|intermediate|\blevel (ii|2)\b|\bii\b|pleno", re.I)
_ENTRY = re.compile(r"junior|\bjr\.?\b|entry[- ]level|\bentry\b|new grad|graduate|early career|trainee|\blevel (i|1)\b|\bi\b$|"
                    r"\bassociates?\b(?! (director|manager|principal|partner|general counsel|vice))|assistants?\b|crew member|"
                    r"team member|cashier|barista|j[uú]nior", re.I)


_REMOTE_RX = re.compile(r"\bremote\b|work from home|\bwfh\b|anywhere|fully distributed|home[- ]based|telecommut|"
                        r"homeoffice|home office|télétravail|teletrabajo|trabalho remoto|zdaln", re.I)
_HYBRID_RX = re.compile(r"\bhybrid\b|hybride|híbrido|hibrid|\d days? (a|per) week in (the )?office|in-office \d", re.I)


def work_setting(remote: bool, *hints: Any) -> str:
    """Remote, Hybrid or On-site, from the source's own field, the location and the title."""
    text = " ".join(_flat(h) for h in hints)
    if _HYBRID_RX.search(text):
        return "Hybrid"
    if remote:
        return "Remote"
    return "On-site"


# Visa sponsorship, read sentence by sentence from the posting. Any sentence
# about sponsorship that contains a negative ("not", "unable", "without", ...)
# means it isn't offered; a clear "no" anywhere wins over a "yes" elsewhere.
# A sentence may run across line breaks (postings often wrap mid-sentence), up to a few hundred characters.
_SPONSOR_SENTENCE = re.compile(r"[^.!?]{0,300}(visa|h-?1b|work permit|immigration|work authori[sz]ation|authori[sz]ed to work)[^.!?]{0,300}", re.I)
# It has to be about immigration AND about sponsoring/supporting it; "executive sponsors" doesn't count.
_SPONSOR_TOPIC = re.compile(r"(visa|h-?1b|work permit|immigration)[^.!?]{0,80}(sponsor|support|assist|process|provid|offer|availab)|"
                            r"(sponsor|support|assist|provid|offer)[^.!?]{0,80}(visa|h-?1b|work permit|immigration)|"
                            r"authori[sz](ed|ation) to work[^.!?]{0,120}sponsor", re.I)
_SPONSOR_YES = re.compile(r"(visa|h-?1b|work permit|immigration)[^.!?]{0,60}sponsor|sponsor[^.!?]{0,60}(visa|h-?1b|work permit|immigration)|"
                          r"visa support|visa assistance|relocation (and|&) visa", re.I)
_NEGATION = re.compile(r"\bnot\b|\bno\b|n't\b|\bunable\b|\bcannot\b|\bwithout\b|\bineligible\b|\bnor\b|\bnever\b|"
                       r"\bunavailable\b|\bwill need to\b|\bmust (already )?(be|have) (authorized|eligible|legally)", re.I)


def visa_sponsorship(description: str | None) -> str | None:
    if not description:
        return None
    text = description[:15000]
    low = text.lower()
    if "sponsor" not in low and "visa" not in low and "h-1b" not in low and "h1b" not in low:
        return None  # fast skip for most postings
    # "U.S." and "e.g." shouldn't end a sentence ("...may not ... certain U.S. visa categories").
    text = re.sub(r"\b(U\.S\.A|U\.S|U\.K|e\.g|i\.e|etc|approx|incl|Inc|Ltd|Co|St|No)\.", lambda m: m.group(1).replace(".", ""), text)
    answer = None
    for m in _SPONSOR_SENTENCE.finditer(text):
        sentence = m.group(0)
        if not _SPONSOR_TOPIC.search(sentence):
            continue  # e.g. "Visa card" or a visa-free travel perk
        if _NEGATION.search(sentence):
            return "No"
        if _SPONSOR_YES.search(sentence):
            answer = "Yes"  # only an actual offer counts, not "visa letters" or "visa fees"
    return answer


# Only explicit statements count; "bachelor's or equivalent experience" still asks for a degree.
_NO_DEGREE = re.compile(
    r"no (college |university |4-year |four-year )?degree (is )?(required|needed|necessary)|"
    r"degree (is )?not (required|needed|necessary)|(do|does) not (need|require) a (college |university )?degree|"
    r"without a (college |university )?degree|high school diploma or (equivalent|ged)|\bged\b or (equivalent|high school)|"
    r"high school (diploma|degree) (is )?(required|preferred)|no experience (or degree )?(required|necessary|needed)|"
    r"kein (studium|abschluss) (erforderlich|notwendig)|sin (título|titulo) requerido", re.I)


def no_degree_required(description: str | None) -> bool:
    if not description:
        return False
    low = description[:15000].lower()
    if "degree" not in low and "diploma" not in low and "ged" not in low and "studium" not in low and "título" not in low:
        return False
    return bool(_NO_DEGREE.search(low))


def job_type(*hints: Any, title: str = "", description: str | None = None) -> str:
    """Pick the job type. The title always wins over the source's own fields,
    because sources sometimes label new-grad and trainee programs as internships."""
    hint_text = " ".join(_flat(h) for h in hints)
    if _APPRENTICE.search(title):
        return "Apprenticeship"
    early = _EARLY_CAREER.search(title)
    if _INTERN.search(title) and not (early and not re.search(r"\bintern|internship|co-?op", title, re.I)):
        return "Internship"
    for t, rx in _TYPE_RX:
        if rx.search(title):
            return t
    # Nothing decisive in the title: use the source's own fields.
    if _SOURCE_APPRENTICE.search(hint_text):
        return "Apprenticeship"
    stated_level = _EXEC.search(title) or _MANAGER.search(title) or _SENIOR.search(title) or _MID.search(title)
    if (_SOURCE_INTERN.search(hint_text) and not early and not stated_level
            and _DESC_INTERN.search((description or "")[:8000])):
        return "Internship"
    for t, rx in _TYPE_RX:
        if rx.search(hint_text):
            return t
    return "Full-time"


def level(title: str, *hints: Any, job_type: str = "") -> str:
    """Experience level, from the title first. Product, project, account and
    similar "manager" titles are individual roles, not people managers."""
    if job_type == "Internship":
        return "Internship"
    t = title or ""
    if _EXEC.search(t):
        return "Executive"
    if _MANAGER.search(t):
        return "Manager"
    if _SENIOR.search(t):
        return "Senior"
    if _ENTRY.search(t) or job_type == "Apprenticeship":
        return "Entry"
    if _MID.search(t):
        return "Mid"
    # Nothing in the title: use the source's level field if it maps cleanly.
    h = " ".join(_flat(x) for x in hints).lower()
    if re.search(r"executive|director|vp", h):
        return "Executive" if re.search(r"executive|vp", h) else "Manager"
    if re.search(r"manager|management", h):
        return "Manager"
    if re.search(r"senior|lead|expert|principal", h):
        return "Senior"
    if re.search(r"entry|junior|associate|graduate", h):
        return "Entry"
    return "Mid"


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


def _parse_date_only(s: str) -> str | None:
    """'November 24, 2026' or '2026-11-24' -> the start of that day (UTC), never in the future.
    Jobs with only a date are marked as day precision, so the site says "Today", not "just now"."""
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d", "%d %B %Y"):
        try:
            d = datetime.strptime(s, fmt).date()
        except ValueError:
            continue
        now = datetime.now(timezone.utc).replace(microsecond=0)
        return min(now, datetime(d.year, d.month, d.day, tzinfo=timezone.utc)).isoformat()
    return None


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
            s = str(value).strip()
            date_only = _parse_date_only(s)
            if date_only:
                return date_only
            s = s.replace("Z", "+00:00")
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


_GENDER_TAG = re.compile(r"\((m|f|w|d|h|x|div|all genders?|alle|f/m/x|m/w/d|w/m/d|m/f/d|h/f|f/h|k/m)[^)]*\)", re.I)


def title_key(title: str) -> str:
    """Normalized title for spotting duplicates: lowercase, no gender tags, punctuation or extra spaces."""
    t = _GENDER_TAG.sub(" ", title or "").lower()
    t = re.sub(r"[^\w]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


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
    workplace_hint: Any = None,
    posted_precision: str = "time",
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
        "workplace": work_setting(bool(remote), workplace_hint, location, title),
        "visa": visa_sponsorship(description),
        "no_degree": no_degree_required(description),
        "category": categorize(title, [_flat(h) for h in (category_hints or [])] + [dept or ""] + tags),
        "job_type": (jt := job_type(*(type_hints or []), title=title, description=description)),
        "level": level(title, *(level_hints or []), job_type=jt),
        "salary": salary or None,
        "tags": tags,
        "url": url,
        "logo": logo_url(job_id, logo or None),
        "posted_at": to_iso(posted_at),
        "posted_precision": posted_precision,
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
