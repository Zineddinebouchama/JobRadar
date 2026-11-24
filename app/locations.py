"""Turn messy location strings into clean, structured places.

Sources write the same place many ways ("New York, NY (HQ)", "New York City
Office", "US-NY-New York", "Remote - USA", "bangalore, , India"). This module
parses each string into places at three levels (city, state/region, country)
so the location filter can match accurately: picking "United States" finds
every US city, picking "California, USA" finds every California city.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# --------------------------------------------------------------------------- #
# Reference data
# --------------------------------------------------------------------------- #
US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
    "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
    "DC": "District of Columbia", "PR": "Puerto Rico",
}
CA_PROVINCES = {
    "AB": "Alberta", "BC": "British Columbia", "MB": "Manitoba", "NB": "New Brunswick",
    "NL": "Newfoundland and Labrador", "NS": "Nova Scotia", "ON": "Ontario", "PE": "Prince Edward Island",
    "QC": "Quebec", "SK": "Saskatchewan", "NT": "Northwest Territories", "YT": "Yukon", "NU": "Nunavut",
}
AU_STATES = {
    "NSW": "New South Wales", "VIC": "Victoria", "QLD": "Queensland", "WA": "Western Australia",
    "SA": "South Australia", "TAS": "Tasmania", "ACT": "Australian Capital Territory", "NT": "Northern Territory",
}

COUNTRIES = """
Afghanistan|Albania|Algeria|Andorra|Angola|Argentina|Armenia|Australia|Austria|Azerbaijan|Bahamas|Bahrain|
Bangladesh|Barbados|Belarus|Belgium|Belize|Benin|Bolivia|Bosnia and Herzegovina|Botswana|Brazil|Brunei|Bulgaria|
Cambodia|Cameroon|Canada|Chile|China|Colombia|Costa Rica|Croatia|Cuba|Cyprus|Czechia|Denmark|Dominican Republic|
Ecuador|Egypt|El Salvador|Estonia|Ethiopia|Fiji|Finland|France|Georgia|Germany|Ghana|Greece|Guatemala|Honduras|
Hong Kong|Hungary|Iceland|India|Indonesia|Iran|Iraq|Ireland|Israel|Italy|Jamaica|Japan|Jordan|Kazakhstan|Kenya|
Kosovo|Kuwait|Kyrgyzstan|Latvia|Lebanon|Liechtenstein|Lithuania|Luxembourg|Macau|Madagascar|Malaysia|Malta|
Mauritius|Mexico|Moldova|Monaco|Mongolia|Montenegro|Morocco|Mozambique|Myanmar|Namibia|Nepal|Netherlands|
New Zealand|Nicaragua|Nigeria|North Macedonia|Norway|Oman|Pakistan|Panama|Paraguay|Peru|Philippines|Poland|
Portugal|Puerto Rico|Qatar|Romania|Russia|Rwanda|Saudi Arabia|Senegal|Serbia|Singapore|Slovakia|Slovenia|
South Africa|South Korea|Spain|Sri Lanka|Sweden|Switzerland|Taiwan|Tanzania|Thailand|Trinidad and Tobago|Tunisia|
Turkey|Uganda|Ukraine|United Arab Emirates|United Kingdom|United States|Uruguay|Uzbekistan|Venezuela|Vietnam|
Zambia|Zimbabwe
"""
COUNTRY_NAMES = [c.strip() for c in COUNTRIES.replace("\n", "").split("|") if c.strip()]

COUNTRY_ALIASES = {
    "us": "United States", "usa": "United States", "u.s.": "United States", "u.s.a.": "United States",
    "united states of america": "United States", "america": "United States",
    "uk": "United Kingdom", "u.k.": "United Kingdom", "great britain": "United Kingdom", "britain": "United Kingdom",
    "england": "United Kingdom", "scotland": "United Kingdom", "wales": "United Kingdom", "gb": "United Kingdom",
    "northern ireland": "United Kingdom",
    "uae": "United Arab Emirates", "u.a.e.": "United Arab Emirates",
    "deutschland": "Germany", "nederland": "Netherlands", "the netherlands": "Netherlands", "holland": "Netherlands",
    "españa": "Spain", "brasil": "Brazil", "méxico": "Mexico", "schweiz": "Switzerland", "suisse": "Switzerland",
    "österreich": "Austria", "italia": "Italy", "polska": "Poland", "sverige": "Sweden", "norge": "Norway",
    "danmark": "Denmark", "suomi": "Finland", "belgië": "Belgium", "belgique": "Belgium", "türkiye": "Turkey",
    "czech republic": "Czechia", "korea": "South Korea", "republic of korea": "South Korea",
    "korea, republic of": "South Korea", "viet nam": "Vietnam", "russian federation": "Russia",
    "hong kong sar": "Hong Kong", "macedonia": "North Macedonia", "ivory coast": "Côte d'Ivoire",
    "republic of ireland": "Ireland", "ksa": "Saudi Arabia", "prc": "China", "mainland china": "China",
    # ISO-3 codes some job boards use
    "gbr": "United Kingdom", "ind": "India", "can": "Canada", "deu": "Germany", "fra": "France",
    "esp": "Spain", "ita": "Italy", "nld": "Netherlands", "bra": "Brazil", "mex": "Mexico", "jpn": "Japan",
    "aus": "Australia", "irl": "Ireland", "sgp": "Singapore", "pol": "Poland", "isr": "Israel", "che": "Switzerland",
}
ISO2 = {
    "us": "United States", "gb": "United Kingdom", "uk": "United Kingdom", "ca": "Canada", "de": "Germany",
    "fr": "France", "es": "Spain", "it": "Italy", "nl": "Netherlands", "in": "India", "br": "Brazil",
    "mx": "Mexico", "jp": "Japan", "au": "Australia", "ie": "Ireland", "sg": "Singapore", "pl": "Poland",
    "pt": "Portugal", "se": "Sweden", "ch": "Switzerland", "at": "Austria", "be": "Belgium", "dk": "Denmark",
    "no": "Norway", "fi": "Finland", "cn": "China", "kr": "South Korea", "ar": "Argentina", "cl": "Chile",
    "co": "Colombia", "za": "South Africa", "ae": "United Arab Emirates", "il": "Israel", "ph": "Philippines",
}

# Broad areas that some remote jobs use instead of a country.
AREAS = {
    "europe": "Europe", "eu": "Europe", "emea": "Europe, Middle East & Africa", "latam": "Latin America",
    "latin america": "Latin America", "south america": "Latin America", "north america": "North America",
    "americas": "Americas", "apac": "Asia-Pacific", "asia": "Asia", "asia pacific": "Asia-Pacific",
    "africa": "Africa", "middle east": "Middle East", "oceania": "Oceania",
    "amer": "Americas", "worldwide": "Worldwide", "global": "Worldwide", "anywhere": "Worldwide", "international": "Worldwide",
}

# Major cities: name -> (region code or name, country). Region is a US/CA/AU
# code where applicable. Aliases point at the canonical city name.
_CITIES_US = """
New York:NY|Brooklyn:NY|Manhattan:NY|Buffalo:NY|Rochester:NY|Albany:NY|San Francisco:CA|Los Angeles:CA|
San Diego:CA|San Jose:CA|Santa Clara:CA|Sunnyvale:CA|Mountain View:CA|Palo Alto:CA|Menlo Park:CA|
Cupertino:CA|Redwood City:CA|San Mateo:CA|Foster City:CA|Oakland:CA|Berkeley:CA|Irvine:CA|Sacramento:CA|
Santa Monica:CA|Hawthorne:CA|Redlands:CA|Pasadena:CA|Long Beach:CA|Fremont:CA|Burbank:CA|Emeryville:CA|
South San Francisco:CA|Culver City:CA|Seattle:WA|Bellevue:WA|Redmond:WA|Kirkland:WA|Tacoma:WA|Spokane:WA|
Portland:OR|Austin:TX|Dallas:TX|Houston:TX|San Antonio:TX|Fort Worth:TX|Plano:TX|Irving:TX|Bastrop:TX|
Starbase:TX|McGregor:TX|Chicago:IL|Romeoville:IL|Evanston:IL|Boston:MA|Cambridge:MA|Somerville:MA|
Waltham:MA|Atlanta:GA|Miami:FL|Orlando:FL|Tampa:FL|Jacksonville:FL|Fort Lauderdale:FL|Cape Canaveral:FL|
Denver:CO|Boulder:CO|Colorado Springs:CO|Phoenix:AZ|Scottsdale:AZ|Tempe:AZ|Tucson:AZ|Las Vegas:NV|Reno:NV|
Salt Lake City:UT|Lehi:UT|Provo:UT|Minneapolis:MN|Saint Paul:MN|Detroit:MI|Ann Arbor:MI|Columbus:OH|
Cleveland:OH|Cincinnati:OH|Pittsburgh:PA|Philadelphia:PA|Nashville:TN|Memphis:TN|Charlotte:NC|Raleigh:NC|
Durham:NC|Washington:DC|Arlington:VA|Reston:VA|Richmond:VA|McLean:VA|Herndon:VA|Baltimore:MD|Bethesda:MD|
New Orleans:LA|Kansas City:MO|St. Louis:MO|Indianapolis:IN|Milwaukee:WI|Madison:WI|Omaha:NE|
Oklahoma City:OK|Louisville:KY|Honolulu:HI|Anchorage:AK|Boise:ID|Albuquerque:NM|Hartford:CT|Stamford:CT|
New Haven:CT|Newark:NJ|Jersey City:NJ|Hoboken:NJ|Princeton:NJ|Providence:RI|Burlington:VT|Des Moines:IA|
Birmingham:AL|Huntsville:AL|Little Rock:AR|Charleston:SC|Greenville:SC|Wilmington:DE|San Juan:PR
"""
_CITIES_CA = "Toronto:ON|Ottawa:ON|Waterloo:ON|Mississauga:ON|Vancouver:BC|Victoria:BC|Montreal:QC|Quebec City:QC|Calgary:AB|Edmonton:AB|Winnipeg:MB|Halifax:NS"
_CITIES_AU = "Sydney:NSW|Melbourne:VIC|Brisbane:QLD|Perth:WA|Adelaide:SA|Canberra:ACT|Hobart:TAS"
_CITIES_WORLD = """
London:United Kingdom|Manchester:United Kingdom|Edinburgh:United Kingdom|Glasgow:United Kingdom|
Bristol:United Kingdom|Leeds:United Kingdom|Cardiff:United Kingdom|Belfast:United Kingdom|Cambridge UK:United Kingdom|
Oxford:United Kingdom|Dublin:Ireland|Cork:Ireland|Galway:Ireland|Paris:France|Lyon:France|Marseille:France|
Toulouse:France|Nice:France|Lille:France|Berlin:Germany|Munich:Germany|Hamburg:Germany|Frankfurt:Germany|
Cologne:Germany|Düsseldorf:Germany|Stuttgart:Germany|Leipzig:Germany|Dresden:Germany|Hanover:Germany|
Nuremberg:Germany|Bonn:Germany|Karlsruhe:Germany|Mannheim:Germany|Amsterdam:Netherlands|Rotterdam:Netherlands|
The Hague:Netherlands|Utrecht:Netherlands|Eindhoven:Netherlands|Brussels:Belgium|Antwerp:Belgium|Ghent:Belgium|
Luxembourg City:Luxembourg|Zurich:Switzerland|Geneva:Switzerland|Basel:Switzerland|Lausanne:Switzerland|
Bern:Switzerland|Vienna:Austria|Graz:Austria|Salzburg:Austria|Madrid:Spain|Barcelona:Spain|Valencia:Spain|
Seville:Spain|Malaga:Spain|Bilbao:Spain|Lisbon:Portugal|Porto:Portugal|Milan:Italy|Rome:Italy|Turin:Italy|
Florence:Italy|Bologna:Italy|Naples:Italy|Stockholm:Sweden|Gothenburg:Sweden|Malmö:Sweden|Copenhagen:Denmark|
Aarhus:Denmark|Oslo:Norway|Bergen:Norway|Helsinki:Finland|Espoo:Finland|Reykjavik:Iceland|Warsaw:Poland|
Krakow:Poland|Wroclaw:Poland|Gdansk:Poland|Poznan:Poland|Prague:Czechia|Brno:Czechia|Budapest:Hungary|
Bucharest:Romania|Cluj-Napoca:Romania|Sofia:Bulgaria|Athens:Greece|Belgrade:Serbia|Zagreb:Croatia|
Ljubljana:Slovenia|Bratislava:Slovakia|Tallinn:Estonia|Riga:Latvia|Vilnius:Lithuania|Kyiv:Ukraine|
Lviv:Ukraine|Istanbul:Turkey|Ankara:Turkey|Tel Aviv:Israel|Jerusalem:Israel|Haifa:Israel|Dubai:United Arab Emirates|
Abu Dhabi:United Arab Emirates|Riyadh:Saudi Arabia|Jeddah:Saudi Arabia|Doha:Qatar|Cairo:Egypt|
Johannesburg:South Africa|Cape Town:South Africa|Nairobi:Kenya|Lagos:Nigeria|Accra:Ghana|Casablanca:Morocco|
Bangalore:India|Mumbai:India|Delhi:India|New Delhi:India|Gurgaon:India|Noida:India|Hyderabad:India|
Chennai:India|Pune:India|Kolkata:India|Ahmedabad:India|Singapore:Singapore|Hong Kong:Hong Kong|Tokyo:Japan|
Osaka:Japan|Kyoto:Japan|Yokohama:Japan|Seoul:South Korea|Busan:South Korea|Beijing:China|Shanghai:China|
Shenzhen:China|Guangzhou:China|Hangzhou:China|Chengdu:China|Wuxi:China|Taipei:Taiwan|Bangkok:Thailand|
Kuala Lumpur:Malaysia|Jakarta:Indonesia|Manila:Philippines|Makati:Philippines|Cebu:Philippines|
Ho Chi Minh City:Vietnam|Hanoi:Vietnam|Karachi:Pakistan|Lahore:Pakistan|Dhaka:Bangladesh|Colombo:Sri Lanka|
Auckland:New Zealand|Wellington:New Zealand|Mexico City:Mexico|Guadalajara:Mexico|Monterrey:Mexico|
São Paulo:Brazil|Rio de Janeiro:Brazil|Campinas:Brazil|Belo Horizonte:Brazil|Curitiba:Brazil|
Porto Alegre:Brazil|Florianópolis:Brazil|Buenos Aires:Argentina|Córdoba:Argentina|Santiago:Chile|
Bogotá:Colombia|Medellín:Colombia|Lima:Peru|Montevideo:Uruguay|Quito:Ecuador|San José CR:Costa Rica|
Panama City:Panama|Guatemala City:Guatemala
"""
CITY_ALIASES = {
    "nyc": "New York", "new york city": "New York", "manhattan": "New York", "sf": "San Francisco",
    "bay area": "San Francisco", "san francisco bay area": "San Francisco", "la": "Los Angeles",
    "bengaluru": "Bangalore", "gurugram": "Gurgaon", "bombay": "Mumbai", "münchen": "Munich",
    "muenchen": "Munich", "köln": "Cologne", "koeln": "Cologne", "zürich": "Zurich", "genève": "Geneva",
    "wien": "Vienna", "praha": "Prague", "warszawa": "Warsaw", "kraków": "Krakow", "wrocław": "Wroclaw",
    "lisboa": "Lisbon", "milano": "Milan", "roma": "Rome", "torino": "Turin", "firenze": "Florence",
    "napoli": "Naples", "bruxelles": "Brussels", "brussel": "Brussels", "den haag": "The Hague",
    "göteborg": "Gothenburg", "kyiv": "Kyiv", "kiev": "Kyiv", "sao paulo": "São Paulo", "bogota": "Bogotá",
    "medellin": "Medellín", "frankfurt am main": "Frankfurt", "hannover": "Hanover", "nürnberg": "Nuremberg",
    "dusseldorf": "Düsseldorf", "malmo": "Malmö", "washington d.c.": "Washington", "washington dc": "Washington",
    "d.c.": "Washington", "st louis": "St. Louis", "saint louis": "St. Louis", "st paul": "Saint Paul",
    "montréal": "Montreal", "québec": "Quebec City", "ciudad de méxico": "Mexico City", "cdmx": "Mexico City",
    "ho chi minh": "Ho Chi Minh City", "saigon": "Ho Chi Minh City", "tel aviv-yafo": "Tel Aviv",
    "hong kong island": "Hong Kong", "new delhi": "New Delhi", "tlv": "Tel Aviv",
    "san francisco bay": "San Francisco", "silicon valley": "San Jose",
}


@dataclass(frozen=True)
class City:
    name: str
    region: str | None   # US/CA/AU code, or None
    country: str


def _fold(s: str) -> str:
    """Lowercase, trim and strip accents for matching ("Zürich" -> "zurich")."""
    s = unicodedata.normalize("NFKD", s.strip().lower())
    return "".join(ch for ch in s if not unicodedata.combining(ch))


def _build():
    cities: dict[str, City] = {}
    for block, country, regions in ((_CITIES_US, "United States", US_STATES), (_CITIES_CA, "Canada", CA_PROVINCES),
                                    (_CITIES_AU, "Australia", AU_STATES)):
        for item in block.replace("\n", "").split("|"):
            if item.strip():
                name, code = item.strip().split(":")
                cities.setdefault(_fold(name), City(name, code, country))
    for item in _CITIES_WORLD.replace("\n", "").split("|"):
        if item.strip():
            name, country = item.strip().split(":")
            display = name.replace(" UK", "").replace(" CR", "")
            cities.setdefault(_fold(name), City(display, None, country))
            cities.setdefault(_fold(display), City(display, None, country))
    for alias, canon in CITY_ALIASES.items():
        if _fold(canon) in cities:
            cities[_fold(alias)] = cities[_fold(canon)]
    countries = {_fold(c): c for c in COUNTRY_NAMES}
    countries.update({_fold(k): v for k, v in COUNTRY_ALIASES.items()})
    return cities, countries


CITIES, COUNTRY_LOOKUP = _build()
_US_BY_NAME = {_fold(v): k for k, v in US_STATES.items()}
_CA_BY_NAME = {_fold(v): k for k, v in CA_PROVINCES.items()}
_AU_BY_NAME = {_fold(v): k for k, v in AU_STATES.items()}

# Words that describe the work arrangement or office, not a place.
_NOISE = re.compile(
    r"\b(remote|hybrid|on-?site|onsite|in-office|office|hq|headquarters|campus|work from home|wfh|"
    r"job|only|based|timezones?|time zones?|est|pst|cst|mst|utc|gmt|flexible|multiple locations|"
    r"various locations|locations?|metro(politan)? area|area|greater|region|n/a|none|tbd|select|blank|"
    r"distributed|perm|any)\b",
    re.I,
)

COUNTRY_SHORT = {"United States": "USA", "United Kingdom": "UK", "United Arab Emirates": "UAE"}


def _region_name(country: str, code: str) -> str:
    return {"United States": US_STATES, "Canada": CA_PROVINCES, "Australia": AU_STATES}[country][code]


def _match_region(tok: str, country_hint: str | None = None):
    """Return (country, code) if the token is a US state / CA province / AU state."""
    f = _fold(tok).replace(".", "")
    up = f.upper()
    order = [("United States", US_STATES, _US_BY_NAME), ("Canada", CA_PROVINCES, _CA_BY_NAME),
             ("Australia", AU_STATES, _AU_BY_NAME)]
    if country_hint:  # prefer the hinted country for ambiguous codes like "WA"
        order.sort(key=lambda o: o[0] != country_hint)
    for country, codes, names in order:
        if up in codes and len(up) >= 2:
            return country, up
        if f in names:
            return country, names[f]
    return None


def _match_country(tok: str) -> str | None:
    f = _fold(tok)
    return COUNTRY_LOOKUP.get(f) or COUNTRY_LOOKUP.get(f.replace(".", ""))


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
@dataclass
class Place:
    city: str | None = None
    region: str | None = None    # code for US/CA/AU
    country: str | None = None
    area: str | None = None      # "Europe", "Worldwide", ...


def parse(raw: str | None) -> list[Place]:
    if not raw:
        return []
    if "Ã" in raw:  # text that was decoded with the wrong encoding ("MÃ¼nchen")
        try:
            raw = raw.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    s = raw.replace("—", ",").replace("–", ",").replace(" - ", ",").replace("(", ",").replace(")", ",")
    # "US-CA-Menlo Park" style
    m = re.fullmatch(r"\s*([A-Za-z]{2})-([A-Za-z]{2,3})-([^,]+)\s*", s)
    if m:
        country = ISO2.get(m.group(1).lower())
        s = f"{m.group(3)}, {m.group(2)}, {country or m.group(1)}"
    m = re.fullmatch(r"\s*([A-Z]{2})-([A-Za-z][^,]+?)(?:-[^,]*)?\s*", s)  # "GB-London", "PL-Warsaw-Lixa C"
    if m and m.group(1).lower() in ISO2:
        s = f"{m.group(2)}, {ISO2[m.group(1).lower()]}"
    s = _NOISE.sub(" ", s)
    tokens = [re.sub(r"\s+", " ", t).strip(" .-:") for t in re.split(r",|;|/|\||:|\bor\b|\band\b|\bin the\b|\bin\b|&|\n", s, flags=re.I)]
    tokens = [t for t in tokens if t and not t.isdigit()]

    places: list[Place] = []
    pending: Place | None = None

    def flush():
        nonlocal pending
        if pending and (pending.country or pending.region):
            places.append(pending)
        pending = None

    for i, tok in enumerate(tokens):
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        f = _fold(tok)
        country = _match_country(tok)
        # A known city name that is also a country/state (e.g. "Singapore", "New York")
        city = CITIES.get(f)
        region = _match_region(tok, pending.country if pending else None)

        if pending and pending.city:
            # A state/province right after a city: attach it (or confirm the one we know).
            if region and not country and (not city or city.name == pending.city or city.name == _region_name(*region)):
                if not pending.region or pending.country == region[0]:
                    pending.country, pending.region = region
                continue
            # A district we don't know ("Noord-Holland", "Jiangsu", "BW") between a city and its country.
            if not country and not city and f not in AREAS:
                continue
        if country and not (city and country == city.country and city.name != country and nxt and _match_region(nxt)):
            if pending and pending.city:
                if not pending.country or pending.country == country or pending.region is None:
                    pending.country = country
                    if city and city.name == country:  # "Singapore, Singapore"
                        pass
                    flush()
                    continue
            flush()
            if city and city.country == country and city.name == country:
                places.append(Place(city=city.name, country=country))
            else:
                places.append(Place(country=country))
            continue
        if f in AREAS:
            flush()
            places.append(Place(area=AREAS[f]))
            continue
        if city:
            flush()
            pending = Place(city=city.name, region=city.region, country=city.country)
            continue
        if region and not pending:
            places.append(Place(region=region[1], country=region[0]))
            continue
        # Unknown token: a city we don't have on file if followed by a region/country.
        if nxt and (_match_region(nxt) or _match_country(nxt) or not _fold(nxt)) and len(tok) > 2:
            flush()
            pending = Place(city=_titlecase(tok))
            continue
        # Otherwise it's a district/region code we can't place ("BW", "Noord-Holland") — skip it.
    flush()

    # "Anywhere in the United States" means the US, not worldwide.
    if any(p.country for p in places):
        places = [p for p in places if p.area != "Worldwide"]

    # De-duplicate while keeping order.
    seen, out = set(), []
    for p in places:
        key = (p.city, p.region, p.country, p.area)
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def _titlecase(s: str) -> str:
    return s if any(c.isupper() for c in s[1:]) else " ".join(w.capitalize() for w in s.split())


# --------------------------------------------------------------------------- #
# Labels used by the filter and the job cards
# --------------------------------------------------------------------------- #
def country_label(country: str) -> str:
    return country


def region_label(country: str, code: str) -> str:
    return f"{_region_name(country, code)}, {COUNTRY_SHORT.get(country, country)}"


def city_label(p: Place) -> str:
    if p.region and p.country in ("United States", "Canada", "Australia"):
        return f"{p.city}, {p.region}, {COUNTRY_SHORT.get(p.country, p.country)}"
    return f"{p.city}, {COUNTRY_SHORT.get(p.country, p.country)}"


def filter_entries(places: list[Place], remote: bool) -> list[tuple[str, str]]:
    """(label, kind) pairs stored for filtering. A city also counts toward its
    state and country, so picking "United States" finds every US city."""
    out: list[tuple[str, str]] = []
    for p in places:
        if p.area:
            out.append((p.area, "area"))
            continue
        if p.country:
            out.append((country_label(p.country), "country"))
        if p.region and p.country in ("United States", "Canada", "Australia"):
            out.append((region_label(p.country, p.region), "region"))
        if p.city and p.country and p.city != p.country:
            out.append((city_label(p), "city"))
    return list(dict.fromkeys(out))


def display(places: list[Place], remote: bool, raw: str | None) -> str:
    """A short, consistent location line for job cards."""
    if not places:
        cleaned = (raw or "").strip()
        if remote:
            return "Remote"
        return cleaned if cleaned and cleaned.lower() not in ("none", "hybrid", "n/a") else "Location not listed"
    parts = []
    for p in places:
        if p.area:
            parts.append(p.area)
        elif p.city and p.city != p.country:
            parts.append(city_label(p))
        elif p.region:
            parts.append(region_label(p.country, p.region))
        else:
            parts.append(COUNTRY_SHORT.get(p.country, p.country))
    text = parts[0] if len(parts) == 1 else f"{parts[0]} +{len(parts) - 1} more"
    return f"Remote · {text}" if remote else text
