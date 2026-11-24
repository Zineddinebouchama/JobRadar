"""App configuration, read from environment variables (and an optional .env file)."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Tiny .env loader so the project has no extra dependency for it."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv(ROOT / ".env")


def _list(name: str, default: str) -> list[str]:
    return [s.strip() for s in os.getenv(name, default).split(",") if s.strip()]


DB_PATH = Path(os.getenv("POSTEDFRESH_DB", str(ROOT / "data" / "postedfresh.db")))
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))
MAX_JOB_AGE_DAYS = int(os.getenv("MAX_JOB_AGE_DAYS", "45"))
USER_AGENT = os.getenv("POSTEDFRESH_USER_AGENT", "PostedFresh/2.0 (job aggregator; portfolio project)")

# Company career boards to watch. These ATS APIs are public and update the
# moment a company publishes a role, which is where "early" listings come from.
GREENHOUSE_BOARDS = _list(
    "GREENHOUSE_BOARDS",
    "stripe,airbnb,discord,figma,robinhood,coinbase,dropbox,reddit,duolingo,instacart,lyft,"
    "pinterest,databricks,cloudflare,gitlab,datadog,twitch,asana,airtable,affirm,brex,chime,"
    "elastic,epicgames,flexport,gusto,klaviyo,lattice,mongodb,peloton,qualtrics,roblox,samsara,"
    "scaleai,squarespace,toast,zscaler,nuro,waymo,spacex,calendly,carta,faire,gemini,mercury,"
    "nextdoor,oscar,sofi,betterment,riotgames,mozilla,khanacademy,coursera,udemy,justworks,esri,"
    "sweetgreen,everlane,seatgeek,stitchfix,tripadvisor,greenhouse,intercom,pagerduty,newrelic,"
    "sumologic,fivetran,cockroachlabs,grafanalabs,vercel,remote",
)
LEVER_BOARDS = _list(
    "LEVER_BOARDS", "spotify,palantir,outreach,zoox,wealthfront,ro,matchgroup,coupa"
)
ASHBY_BOARDS = _list(
    "ASHBY_BOARDS",
    "openai,notion,ramp,linear,supabase,posthog,replit,perplexity,cursor,clickup,zapier,plaid,"
    "snowflake,lemonade,harvey,runway",
)
# SmartRecruiters hosts many retail, hospitality and global employers, which is
# where most part-time, seasonal and apprenticeship roles show up.
SMARTRECRUITERS_COMPANIES = _list(
    "SMARTRECRUITERS_COMPANIES",
    "BoschGroup,McDonaldsCorporation,LVMH,Equinox,Sodexo,Securitas,Experian,ServiceNow",
)
SMARTRECRUITERS_MAX_PER_COMPANY = int(os.getenv("SMARTRECRUITERS_MAX_PER_COMPANY", "1000"))
HIMALAYAS_PAGES = int(os.getenv("HIMALAYAS_PAGES", "15"))
ARBEITNOW_PAGES = int(os.getenv("ARBEITNOW_PAGES", "5"))
