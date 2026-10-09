"""New ramen websites, spotted in public Certificate Transparency logs.

Every website that turns on HTTPS gets a certificate, and every certificate is published in public logs
(that's what the logs are for: anyone may check them). crt.sh offers a free search over them.
A new "somethingramen.com" usually means a shop is opening.

For each ramen-named domain we haven't looked at, we read the site's own homepage (and a contact/location
page) politely, keep it only if it's a ramen restaurant with a US street address, and remember the day we
first saw the domain so brand-new ones can be marked as new.
"""
from __future__ import annotations

import json
import re
import time

import requests
from bs4 import BeautifulSoup

from .crawl import Crawler
from .places import RAMEN_IN_DOMAIN, RAMEN_WORD, Geocoder, find_locations, host_of

CRTSH = "https://crt.sh/"
# Indexed (fast) searches on crt.sh: names ending in these, or starting with "ramen".
PATTERNS = [
    "%ramen.com", "%ramen.net", "%ramen.us", "%ramen.co", "%ramen.org", "%ramen.biz", "%ramen.restaurant",
    "%ramen.kitchen", "%ramen.menu", "%ramen.shop", "%ramen.store", "%ramen.online", "%ramen.site",
    "%ramenbar.com", "%ramenhouse.com", "%ramenshop.com", "%ramenco.com", "%ramenkitchen.com",
    "%ramenlab.com", "%ramenbar.net", "%ramenya.com", "%ramenspot.com", "%ramenexpress.com", "%ramenclub.com",
    "%ramenhub.com", "%ramenstudio.com", "%ramennoodle.com", "%ramennoodles.com", "%ramenandsushi.com",
    "%ramenonline.com", "%ramenrestaurant.com", "%ramenusa.com", "%ramentx.com", "%ramenla.com",
    "%ramennyc.com", "%ramenatx.com", "%ramenpdx.com", "%ramensf.com", "%ramenchicago.com", "ramen%.com",
    "ramen%.net", "ramen%.us", "ramen%.co",
]
_TWO_PART = {"co", "com", "net", "org", "gov", "edu", "ac"}
PARKED = re.compile(r"domain (?:is )?for sale|buy this domain|this domain may be for sale|parked free|"
                    r"domain has (?:been )?registered|website coming soon|under construction|godaddy\b.*\bparked|"
                    r"hostinger|this site can.t be reached|account suspended", re.IGNORECASE)
CONTACT_HINT = re.compile(r"contact|location|find[- ]?us|visit|about|hours", re.IGNORECASE)
NOT_RESTAURANT = re.compile(r"recipe|blog|instant noodle|cup noodle|wholesale|supplier|merch|t-shirt|podcast|"
                            r"anime|game|crypto|nft|token", re.IGNORECASE)


def registrable(name: str) -> str | None:
    name = name.strip().lower().lstrip("*.").rstrip(".")
    if not name or " " in name or "@" in name:
        return None
    parts = name.split(".")
    if len(parts) < 2:
        return None
    if len(parts) >= 3 and parts[-2] in _TWO_PART and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def ramen_domain(domain: str) -> bool:
    label = domain.split(".")[0]
    return bool(RAMEN_IN_DOMAIN.search(label))


def search_crtsh(pattern: str, session: requests.Session, log=print) -> dict[str, str] | None:
    """{domain: earliest certificate date seen} for one crt.sh pattern. None on trouble (logged)."""
    for attempt in range(3):
        try:
            r = session.get(CRTSH, params={"q": pattern, "output": "json", "exclude": "expired"}, timeout=180)
            if r.status_code == 200:
                rows = r.json()
                break
            log(f"    crt.sh {pattern}: HTTP {r.status_code}")
        except (requests.RequestException, ValueError) as e:
            log(f"    crt.sh {pattern}: {type(e).__name__}")
        time.sleep(15 * (attempt + 1))
    else:
        return None
    out: dict[str, str] = {}
    for row in rows:
        names = set(str(row.get("name_value") or "").split("\n")) | {str(row.get("common_name") or "")}
        day = str(row.get("not_before") or row.get("entry_timestamp") or "")[:10]
        for n in names:
            d = registrable(n)
            if d and ramen_domain(d):
                if d not in out or (day and day < out[d]):
                    out[d] = day
    return out


def all_domains(log=print) -> tuple[dict[str, str], dict[str, set[str]], set[str]]:
    """({domain: earliest cert date}, {domain: patterns that found it}, patterns that answered)."""
    s = requests.Session()
    s.headers["User-Agent"] = "KnewdleNOW-MenuCheck/1.0 (+https://swatsrocks.github.io/knewdle-data/)"
    found: dict[str, str] = {}
    by: dict[str, set[str]] = {}
    ok: set[str] = set()
    for p in PATTERNS:
        got = search_crtsh(p, s, log)
        if got is not None:
            ok.add(p)
        for d, day in (got or {}).items():
            by.setdefault(d, set()).add(p)
            if d not in found or (day and day < found[d]):
                found[d] = day
        log(f"    crt.sh {p}: {'no answer' if got is None else f'{len(got)} domains'}")
        time.sleep(3)  # be gentle with a free public service
    return found, by, ok


def read_domain(crawler: Crawler, domain: str, geocode: Geocoder) -> dict:
    """Look at one domain's own site. Returns {status, name, locations:[...]}."""
    home = crawler.fetch_home(domain)
    if home is None:
        return {"status": "unreachable"}
    final = host_of(home.url)
    if final and not final.endswith(host_of(domain)) and not ramen_domain(registrable(final) or ""):
        return {"status": "elsewhere", "to": final}  # forwards to some other site (a platform, a parked page…)
    locs, text, title = find_locations(home.content)
    pages = [text]
    if not locs:
        soup = BeautifulSoup(home.content, "html.parser")
        links = []
        for a in soup.find_all("a", href=True):
            href = requests.compat.urljoin(home.url, a["href"]).split("#")[0]
            if host_of(href) == final and CONTACT_HINT.search(a.get_text(" ", strip=True) + " " + href):
                links.append(href)
        for link in list(dict.fromkeys(links))[:2]:
            time.sleep(crawler.crawl_delay(link))
            r = crawler.fetch(link)
            if r is None:
                continue
            more, t, _ = find_locations(r.content)
            pages.append(t)
            locs += more
            if locs:
                break
    alltext = "\n".join(pages)
    if len(alltext) < 200 and not locs:
        return {"status": "empty"}
    if PARKED.search(alltext[:3000]) and not locs:
        return {"status": "parked"}
    if not RAMEN_WORD.search(alltext):
        return {"status": "not ramen"}
    if NOT_RESTAURANT.search(title or "") and not locs:
        return {"status": "not a restaurant"}
    name = (locs[0].name if locs and locs[0].name else None) or title or domain
    if len(name) > 60:
        name = name[:60].rsplit(" ", 1)[0]
    places = []
    seen = set()
    for loc in locs[:10]:
        if loc.key in seen:
            continue
        seen.add(loc.key)
        if not geocode.locate(loc):
            continue
        places.append({"key": loc.key, "lat": loc.lat, "lon": loc.lon, "address": loc.address, "soon": loc.soon})
    if not places:
        return {"status": "no US address", "name": name}
    return {"status": "ok", "name": name, "website": home.url, "locations": places}


__all__ = ["all_domains", "read_domain", "registrable", "ramen_domain", "json"]
