"""Shared helpers for finding ramen places beyond the menu check.

  * find_locations(): US street addresses (and map pins) on a restaurant's own web page.
  * geocode(): turns an address into a map point with the US Census Bureau's free geocoder
    (a public federal service: no key, no account, no fees).
  * same_place(): the app's "is this the same shop?" rule, so nothing is published twice.
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

import requests
from bs4 import BeautifulSoup

# "ramen" as a word start, so "Sacramento" (sac-RAMEN-to) doesn't count.
RAMEN_WORD = re.compile(r"(?<![a-z])(?:ramen|ラーメン|らーめん|拉麺|拉麵)", re.IGNORECASE)
RAMEN_IN_DOMAIN = re.compile(r"(?<!sac)ramen", re.IGNORECASE)

STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS",
    "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC",
    "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "PR",
}
STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO",
    "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC", "puerto rico": "PR",
}
_STATE_ALT = "|".join(sorted(STATES)) + "|" + "|".join(sorted((n.title() for n in STATE_NAMES), key=len, reverse=True))
_SUFFIX = (
    r"St|Street|Ave|Avenue|Av|Blvd|Boulevard|Rd|Road|Dr|Drive|Way|Ln|Lane|Pkwy|Parkway|Hwy|Highway|Ct|Court|"
    r"Pl|Place|Plaza|Sq|Square|Ter|Terrace|Cir|Circle|Trl|Trail|Pike|Broadway|Row|Loop|Expy|Expressway|Fwy|"
    r"Freeway|Center|Ctr|Crossing|Xing|Market|Mall|Alley|Walk|Commons|Promenade|Tpke|Turnpike|Run|Path|Bowery"
)
_UNIT = r"(?:(?:Suite|Ste|Unit|Space|Spc|Bldg|Building|Fl|Floor|Level|Rm|Room|#)\.?\s*#?\s*[\w-]+|[A-Z]?-?\d{1,4}[A-Z]?(?=[\s,]))"
# 1234 W Main St, Suite 5, Charlotte, NC 28202
_ADDRESS = re.compile(
    rf"(?<![\w$.])(?P<street>\d{{1,6}}[A-Za-z]?(?:-\d{{1,5}})?\s+(?:[NSEW]\.?\s+|North\s+|South\s+|East\s+|West\s+)?"
    rf"(?:[A-Za-z0-9.'&\- ]{{1,40}}?\s)?(?:{_SUFFIX})\b\.?(?:\s+(?:[NSEW]{{1,2}})\b\.?)?)"
    rf"(?:[\s,]+(?P<unit>{_UNIT}))?"
    rf"[\s,]+(?P<city>[A-Z][A-Za-z.'\-]+(?:\s[A-Z][A-Za-z.'\-]+){{0,3}}),?\s+"
    rf"(?P<state>{_STATE_ALT})\.?,?\s+(?P<zip>\d{{5}})(?:-\d{{4}})?\b"
)
_COORD_PATTERNS = (
    re.compile(r"!3d(-?\d{1,2}\.\d{3,})!4d(-?\d{1,3}\.\d{3,})"),
    re.compile(r"@(-?\d{1,2}\.\d{3,}),(-?\d{1,3}\.\d{3,})"),
    re.compile(r"[?&](?:q|ll|daddr|destination|center|query)=(-?\d{1,2}\.\d{3,}),\s*(-?\d{1,3}\.\d{3,})"),
)
SOON = re.compile(r"coming soon|opening soon|now hiring for our new|grand opening (?:date )?tba", re.IGNORECASE)


@dataclass
class Location:
    street: str
    city: str
    state: str
    zip: str
    lat: float | None = None
    lon: float | None = None
    name: str | None = None
    soon: bool = False

    @property
    def address(self) -> str:
        return f"{self.street}, {self.city}, {self.state}"

    @property
    def oneline(self) -> str:
        return f"{self.street}, {self.city}, {self.state} {self.zip}"

    @property
    def key(self) -> str:
        return re.sub(r"[^a-z0-9]", "", (self.street.split()[0] + self.zip).lower())


def _state_code(s: str) -> str | None:
    s = s.strip().rstrip(".")
    if s.upper() in STATES:
        return s.upper()
    return STATE_NAMES.get(s.lower())


def _clean(s: str) -> str:
    return " ".join(s.replace(" ", " ").split()).strip(" ,")


def addresses_in_text(text: str) -> list[tuple[Location, int]]:
    """US street addresses in plain text, with where each one starts."""
    flat = re.sub(r"\s*\n\s*", ", ", text.replace(" ", " "))
    flat = re.sub(r"(?:,\s*){2,}", ", ", flat)
    out, seen = [], set()
    matches = list(_ADDRESS.finditer(flat))
    for i, m in enumerate(matches):
        st = _state_code(m.group("state"))
        if not st:
            continue
        street = _clean(m.group("street"))
        if m.group("unit"):
            street += " " + _clean(m.group("unit"))
        loc = Location(street, _clean(m.group("city")), st, m.group("zip"))
        if loc.key in seen:
            continue
        seen.add(loc.key)
        out.append((loc, m))
    # "Coming soon" belongs to whichever address it sits closest to (within a short distance).
    for s in SOON.finditer(flat):
        best, gap = None, 151
        for loc, m in out:
            g = 0 if m.start() <= s.start() <= m.end() else min(abs(s.start() - m.end()), abs(m.start() - s.end()))
            if g < gap or (g == gap and m.start() > s.start()):  # a tie goes to the heading's address below
                best, gap = loc, g
        if best is not None:
            best.soon = True
    return [(loc, m.start()) for loc, m in out]


def _walk_ld(obj):
    if isinstance(obj, list):
        for x in obj:
            yield from _walk_ld(x)
    elif isinstance(obj, dict):
        yield obj
        for k in ("@graph", "location", "department", "subOrganization", "containsPlace", "itemListElement", "item"):
            if k in obj:
                yield from _walk_ld(obj[k])


def _ld_locations(soup: BeautifulSoup) -> list[Location]:
    out = []
    for tag in soup.find_all("script", type=lambda t: t and "ld+json" in t.lower()):
        try:
            data = json.loads(tag.string or tag.get_text() or "")
        except (ValueError, TypeError):
            continue
        for o in _walk_ld(data):
            addr = o.get("address")
            if isinstance(addr, list):
                addr = addr[0] if addr else None
            if not isinstance(addr, dict):
                continue
            street = _clean(str(addr.get("streetAddress") or ""))
            city = _clean(str(addr.get("addressLocality") or ""))
            st = _state_code(str(addr.get("addressRegion") or ""))
            zp = re.match(r"\d{5}", str(addr.get("postalCode") or "").strip())
            country = str(addr.get("addressCountry") or "US")
            if isinstance(addr.get("addressCountry"), dict):
                country = str(addr["addressCountry"].get("name") or "US")
            if not (street and city and st and zp) or country.upper() not in ("US", "USA", "UNITED STATES"):
                continue
            loc = Location(street, city, st, zp.group(0), name=_clean(str(o.get("name") or "")) or None)
            geo = o.get("geo") if isinstance(o.get("geo"), dict) else None
            if geo:
                try:
                    loc.lat, loc.lon = float(geo.get("latitude")), float(geo.get("longitude"))
                except (TypeError, ValueError):
                    pass
            out.append(loc)
    return out


def _map_pins(soup: BeautifulSoup) -> list[tuple[float, float]]:
    pins = []
    for el in soup.find_all(["a", "iframe"]):
        link = unquote(el.get("href") or el.get("src") or "")
        if "google." not in link and "maps.apple" not in link and "goo.gl" not in link:
            continue
        for pat in _COORD_PATTERNS:
            m = pat.search(link)
            if m:
                lat, lon = float(m.group(1)), float(m.group(2))
                if 15 < lat < 72 and -180 < lon < -60:
                    pins.append((lat, lon))
                break
    return list(dict.fromkeys(pins))


def page_title(soup: BeautifulSoup) -> str | None:
    for meta in ("og:site_name", "og:title"):
        tag = soup.find("meta", property=meta)
        if tag and tag.get("content"):
            return _clean(tag["content"])
    if soup.title and soup.title.string:
        return _clean(re.split(r"\s+[|\-–—•:]\s+", soup.title.string.strip())[0])
    return None


def find_locations(html: bytes | str) -> tuple[list[Location], str, str | None]:
    """(locations, page text, page title) for one page. Structured data wins; text addresses fill in."""
    soup = BeautifulSoup(html, "html.parser")
    title = page_title(soup)
    found = _ld_locations(soup)
    pins = _map_pins(soup)
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    keys = {l.key for l in found}
    for loc, _ in addresses_in_text(text):
        if loc.key not in keys:
            keys.add(loc.key)
            found.append(loc)
    # One address and one map pin on the page: the pin is that address.
    if len(found) == 1 and len(pins) == 1 and found[0].lat is None:
        found[0].lat, found[0].lon = pins[0]
    return found, text, title


# ---------- geocoding (US Census Bureau, free) ----------

CENSUS = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"


class Geocoder:
    """Address -> (lat, lon). Remembers answers (also across runs) so each address is looked up once."""

    def __init__(self, cache: dict | None = None, user_agent: str = "KnewdleNOW-MenuCheck/1.0"):
        self.cache: dict = cache if cache is not None else {}
        self.ua = user_agent
        self._lock = threading.Lock()
        self._last = 0.0

    def __call__(self, loc: Location) -> tuple[float, float] | None:
        key = loc.oneline.lower()
        if key in self.cache:
            v = self.cache[key]
            return tuple(v) if v else None
        with self._lock:  # gentle on a free public service: about 4 lookups a second at most
            wait = 0.25 - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
        point = None
        for attempt in range(2):
            try:
                r = requests.get(CENSUS, params={"address": loc.oneline, "benchmark": "Public_AR_Current",
                                                 "format": "json"}, headers={"User-Agent": self.ua}, timeout=30)
                if r.status_code == 200:
                    matches = r.json().get("result", {}).get("addressMatches", [])
                    if matches:
                        c = matches[0]["coordinates"]
                        point = (round(float(c["y"]), 6), round(float(c["x"]), 6))
                    break
            except (requests.RequestException, ValueError, KeyError):
                time.sleep(2 * (attempt + 1))
        else:
            return None  # service trouble: don't remember a failure
        self.cache[key] = list(point) if point else None
        return point

    def locate(self, loc: Location) -> bool:
        if loc.lat is None:
            p = self(loc)
            if p:
                loc.lat, loc.lon = p
        return loc.lat is not None


# ---------- same shop? (mirrors the app's RamenMerge.isSamePlace) ----------

_FILLER = {"ramen", "restaurant", "noodle", "noodles", "house", "bar", "kitchen", "the", "and", "japanese", "shop"}


def _words(n: str) -> set[str]:
    return {w for w in re.split(r"[^a-z0-9]+", n.lower()) if len(w) >= 3 and w not in _FILLER}


def distance_m(a_lat, a_lon, b_lat, b_lon) -> float:
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp, dl = p2 - p1, math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def same_place(a: dict, b: dict) -> bool:
    d = distance_m(a["lat"], a["lon"], b["lat"], b["lon"])
    if d > 150:
        return False
    na, nb = a["name"].lower().strip(), b["name"].lower().strip()
    if na == nb or (d < 20 and (na in nb or nb in na)):
        return True
    wa, wb = _words(na), _words(nb)
    if wa and wb and wa & wb and d < 120:
        return True
    ha, hb = host_of(a.get("website") or ""), host_of(b.get("website") or "")
    return bool(ha and ha == hb and d < 150)


def host_of(url: str) -> str:
    if not url:
        return ""
    u = url if url.startswith("http") else "https://" + url
    return (urlparse(u).hostname or "").lower().removeprefix("www.")


class PlaceIndex:
    """Fast "is this already known?" lookups over many places, using a coarse grid."""

    def __init__(self, places=()):
        self.grid: dict[tuple[int, int], list[dict]] = {}
        self.hosts: set[str] = set()
        for p in places:
            self.add(p)

    @staticmethod
    def _cell(lat, lon):
        return int(lat * 100), int(lon * 100)  # ~1 km cells

    def add(self, p: dict):
        self.grid.setdefault(self._cell(p["lat"], p["lon"]), []).append(p)
        h = host_of(p.get("website") or "")
        if h:
            self.hosts.add(h)

    def has(self, p: dict) -> bool:
        ci, cj = self._cell(p["lat"], p["lon"])
        for i in (ci - 1, ci, ci + 1):
            for j in (cj - 1, cj, cj + 1):
                for q in self.grid.get((i, j), ()):
                    if same_place(p, q):
                        return True
        return False
