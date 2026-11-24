"""Company logos: free, fetched once per company and cached on disk.

Each watched company board is mapped to its official website. The logo is the
site's icon from Google's free favicon service (falling back to DuckDuckGo's),
downloaded once and served from this server afterwards.
"""
from __future__ import annotations

import re
from pathlib import Path

import httpx

from . import config

# Board slug -> official website.
BOARD_DOMAINS = {
    # Greenhouse
    "stripe": "stripe.com", "airbnb": "airbnb.com", "discord": "discord.com", "figma": "figma.com",
    "robinhood": "robinhood.com", "coinbase": "coinbase.com", "dropbox": "dropbox.com", "reddit": "reddit.com",
    "duolingo": "duolingo.com", "instacart": "instacart.com", "lyft": "lyft.com", "pinterest": "pinterest.com",
    "databricks": "databricks.com", "cloudflare": "cloudflare.com", "gitlab": "gitlab.com", "datadog": "datadoghq.com",
    "twitch": "twitch.tv", "asana": "asana.com", "airtable": "airtable.com", "affirm": "affirm.com", "brex": "brex.com",
    "chime": "chime.com", "elastic": "elastic.co", "epicgames": "epicgames.com", "flexport": "flexport.com",
    "gusto": "gusto.com", "klaviyo": "klaviyo.com", "lattice": "lattice.com", "mongodb": "mongodb.com",
    "peloton": "onepeloton.com", "qualtrics": "qualtrics.com", "roblox": "roblox.com", "samsara": "samsara.com",
    "scaleai": "scale.com", "squarespace": "squarespace.com", "toast": "toasttab.com", "zscaler": "zscaler.com",
    "nuro": "nuro.ai", "waymo": "waymo.com", "spacex": "spacex.com", "calendly": "calendly.com", "carta": "carta.com",
    "faire": "faire.com", "gemini": "gemini.com", "mercury": "mercury.com", "nextdoor": "nextdoor.com",
    "oscar": "hioscar.com", "sofi": "sofi.com", "betterment": "betterment.com", "riotgames": "riotgames.com",
    "mozilla": "mozilla.org", "khanacademy": "khanacademy.org", "coursera": "coursera.org", "udemy": "udemy.com",
    "justworks": "justworks.com", "esri": "esri.com", "sweetgreen": "sweetgreen.com", "everlane": "everlane.com",
    "seatgeek": "seatgeek.com", "stitchfix": "stitchfix.com", "tripadvisor": "tripadvisor.com",
    "greenhouse": "greenhouse.com", "intercom": "intercom.com", "pagerduty": "pagerduty.com", "newrelic": "newrelic.com",
    "sumologic": "sumologic.com", "fivetran": "fivetran.com", "cockroachlabs": "cockroachlabs.com",
    "grafanalabs": "grafana.com", "vercel": "vercel.com", "remote": "generalassemb.ly",
    # Lever
    "spotify": "spotify.com", "palantir": "palantir.com", "outreach": "outreach.io", "zoox": "zoox.com",
    "wealthfront": "wealthfront.com", "ro": "ro.co", "matchgroup": "mtch.com", "coupa": "coupa.com",
    # Ashby
    "openai": "openai.com", "notion": "notion.so", "ramp": "ramp.com", "linear": "linear.app",
    "supabase": "supabase.com", "posthog": "posthog.com", "replit": "replit.com", "perplexity": "perplexity.ai",
    "cursor": "cursor.com", "clickup": "clickup.com", "zapier": "zapier.com", "plaid": "plaid.com",
    "snowflake": "snowflake.com", "lemonade": "lemonade.com", "harvey": "harvey.ai", "runway": "runwayml.com",
    # SmartRecruiters
    "boschgroup": "bosch.com", "mcdonaldscorporation": "mcdonalds.com", "lvmh": "lvmh.com", "equinox": "equinox.com",
    "sodexo": "sodexo.com", "securitas": "securitas.com", "experian": "experian.com", "servicenow": "servicenow.com",
}

COMPANY_BOARDS = ("greenhouse", "lever", "ashby", "smartrecruiters")
LOGO_DIR = config.DB_PATH.parent / "logos"
_DOMAIN_RX = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}$")


# Only legal endings are ignored; words like "Group" or "Labs" are part of the name.
_LEGAL = re.compile(r"\b(inc|incorporated|llc|ltd|limited|corp|corporation|gmbh|ag|se|plc|sa|bv|nv|"
                    r"ev|e\.v|pty|srl|kg|co\. kg)\b\.?", re.I)


def name_key(name: str) -> str:
    """'Abnormal Security, Inc.' and 'Abnormalsecurity' both become 'abnormalsecurity'."""
    n = _LEGAL.sub(" ", (name or "").lower())
    return re.sub(r"[^a-z0-9]+", "", n)


async def lookup_domain(client: httpx.AsyncClient, company: str) -> str:
    """Find a company's website from its name with a free public lookup. Only an exact
    name match counts, so a company never gets someone else's logo. '' = not found."""
    key = name_key(company)
    if len(key) < 3:
        return ""
    r = await client.get("https://autocomplete.clearbit.com/v1/companies/suggest", params={"query": company}, timeout=10)
    if r.status_code != 200:
        raise httpx.HTTPError(f"lookup returned {r.status_code}")
    for s in r.json()[:5]:
        domain = (s.get("domain") or "").lower()
        if name_key(s.get("name", "")) != key or not _DOMAIN_RX.match(domain):
            continue
        # The name must also be in the website address ("celonis" -> celonis.com),
        # which rules out look-alike companies with different sites.
        root = re.sub(r"[^a-z0-9]", "", domain.rsplit(".", 2)[0] if domain.count(".") >= 2 else domain.split(".")[0])
        if key in root or root in key:
            return domain
    return ""


def domain_for(job_id: str) -> str | None:
    """'greenhouse:stripe/123' -> 'stripe.com' (None for sources we can't map)."""
    source, _, rest = job_id.partition(":")
    if source not in COMPANY_BOARDS:
        return None
    return BOARD_DOMAINS.get(rest.split("/", 1)[0].lower())


def logo_url(job_id: str, existing: str | None) -> str | None:
    if existing:
        return existing
    domain = domain_for(job_id)
    return f"/api/logo/{domain}" if domain else None


def _path(domain: str, ext: str) -> Path:
    return LOGO_DIR / f"{domain}.{ext}"


async def get_logo(client: httpx.AsyncClient, domain: str) -> tuple[bytes, str] | None:
    """Return (image bytes, content type) for a domain, downloading it once."""
    domain = domain.lower()
    if not _DOMAIN_RX.match(domain):
        return None
    LOGO_DIR.mkdir(parents=True, exist_ok=True)
    for ext, ctype in (("png", "image/png"), ("ico", "image/x-icon")):
        p = _path(domain, ext)
        if p.exists():
            return p.read_bytes(), ctype
    if _path(domain, "none").exists():
        return None
    try:
        # Google redirects every request; unknown sites end in a 404 with a generic globe.
        r = await client.get("https://www.google.com/s2/favicons", params={"domain": domain, "sz": 128},
                             follow_redirects=True, timeout=10)
        if r.status_code == 200 and r.content[:4] == b"\x89PNG" and len(r.content) > 200:
            _path(domain, "png").write_bytes(r.content)
            return r.content, "image/png"
        r = await client.get(f"https://icons.duckduckgo.com/ip3/{domain}.ico", timeout=10)
        if r.status_code == 200 and len(r.content) > 200:
            _path(domain, "ico").write_bytes(r.content)
            return r.content, r.headers.get("content-type", "image/x-icon")
    except httpx.HTTPError:
        return None  # try again next time
    _path(domain, "none").write_bytes(b"")
    return None
