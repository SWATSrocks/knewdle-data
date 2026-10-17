"""Chain locations that the chain itself no longer lists (see "directories" in chains.json).

Big chains keep a public location directory: a page per state linking to a page per city, linking to a page
per store (e.g. locations.noodles.com/nc -> /nc/charlotte -> /nc/charlotte/8016-providence-rd). Map data
(Overture, Foursquare, OpenStreetMap) can keep a closed store for months; the chain's own directory drops it
when it closes. So for every listing of such a chain that Knewdle NOW can show, we look for a store in the
same state whose address starts with the same house number. None there -> closed.

Read politely like everything else (robots.txt, Crawl-delay), only the states that have listings, at most
once a week (results are cached in state/chain_dirs.json). Safety: a state whose pages can't all be read is
skipped, and if a chain would lose more than MAX_SHARE of its listings at once, nothing is hidden for it
(that smells like the directory changed shape, not like real closures).
"""
from __future__ import annotations

import datetime as dt
import json
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "chains.json"
CACHE = ROOT / "state" / "chain_dirs.json"
REFRESH_DAYS = 6
MAX_SHARE = 0.25        # never hide more than this share of a chain's listings in one go
MAX_PAGES = 600         # per chain per refresh
STATE = re.compile(r",\s*([A-Z]{2})(?:\s+\d{5}(?:-\d{4})?)?\s*$")
HOUSE = re.compile(r"^\s*(\d+[a-z]?)\b", re.IGNORECASE)


def directories() -> list[dict]:
    return json.loads(CONFIG.read_text()).get("directories", []) if CONFIG.exists() else []


def state_of(address: str | None) -> str | None:
    m = STATE.search(address or "")
    return m.group(1).lower() if m else None


def house_of(address: str | None) -> str | None:
    m = HOUSE.match(address or "")
    return m.group(1).lower() if m else None


def _segments(url: str) -> list[str]:
    return [s for s in urlparse(url).path.split("/") if s]


def _links(html: bytes, base: str, host: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for a in soup.find_all("a", href=True):
        u = urljoin(base, a["href"]).split("#")[0].split("?")[0].rstrip("/")
        if urlparse(u).hostname == host:
            out.append(u)
    return list(dict.fromkeys(out))


def read_state(crawler, d: dict, st: str, log=print) -> set[str] | None:
    """Every store the chain lists in one state as "city/street-slug", or None if it couldn't all be read."""
    base = d["directory"].rstrip("/")
    host = urlparse(base).hostname
    depth = len(_segments(base))           # path segments before the state (0 for locations.noodles.com)
    delay = max(crawler.delay, min(crawler.crawl_delay(base + "/"), 20))
    r = crawler.fetch(f"{base}/{st}")
    if r is None:
        log(f"    {d['name']}: no page for {st.upper()} (not allowed, unavailable, or no stores there)")
        return _maybe_empty(crawler, base, st)
    stores: set[str] = set()
    cities: list[str] = []

    def take(url: str):
        seg = _segments(url)
        if len(seg) == depth + 3 and seg[depth] == st and house_of(seg[depth + 2].replace("-", " ")):
            stores.add(f"{seg[depth + 1]}/{seg[depth + 2]}")   # "charlotte/8016-providence-rd"

    take(r.url)                            # a state with one store may send us straight to it
    for u in _links(r.content, r.url, host):
        seg = _segments(u)
        if len(seg) == depth + 2 and seg[depth] == st:
            cities.append(u)
        take(u)
    for city in cities:
        time.sleep(delay)
        cr = crawler.fetch(city)
        if cr is None:
            log(f"    {d['name']}: couldn't read {city}; skipping {st.upper()} this time")
            return None
        take(cr.url)                       # a city with one store often redirects to it
        for u in _links(cr.content, cr.url, host):
            take(u)
    if not stores:
        log(f"    {d['name']}: found no stores on the {st.upper()} pages; skipping (page layout changed?)")
        return None
    return stores


def _maybe_empty(crawler, base: str, st: str) -> set[str] | None:
    """The state page is missing: real (no stores left in that state) only if the directory's index still
    works and simply doesn't list the state."""
    r = crawler.fetch(base + "/") or crawler.fetch(base + "/index")
    if r is None:
        return None
    host = urlparse(base).hostname
    depth = len(_segments(base))
    states = {_segments(u)[depth] for u in _links(r.content, r.url, host) if len(_segments(u)) == depth + 1}
    states = {s for s in states if re.fullmatch(r"[a-z]{2}", s)}
    if len(states) >= 5 and st not in states:
        return set()                       # the chain has left this state entirely
    return None


STREET_FILLER = {"st", "street", "rd", "road", "ave", "avenue", "blvd", "dr", "drive", "ln", "lane", "way", "ct", "pl",
                 "pkwy", "parkway", "hwy", "highway", "n", "s", "e", "w", "north", "south", "east", "west", "ne", "nw",
                 "se", "sw", "ste", "suite", "unit", "us", "the", "and", "at", "of"}


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def _street_words(street: str) -> set[str]:
    """'2255 N Main St' -> {'main'}; '303 16th Street' -> {'16th'} (house numbers and filler words dropped)."""
    return {w for w in re.split(r"[^a-z0-9]+", street.lower()) if w and not w.isdigit() and w not in STREET_FILLER}


def still_listed(address: str, stores: list[str]) -> bool:
    """Is this listing among the chain's stores? Same house number anywhere in the state, or (map data often
    has the wrong number) a store on the same street in the same city."""
    num = house_of(address)
    if any(house_of(x.split("/", 1)[1].replace("-", " ")) == num for x in stores):
        return True
    parts = [p.strip() for p in address.split(",")]
    if len(parts) < 3:
        return False
    street, city = parts[0], _slug(parts[-2])
    words = _street_words(street)
    for x in stores:
        c, slug = x.split("/", 1)
        if c == city and words & _street_words(slug.replace("-", " ")):
            return True
    return False


def chain_listings(d: dict, listed: list[dict]) -> list[dict]:
    rx = re.compile(d["match"], re.IGNORECASE)
    return [p for p in listed if rx.search(p.get("name") or "")]


def check(crawler, listed: list[dict], log=print, today: str | None = None) -> list[dict]:
    """Closed entries ({"n","la","lo","why":"chain"}) for every configured chain directory."""
    today = today or dt.date.today().isoformat()
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    out: list[dict] = []
    for d in directories():
        mine = chain_listings(d, listed)
        if not mine:
            continue
        rec = cache.setdefault(d["name"], {"states": {}})
        wanted = sorted({s for s in (state_of(p.get("address")) for p in mine) if s})
        for st in wanted:
            prev = rec["states"].get(st)
            fresh = prev and (dt.date.fromisoformat(today) - dt.date.fromisoformat(prev["checked"])).days < REFRESH_DAYS
            if fresh:
                continue
            try:
                stores = read_state(crawler, d, st, log)
            except Exception as e:  # noqa: BLE001 - one state's trouble mustn't stop the rest
                log(f"    {d['name']}: {st.upper()} failed ({type(e).__name__})")
                stores = None
            if stores is not None:
                rec["states"][st] = {"checked": today, "stores": sorted(stores)}
            elif prev:
                pass                       # keep the last good read (it's at most a few weeks old)
        gone = []
        for p in mine:
            st, num = state_of(p.get("address")), house_of(p.get("address"))
            s = rec["states"].get(st or "")
            if not (st and num and s):
                continue                   # can't tell: leave it alone
            age = (dt.date.fromisoformat(today) - dt.date.fromisoformat(s["checked"])).days
            if age <= 30 and not still_listed(p["address"], s["stores"]):
                gone.append(p)
        if len(gone) > MAX_SHARE * len(mine):
            log(f"  {d['name']}: {len(gone)} of {len(mine)} listings missing from its directory: too many at once, "
                f"hiding none (check the directory by hand)")
            rec["last"] = {"date": today, "listings": len(mine), "missing": len(gone), "hidden": 0}
            continue
        log(f"  {d['name']}: {len(mine)} listings, {len(gone)} no longer in the chain's own directory")
        rec["last"] = {"date": today, "listings": len(mine), "missing": len(gone), "hidden": len(gone)}
        out += [{"n": p["name"], "la": p["lat"], "lo": p["lon"], "why": "chain",
                 "a": p.get("address")} for p in gone]
    CACHE.write_text(json.dumps(cache, indent=1, sort_keys=True))
    return out
