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
    return bool(RAMEN_IN_DOMAIN.search(domain.split(".")[0]))


def search_crtsh(pattern: str, session: requests.Session, log=print) -> dict[str, str] | None:
    """{domain: earliest certificate date seen} for one crt.sh pattern. None on trouble (logged)."""
    for attempt in range(1):
        try:
            r = session.get(CRTSH, params={"q": pattern, "output": "json", "exclude": "expired"}, timeout=(15, 75))
            if r.status_code == 200:
                rows = r.json()
                break
            log(f"    crt.sh {pattern}: HTTP {r.status_code}")
        except (requests.RequestException, ValueError) as e:
            log(f"    crt.sh {pattern}: {type(e).__name__}")
        time.sleep(10)
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


def _add(found: dict, by: dict, pattern: str, name: str, day: str) -> None:
    d = registrable(name)
    if d and ramen_domain(d):
        by.setdefault(d, set()).add(pattern)
        if d not in found or (day and day < found[d]):
            found[d] = day


def _search_db(log=print, budget_s: float = 20 * 60):
    """The same logs through crt.sh's free public database (guest access), used when its website is busy.
    Returns ({domain: earliest cert date}, {domain: patterns}, patterns answered) or None if unreachable."""
    try:
        import psycopg
    except ImportError:
        log("    crt.sh database: psycopg not installed")
        return None
    found: dict[str, str] = {}
    by: dict[str, set[str]] = {}
    ok: set[str] = set()
    deadline = time.time() + budget_s
    try:
        conn = psycopg.connect(host="crt.sh", port=5432, dbname="certwatch", user="guest",
                               connect_timeout=30, autocommit=True, application_name="KnewdleNOW-MenuCheck")
    except Exception as e:  # noqa: BLE001
        log(f"    crt.sh database: can't connect ({type(e).__name__}: {str(e)[:120]})")
        return None
    with conn:
        for p in PATTERNS:
            if time.time() > deadline:
                log("    crt.sh database: out of time for this run")
                break
            if p.startswith("%"):
                # Ending-with search: crt.sh indexes reversed names, so this is a fast lookup.
                where, arg = "reverse(lower(cai.NAME_VALUE)) LIKE reverse(lower(%s))", p
            else:
                where, arg = "lower(cai.NAME_VALUE) LIKE lower(%s)", p.split("%")[0] + "%"
            sql = f"""
                SELECT lower(cai.NAME_VALUE), min(x509_notBefore(cai.CERTIFICATE))::date
                FROM certificate_and_identities cai
                WHERE {where} AND x509_notAfter(cai.CERTIFICATE) > now()
                GROUP BY 1 LIMIT 20000"""
            try:
                with conn.cursor() as cur:
                    cur.execute("SET statement_timeout = '90s'")
                    cur.execute(sql, (arg,))
                    rows = cur.fetchall()
            except Exception as e:  # noqa: BLE001
                log(f"    crt.sh database {p}: {type(e).__name__}: {str(e)[:150]}")
                continue
            ok.add(p)
            n0 = len(found)
            suffix = p.split("%")[-1]
            for name, day in rows:
                if suffix and not name.endswith(suffix):
                    continue
                _add(found, by, p, name, str(day or ""))
            log(f"    crt.sh database {p}: {len(rows)} names, {len(found) - n0} new domains")
            time.sleep(1)
    return found, by, ok


def all_domains(log=print, budget_s: float = 20 * 60) -> tuple[dict[str, str], dict[str, set[str]], set[str]]:
    """({domain: earliest cert date}, {domain: patterns that found it}, patterns that answered).
    Tries crt.sh's website first; if it's busy (it often is), uses crt.sh's public database instead.
    Stops starting new searches after budget_s seconds."""
    deadline = time.time() + budget_s
    s = requests.Session()
    s.headers["User-Agent"] = "KnewdleNOW-MenuCheck/1.0 (+https://swatsrocks.github.io/knewdle-data/)"
    found: dict[str, str] = {}
    by: dict[str, set[str]] = {}
    ok: set[str] = set()
    misses = 0
    for p in PATTERNS:
        if time.time() > deadline:
            log(f"    crt.sh: out of time, {len(PATTERNS) - PATTERNS.index(p)} searches left for next run")
            break
        if misses >= 3 and not ok:
            log("    crt.sh website isn't answering: switching to its public database")
            db = _search_db(log, max(60.0, deadline - time.time()))
            if db:
                f2, b2, ok2 = db
                for d, day in f2.items():
                    if d not in found or (day and day < found[d]):
                        found[d] = day
                    by.setdefault(d, set()).update(b2.get(d, set()))
                ok |= ok2
            break
        got = search_crtsh(p, s, log)
        if got is None:
            misses += 1
        else:
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
