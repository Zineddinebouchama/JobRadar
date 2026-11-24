"""Free cover letter builder that runs entirely on this server.

It reads the candidate's resume and the job posting, finds the skills they
share, turns the resume's strongest bullet points into first-person
sentences, and fills a well-structured letter the user can edit. The PDF is
rendered locally with fpdf2.
"""
from __future__ import annotations

import io
import re
from collections import Counter
from datetime import date
from typing import Any

from . import locations

# --------------------------------------------------------------------------- #
# Keyword matching between a resume and a job posting
# --------------------------------------------------------------------------- #
SKILLS = """
python java javascript typescript go golang rust c++ c# ruby php swift kotlin scala sql nosql r matlab
react angular vue next.js node.js django flask fastapi spring rails .net graphql rest api apis microservices
aws azure gcp docker kubernetes terraform ci/cd jenkins git linux bash devops sre observability
postgres postgresql mysql mongodb redis kafka spark hadoop airflow snowflake databricks dbt bigquery etl
machine learning deep learning pytorch tensorflow nlp llm llms computer vision statistics a/b testing
pandas numpy scikit-learn tableau looker power bi excel analytics data analysis data visualization
figma sketch adobe photoshop illustrator ux ui prototyping wireframing user research design systems
product management roadmap agile scrum kanban jira confluence stakeholder management okrs
seo sem google analytics hubspot salesforce crm marketing automation content strategy copywriting
social media email marketing paid media branding public relations
b2b b2c saas quota pipeline prospecting cold calling negotiation account management lead generation
customer success customer service zendesk onboarding retention
accounting gaap ifrs financial modeling forecasting budgeting fp&a audit tax payroll quickbooks
recruiting sourcing talent acquisition hris workday benefits employee relations
project management pmp operations logistics supply chain procurement inventory six sigma lean
compliance risk management legal contracts regulatory
nursing patient care emr epic hipaa clinical cpr bls
teaching curriculum tutoring lesson planning classroom management
leadership mentoring communication collaboration problem solving cross-functional presentation
bilingual spanish french german mandarin
security soc penetration testing siem iam cloud security incident response
sales marketing merchandising pos cashiering inventory scheduling bookkeeping welding forklift osha
photography videography canva tiktok instagram linkedin writing editing research tutoring coaching
microsoft office word powerpoint outlook google workspace data entry phlebotomy hospitality
""".split("\n")
SKILL_TERMS = {t for line in SKILLS for t in line.split()}
# Multi-word skills are matched as phrases.
PHRASES = [
    "machine learning", "deep learning", "computer vision", "a/b testing", "scikit-learn", "power bi",
    "data analysis", "data visualization", "user research", "design systems", "product management",
    "stakeholder management", "content strategy", "social media", "email marketing", "paid media",
    "public relations", "cold calling", "account management", "lead generation", "customer success",
    "customer service", "financial modeling", "talent acquisition", "employee relations",
    "project management", "supply chain", "six sigma", "risk management", "patient care",
    "lesson planning", "classroom management", "problem solving", "cross-functional",
    "penetration testing", "cloud security", "incident response", "google analytics",
    "marketing automation", "rest api", "ci/cd", "community outreach", "community management",
    "event planning", "cash handling", "point of sale", "food safety", "food handling", "first aid",
    "customer experience", "microsoft office", "google workspace", "data entry", "time management",
    "attention to detail", "video editing", "graphic design", "content creation", "social media marketing",
]
SKILL_VOCAB = SKILL_TERMS | set(PHRASES)

STOP = set("""
a about above after again against all also am an and any are as at be because been before being below between both
but by can could did do does doing down during each etc few for from further had has have having he her here hers
him his how i if in into is it its itself just me more most my no nor not now of off on once only or other our ours
out over own same she should so some such than that the their them then there these they this those through to too
under until up very was we were what when where which while who whom why will with would you your yours
role team work working job position company candidate candidates experience years year strong ability skills skill
including include includes required requirements preferred qualifications responsibilities responsible new well
etc using use help across within must plus based looking join us our we're you'll you're who'll per may like make
opportunity opportunities benefits salary equal employer status applicants apply application other related range
pay time full part day days week weeks paid location office remote hybrid support build building ensure provide
provides identify relevant maintain motivated proactive mission personalized looking ideal great best want world
people help helping across every through about want them their they this that will able strong excellent
environment culture values impact growth career careers employees employee hiring hire interns internship
intern summer program position positions qualified preferred please must also more most such well within
""".split())

WORD = re.compile(r"[a-zA-Z][a-zA-Z0-9+#./-]{1,30}")


def _norm(s: str) -> str:
    return " " + re.sub(r"\s+", " ", s.lower()) + " "


def extract_keywords(job_text: str, limit: int = 25, company: str = "") -> list[str]:
    text = _norm(job_text)
    skip = {w.lower() for w in re.findall(r"\w+", company or "")}
    found: list[str] = []
    for p in PHRASES:
        if f" {p} " in text or f" {p}," in text or f" {p}." in text:
            found.append(p)
    words = [w.lower().strip(".,/-") for w in WORD.findall(job_text)]
    phrase_words = {w for p in PHRASES for w in p.split()}
    skill_set = SKILL_TERMS - phrase_words
    for w in words:
        if w in skill_set and w not in found:
            found.append(w)
    # Plus the most repeated meaningful words in the posting.
    counts = Counter(w for w in words if w not in STOP and w not in skip and len(w) > 3 and not w.isdigit())
    for w, n in counts.most_common(40):
        if n < 3 or len(found) >= limit:
            break
        if w not in found and not any(w in f for f in found):
            found.append(w)
    return found[:limit]


def match_score(resume: str, keywords: list[str]) -> dict[str, Any]:
    text = _norm(resume)
    matched = [k for k in keywords if re.search(r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])", text)]
    missing = [k for k in keywords if k not in matched]
    score = round(100 * len(matched) / len(keywords)) if keywords else 0
    return {"score": score, "matched": matched, "missing": missing}


# --------------------------------------------------------------------------- #
# Resume file parsing
# --------------------------------------------------------------------------- #
def extract_resume_text(filename: str, data: bytes) -> str:
    name = filename.lower()
    if name.endswith(".pdf"):
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        return "\n".join((p.extract_text() or "") for p in reader.pages).strip()
    if name.endswith(".docx"):
        import docx

        d = docx.Document(io.BytesIO(data))
        return "\n".join(p.text for p in d.paragraphs).strip()
    return data.decode("utf-8", errors="replace").strip()


# --------------------------------------------------------------------------- #
# Reading the resume
# --------------------------------------------------------------------------- #
EMAIL_RX = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE_RX = re.compile(r"(?:\+?\d{1,3}[\s.-]?)?\(?\d{2,4}\)?[\s.-]?\d{3,4}[\s.-]?\d{3,4}")
BULLET_RX = re.compile(r"^\s*(?:[-•*▪●◦‣]|\d+[.)])\s+(.+)$")

# Past-tense verbs that start most resume bullets but aren't regular "-ed" words.
IRREGULAR_VERBS = set("""
built led ran wrote grew made drove won taught sold spoke set put cut bought brought began became
held kept met paid read sent spent stood took told thought understood oversaw undertook rebuilt
redesigned rewrote shipped got gave found fed flew forecast leveraged
""".split())


def contact_from_resume(resume: str) -> dict[str, str]:
    lines = [l.strip() for l in resume.splitlines() if l.strip()]
    name = ""
    for line in lines[:4]:
        words = line.split()
        looks_like_name = (
            1 < len(words) <= 4
            and not re.search(r"[@\d|/:.,!?]", line)
            and all(w[:1].isupper() for w in words)
            and not any(w.lower() in ("resume", "curriculum", "vitae", "cv", "profile", "summary") for w in words)
        )
        if looks_like_name:
            name = line.title() if line.isupper() else line
            break
    email = EMAIL_RX.search(resume)
    head = "\n".join(lines[:6])
    phone = PHONE_RX.search(head)
    return {"name": name, "email": email.group(0) if email else "", "phone": phone.group(0).strip() if phone else ""}


def _first_person(bullet: str) -> str | None:
    """'Grew followers from 1,200 to 4,800' -> 'I grew followers from 1,200 to 4,800'."""
    text = bullet.strip().rstrip(".;,")
    if not text:
        return None
    first = text.split()[0].lower().strip(",")
    if first.endswith("ed") or first in IRREGULAR_VERBS:
        return "I " + text[0].lower() + text[1:]
    return None


def _original_case(term: str, resume: str) -> str:
    """Use the resume's own spelling of a skill ("SQL", "Google Analytics"), but
    prefer lowercase when the resume ever writes it that way, so a job title
    like "Social Media Assistant" doesn't capitalize "social media" mid-sentence."""
    found = re.findall(r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])", resume, re.I)
    if not found:
        return term
    lower = [f for f in found if f == f.lower()]
    return lower[0] if lower else found[0]


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


_LEGAL = r"(inc|inc\.|incorporated|llc|l\.l\.c\.|ltd|ltd\.|limited|corp|corp\.|corporation|co\.|gmbh|ag|se|plc|s\.a\.|sa|b\.v\.|bv|n\.v\.|e\.v\.|pty|pty ltd|pvt ltd|private limited|s\.r\.l\.|srl|kg|gmbh & co\. kg)"


def _clean_company(name: str) -> str:
    """"Yelp, Inc" -> "Yelp", "Bosch GmbH" -> "Bosch", "JOBLINGE e.V." -> "JOBLINGE"."""
    n = re.sub(r"\s+", " ", name).strip()
    for _ in range(2):
        n = re.sub(r"[,\s]+" + _LEGAL + r"\s*$", "", n, flags=re.I).strip(" ,")
    return n or name.strip()


def _clean_title(title: str, location: str) -> str:
    """Drop location or work-setting tails that job boards add to titles:
    "Housekeeping Associates, Plano" -> "Housekeeping Associates",
    "Data Analyst (Remote - US)" -> "Data Analyst"."""
    t = re.sub(r"\s+", " ", title).strip()
    t = re.sub(r"\s*[(\[][^)\]]*\b(remote|hybrid|on-?site|onsite|f/m/x|m/w/d|w/m/d|m/f/d|h/f)\b[^)\]]*[)\]]\s*$", "", t, flags=re.I)
    t = re.sub(r"\s*[-–|,]\s*(remote|hybrid|on-?site)\b.*$", "", t, flags=re.I)
    places = [p.strip().lower() for p in re.split(r"[,;/]", location) if len(p.strip()) > 2]
    m = re.match(r"^(.*?)\s*\(([^)]+)\)$", t)
    if m and m.group(2).strip().lower() in places:
        t = m.group(1)
    m = re.match(r"^(.*?)\s*[-–|,]\s*([^,\-–|]+)$", t)
    if m:
        tail = m.group(2).strip()
        place_like = (tail.lower() in places or re.search(r"\b(cities|city|area|region|metro|county|district)$", tail, re.I)
                      or (len(tail.split()) <= 3 and locations.parse(tail)))
        if place_like:
            t = m.group(1)
    return t.strip(" -–,|") or title.strip()


PLACEHOLDER ="[Add one sentence about why {company} interests you, such as its mission, products or team.]"


def build_cover_letter(resume: str, job: dict[str, Any]) -> dict[str, Any]:
    title = _clean_title(job.get("title") or "the open", job.get("location") or "")
    company = _clean_company(job.get("company") or "your company")
    description = job.get("description") or ""
    contact = contact_from_resume(resume)

    keywords = extract_keywords(f"{title}\n{description}", company=company)
    match = match_score(resume, keywords)
    real = [k for k in match["matched"] if k in SKILL_VOCAB]
    skills = [_original_case(k, resume) for k in real[:4]]
    matched_set = [k.lower() for k in match["matched"]]

    # Pick the two bullet points that best match this job (and prefer ones with numbers).
    candidates = []
    for line in resume.splitlines():
        m = BULLET_RX.match(line)
        if not m:
            continue
        sentence = _first_person(m.group(1))
        if not sentence:
            continue
        low = sentence.lower()
        score = sum(1 for k in matched_set if k in low) * 2 + (1 if re.search(r"\d", sentence) else 0)
        candidates.append((score, sentence))
    candidates.sort(key=lambda c: -c[0])
    highlights = [s for _, s in candidates[:2]]

    role = title if re.search(r"\b(position|role)\b", title, re.I) else f"{title} role"
    opening = f"I'm excited to apply for the {role} at {company}."
    if len(skills) >= 2:
        opening += (f" My background in {_join(skills[:3])} lines up closely with what you're looking for, "
                    "and I'm confident I can contribute to your team from day one.")
    elif len(skills) == 1:
        opening += (f" My experience with {skills[0]} lines up with what you're looking for, "
                    "and I'm confident I can contribute to your team from day one.")
    else:
        opening += " I'm confident my experience and eagerness to learn would make me a strong addition to your team."

    if len(highlights) == 2:
        middle = f"In my recent experience, {highlights[0]}. {highlights[1][0].upper()}{highlights[1][1:]}."
    elif highlights:
        middle = f"In my recent experience, {highlights[0]}."
    elif skills:
        middle = (f"Through my experience so far, I've built strong skills in {_join(skills)}, "
                  "and I'm eager to put them to work on real challenges.")
    else:
        middle = ("Through my experience so far, I've learned to pick up new tools quickly, communicate clearly "
                  "and follow through on what I start.")
    if highlights and len(skills) >= 2:
        middle += f" These experiences have sharpened my skills in {_join(skills[:2])}."
    elif highlights and len(skills) == 1:
        middle += f" These experiences have sharpened my skills in {skills[0]}."

    closing = (f"I would welcome the opportunity to discuss how I can contribute to {company}. "
               "Thank you for your time and consideration.")

    header = [contact["name"] or "[Your Name]"]
    line2 = " | ".join(x for x in (contact["email"], contact["phone"]) if x)
    if line2:
        header.append(line2)
    today = date.today().strftime("%B %-d, %Y")

    letter = "\n".join([
        *header, "", today, "", f"Hiring Team, {company}", "",
        f"Dear {company} Hiring Team,", "",
        opening, "",
        middle, "",
        PLACEHOLDER.format(company=company), "",
        closing, "",
        "Sincerely,",
        contact["name"] or "[Your Name]",
    ])
    return {"letter": letter, "contact": contact, "matched": skills, "missing": match["missing"][:6]}


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #
_PDF_REPLACE = {"\u2019": "'", "\u2018": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-", "\u2014": "-",
                "\u2022": "-", "\u2026": "...", "\u00a0": " "}


def _latin1(text: str) -> str:
    for a, b in _PDF_REPLACE.items():
        text = text.replace(a, b)
    return text.encode("latin-1", "replace").decode("latin-1")


def letter_to_pdf(text: str) -> bytes:
    """Render the letter as a clean one-page PDF (letter size, 1-inch margins)."""
    from fpdf import FPDF

    pdf = FPDF(format="letter", unit="pt")
    pdf.set_margins(72, 72, 72)
    pdf.set_auto_page_break(True, margin=72)
    pdf.add_page()
    lines = _latin1(text).splitlines()
    # The first line (the name) is set a little larger.
    if lines:
        pdf.set_font("Helvetica", "B", 15)
        pdf.multi_cell(0, 20, lines[0], new_x="LMARGIN", new_y="NEXT")
        lines = lines[1:]
    pdf.set_font("Helvetica", "", 11)
    for line in lines:
        if line.strip():
            pdf.multi_cell(0, 15, line, align="L", new_x="LMARGIN", new_y="NEXT")
        else:
            pdf.ln(9)
    return bytes(pdf.output())
