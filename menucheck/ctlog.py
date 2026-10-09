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

# crt.sh's free public database (guest access) indexes each part of a website name REVERSED, so
# "nemar…" finds parts ending in "ramen" (kaiyoramen.com, ramen-bar.com). One search per letter before
# "ramen" keeps each search small enough for the free service.
SEARCHES = ["nemar"] + [f"nemar{c}:*" for c in "abcdefghijklmnopqrstuvwxyz0123456789"]
# Endings of US-facing websites (skips e.g. .nl/.be, where "ramen" means "windows").
US_ENDINGS = {"com", "net", "us", "co", "org", "biz", "restaurant", "kitchen", "menu", "shop", "store", "online",
              "site", "nyc", "la", "info", "app", "io", "food", "bar", "cafe", "xyz", "live", "club", "place",
              "website", "space", "tech", "page", "eat", "life", "fun", "top", "vip", "pro", "bz", "ai"}
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
    return bool(RAMEN_IN_DOMAIN.search(domain.split(".")[0]))


def _add(found: dict, by: dict, search: str, name: str, day: str) -> None:
    d = registrable(name)
    if d and d.rsplit(".", 1)[-1] in US_ENDINGS and ramen_domain(d):
        by.setdefault(d, set()).add(search)
        if d not in found or (day and day < found[d]):
            found[d] = day


SQL = """SELECT lower(cai.NAME_VALUE), min(x509_notBefore(cai.CERTIFICATE))::date
         FROM certificate_and_identities cai
         WHERE to_tsquery('certwatch', %s) @@ identities(cai.CERTIFICATE)
           AND x509_notAfter(cai.CERTIFICATE) > now()
         GROUP BY 1 LIMIT 100000"""


def all_domains(log=print, budget_s: float = 25 * 60) -> tuple[dict[str, str], dict[str, set[str]], set[str]]:
    """({domain: earliest current cert date}, {domain: searches that found it}, searches that answered)."""
    try:
        import psycopg
    except ImportError:
        log("    crt.sh: psycopg not installed")
        return {}, {}, set()
    found: dict[str, str] = {}
    by: dict[str, set[str]] = {}
    ok: set[str] = set()
    deadline = time.time() + budget_s
    for q in SEARCHES:
        if time.time() > deadline:
            log(f"    crt.sh: out of time, {len(SEARCHES) - SEARCHES.index(q)} searches left for next run")
            break
        t0 = time.time()
        try:
            # A fresh connection each time: the free service drops a connection after any error.
            with psycopg.connect(host="crt.sh", port=5432, dbname="certwatch", user="guest", connect_timeout=30,
                                 autocommit=True, application_name="KnewdleNOW-MenuCheck") as conn, conn.cursor() as cur:
                cur.execute("SET statement_timeout = '150s'")
                cur.execute(SQL, (q,))
                rows = cur.fetchall()
        except Exception as e:  # noqa: BLE001
            log(f"    crt.sh {q}: {type(e).__name__}: {str(e).splitlines()[0][:120] if str(e) else ''}")
            time.sleep(5)
            continue
        ok.add(q)
        n0 = len(found)
        for name, day in rows:
            _add(found, by, q, name, str(day or ""))
        log(f"    crt.sh {q}: {len(rows)} names, {len(found) - n0} new ramen domains ({time.time() - t0:.0f}s)")
        time.sleep(2)  # be gentle with a free public service
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
