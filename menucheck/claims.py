"""Claimed shops: owners verify by putting their code on their own website, then post updates there.

    python -m menucheck.claims [--all] [--dry-run]

  * Each website has a code: "kn-" + the first 6 characters of sha256("knewdle:" + website host). The claim page
    (docs/shops/) computes the same code in the browser. A code only counts on its own website, so nobody can
    claim someone else's shop.
  * Claimed shops are checked daily; every other ramen website once a week (a seventh of them each day).
  * Updates are plain lines in a "Knewdle NOW updates" section of the homepage, or on a /knewdle page:
        Special: Spicy miso tonkotsu, $15
        Closed: Nov 27-28
        Hours: Tue-Sun 11:30am-9pm
        Note: Late-night ramen Fridays!
  * Publishes docs/claimed.json (read by the app) and docs/shops_index.json (the claim page's search list).
  * The same visit reads the shop's opening hours from its own site (structured data or an "Hours" section,
    see hours.py) and publishes docs/hours.json for single-location websites.
  * The same visit also notes each website's health (state "site_health"): its domain no longer exists, it's
    become a for-sale/parked page, or it's been down for weeks. closed.py turns that into "may be closed".
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from . import hours as hours_mod
from .crawl import Crawler, is_blocked_host
from .places import host_of

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state" / "claims_state.json"
EXTRA = ROOT / "state" / "extra_state.json"
MENU_STATE = ROOT / "state" / "menu_state.json"
CLAIMED = ROOT / "docs" / "claimed.json"
INDEX = ROOT / "docs" / "shops_index.json"
HOURS = ROOT / "docs" / "hours.json"
HOURS_KEEP_DAYS = 60       # hours found before stay published this long when a later visit can't read them
CONFIG = ROOT / "config.json"
OPTOUT = ROOT / "optout.txt"

LIMITS = {"special": 120, "note": 120, "hours": 160}
# Shared ordering/website platforms: one address for many restaurants, so a code there can't prove ownership.
SHARED_HOSTS = ("skytab.com", "popmenu.com", "menufy.com", "order.online", "orderonline", "res-menu.com",
                "getbento.com", "bentobox", "square.site", "squareup.com", "wixsite.com", "godaddysites.com",
                "business.site", "linktr.ee", "facebook.com", "instagram.com", "yelp.com", "google.com")
MISSES_TO_UNCLAIM = 2      # code gone on this many daily checks in a row -> badge removed
URL = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
LINE = re.compile(r"^\s*(special|closed|hours|note)\s*[:\-–]\s*(.+?)\s*$", re.IGNORECASE)
# A shop's own homepage saying it has closed for good (not "closed Mondays" or "temporarily closed").
CLOSED_FOR_GOOD = re.compile(
    r"permanently closed|closed permanently|closed for good|(?:have|has) (?:now |officially |permanently )?closed "
    r"(?:our|its) doors|closing (?:our|its) doors (?:for good|permanently)|we(?:'ve| have) (?:officially )?closed\b(?! on)"
    r"(?! for (?:the|a|today|tonight|lunch|dinner|holiday|vacation|private|renovation))", re.IGNORECASE)
TEMPORARY = re.compile(r"temporar|reopen|re-open|renovat|vacation|holiday|for the day|tonight|today", re.IGNORECASE)


# A website that's no longer the restaurant's: for sale, parked, expired or suspended.
SITE_GONE = re.compile(r"domain (?:is |may be )?for sale|buy this domain|this domain (?:name )?(?:is|may be) for sale|"
                       r"parked (?:free|domain|by)|domain (?:has )?expired|this domain has expired|renew (?:this|your) domain|"
                       r"account (?:has been )?suspended|website (?:has been )?suspended|hugedomains|dan\.com|"
                       r"sedo(?:parking)?\b|afternic", re.IGNORECASE)


_DNS_WORKS: bool | None = None


def dns_works() -> bool:
    """Sanity check before believing "domain gone": well-known names must resolve here."""
    global _DNS_WORKS
    if _DNS_WORKS is None:
        _DNS_WORKS = all(_resolves(h) for h in ("github.com", "wikipedia.org"))
    return _DNS_WORKS


def _resolves(h: str) -> bool:
    try:
        socket.getaddrinfo(h, 443)
        return True
    except OSError:
        return False


def domain_gone(host: str) -> bool | None:
    """True if neither host nor www.host exists in DNS any more; None if DNS couldn't tell (try again later)."""
    if not dns_works():
        return None
    gone = 0
    for h in (host, "www." + host.removeprefix("www.")):
        try:
            socket.getaddrinfo(h, 443)
            return False
        except socket.gaierror as e:
            if e.errno in (socket.EAI_NONAME, getattr(socket, "EAI_NODATA", -5)):
                gone += 1
            else:
                return None
        except OSError:
            return None
    return gone == 2


def site_health(crawler: Crawler, host: str, home) -> str | None:
    """'ok', 'dns' (domain gone), 'parked' (for sale etc.), 'down' (no answer), or None when we can't tell."""
    if home is not None:
        text = BeautifulSoup(home.content, "html.parser").get_text(" ", strip=True)[:4000]
        return "parked" if SITE_GONE.search(text) else "ok"
    gone = domain_gone(host)
    if gone:
        return "dns"
    if gone is None:
        return None
    u = f"https://{host}/"
    if crawler.robots_reachable(u) and not crawler.allowed(u):
        return None                      # robots.txt says no: that's not a sign of anything
    return "down"


def says_closed_for_good(html: bytes | str) -> bool:
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)[:6000]
    for m in CLOSED_FOR_GOOD.finditer(text):
        around = text[max(0, m.start() - 120): m.end() + 120]
        if not TEMPORARY.search(around):
            return True
    return False


UPDATES_HEADING = re.compile(r"knewdle\s*now\s*updates", re.IGNORECASE)
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


def today() -> dt.date:
    return dt.date.today()


def code_for(host: str) -> str:
    h = host.lower().removeprefix("www.")
    return "kn-" + hashlib.sha256(f"knewdle:{h}".encode()).hexdigest()[:6]


def has_code(html: bytes | str, code: str) -> bool:
    """The code as visible text ("Knewdle NOW verified: kn-xxxxxx") or in a <meta name="knewdle-verify"> tag."""
    soup = BeautifulSoup(html, "html.parser")
    for m in soup.find_all("meta", attrs={"name": re.compile(r"^knewdle-verify$", re.I)}):
        if (m.get("content") or "").strip().lower() == code:
            return True
    return code in soup.get_text(" ", strip=True).lower()


def _clean(s: str, limit: int) -> str:
    s = URL.sub("", s)
    s = re.sub(r"[\x00-\x1f\x7f<>]", " ", s)
    s = " ".join(s.split()).strip(" -–—·•")
    return s[:limit].rstrip()


# ---------- reading updates ----------

def update_lines(html: bytes | str, whole_page: bool) -> list[tuple[str, str]]:
    """(kind, text) lines from a "Knewdle NOW updates" section (or the whole page, for a /knewdle page)."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    lines = [l.strip() for l in soup.get_text("\n", strip=True).split("\n") if l.strip()]
    if not whole_page:
        start = next((i for i, l in enumerate(lines) if UPDATES_HEADING.search(l)), None)
        if start is None:
            return []
        lines = lines[start + 1: start + 15]
    out, started = [], False
    for l in lines:
        m = LINE.match(l)
        if m:
            out.append((m.group(1).lower(), m.group(2)))
            started = True
        elif started and not whole_page:
            break  # the section ended
    return out[:8]


def _date(month: str | None, day: str, base: dt.date, num_month: str | None = None, year: str | None = None):
    mon = int(num_month) if num_month else MONTHS.get((month or "")[:3].lower())
    if not mon:
        return None
    try:
        d = dt.date(int(year) if year else base.year, mon, int(day))
    except ValueError:
        return None
    if not year and d < base - dt.timedelta(days=60):
        d = d.replace(year=d.year + 1)  # "Jan 2" written in December means next year
    return d


_MON = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_RANGE_TEXT = re.compile(rf"{_MON}\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s*(\d{{4}}))?"
                         rf"(?:\s*(?:-|–|—|to|through|thru)\s*(?:{_MON}\s+)?(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s*(\d{{4}}))?)?",
                         re.IGNORECASE)
_RANGE_NUM = re.compile(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?(?:\s*(?:-|–|—|to|through)\s*(?:(\d{1,2})/)?(\d{1,2})(?:/(\d{2,4}))?)?")


def closed_range(text: str, base: dt.date | None = None) -> tuple[dt.date, dt.date] | None:
    """'Nov 27-28', 'Nov 27 - Dec 2', 'December 24', '11/27-11/28', '11/27' -> (first day, last day)."""
    base = base or today()
    m = _RANGE_TEXT.search(text)
    if m:
        start = _date(m.group(1), m.group(2), base, year=m.group(3))
        if not start:
            return None
        end = start
        if m.group(5):
            end = _date(m.group(4) or m.group(1), m.group(5), start, year=m.group(6) or str(start.year))
            if end and end < start:
                end = end.replace(year=end.year + 1)
        return (start, end) if end else None
    m = _RANGE_NUM.search(text)
    if m:
        yr = lambda y: None if not y else (y if len(y) == 4 else "20" + y)  # noqa: E731
        start = _date(None, m.group(2), base, num_month=m.group(1), year=yr(m.group(3)))
        if not start:
            return None
        end = start
        if m.group(5):
            end = _date(None, m.group(5), start, num_month=m.group(4) or m.group(1),
                        year=yr(m.group(6)) or str(start.year))
            if end and end < start:
                end = end.replace(year=end.year + 1)
        return (start, end) if end else None
    return None


def parse_updates(lines: list[tuple[str, str]], base: dt.date | None = None) -> dict:
    base = base or today()
    out: dict = {}
    for kind, text in lines:
        if kind == "closed":
            r = closed_range(text, base)
            if r and r[1] >= base and "closed" not in out:
                out["closed"] = {"from": r[0].isoformat(), "to": r[1].isoformat(), "text": _clean(text, 60)}
        elif kind in LIMITS and kind not in out:
            t = _clean(text, LIMITS[kind])
            if t:
                out[kind] = t
    return out


# ---------- the daily run ----------

def load_json(p: Path, default):
    return json.loads(p.read_text()) if p.exists() else default


def listings() -> dict[str, list[dict]]:
    """Every ramen listing with a website, grouped by website host: {host: [{name, address, lat, lon}]}."""
    by_host: dict[str, list[dict]] = {}

    def add(name, website, address, lat, lon):
        h = host_of(website or "")
        if not h or not name or lat is None or is_blocked_host("https://" + h) or any(x in h for x in SHARED_HOSTS):
            return
        lst = by_host.setdefault(h, [])
        if not any(x["name"] == name and abs(x["lat"] - lat) < 0.002 for x in lst):
            lst.append({"name": name, "address": address, "lat": round(lat, 5), "lon": round(lon, 5),
                        "website": (website or "").strip()})

    extra = load_json(EXTRA, {})
    for p in extra.get("app_known", []):
        add(p.get("name"), p.get("website"), p.get("address"), p.get("lat"), p.get("lon"))
    for s in load_json(MENU_STATE, {"places": {}}).get("places", {}).values():
        if s.get("kind") in ("shop", "serves"):
            add(s.get("name"), s.get("website"), s.get("address"), s.get("lat"), s.get("lon"))
    osm = ROOT / "docs" / "osm_ramen.json"
    for e in (load_json(osm, {}).get("elements", []) if osm.exists() else []):
        t = e.get("tags", {})
        street = " ".join(x for x in (t.get("addr:housenumber"), t.get("addr:street")) if x)
        add(t.get("name") or t.get("name:en"), t.get("website") or t.get("contact:website"),
            ", ".join(x for x in (street, t.get("addr:city"), t.get("addr:state")) if x) or None, e.get("lat"), e.get("lon"))
    for e in extra.get("published", []):
        if e.get("f") != "hall":  # vendors inside halls link to the hall's site, not their own
            add(e.get("n"), e.get("w"), e.get("a"), e.get("la"), e.get("lo"))
    return by_host


def find_hours(crawler: Crawler, home) -> tuple[list | None, str | None]:
    """The shop's weekly hours from its homepage, or from up to 2 of its own hours/contact/location pages."""
    week, how = hours_mod.parse(home.content)
    if week:
        return week, how
    base = host_of(home.url)
    soup = BeautifulSoup(home.content, "html.parser")
    seen, tries = set(), []
    for a in soup.find_all("a", href=True):
        href = urljoin(home.url, a["href"]).split("#")[0]
        if not href.startswith("http") or host_of(href) != base or href.rstrip("/") == home.url.rstrip("/"):
            continue
        label = (a.get_text(" ", strip=True) or "") + " " + urlparse(href).path
        if hours_mod.HOURS_LINK.search(label) and href not in seen and not re.search(r"\.(pdf|jpe?g|png)$", href, re.I):
            seen.add(href)
            # "hours" pages first, then location/contact, then about
            tries.append((0 if re.search(r"hour", label, re.I) else 1 if re.search(r"locat|visit|find|contact", label, re.I) else 2, href))
    for _, href in sorted(tries)[:3]:
        page = crawler.fetch(href)
        if page is not None and host_of(page.url) == base:
            week, how = hours_mod.parse(page.content)
            if week:
                return week, how
    return None, None


def page_key(url: str) -> str | None:
    """"jinyaramenbar.com/locations/charlotte" for a chain's own page about one store; None for a homepage.
    The app builds the same key from a listing's website."""
    u = (url or "").strip()
    if not u:
        return None
    if not u.lower().startswith("http"):
        u = "https://" + u
    p = urlparse(u)
    host = (p.hostname or "").lower().removeprefix("www.")
    path = re.sub(r"/+$", "", p.path or "").lower()
    if not host or not path or re.search(r"\.(pdf|jpe?g|png)$", path):
        return None
    return host + path


def check_chain_pages(crawler: Crawler, host: str, urls: list[str]) -> dict:
    """Each store page of a multi-location website: {page key: {"w": week, "how": how}} (None where unreadable).
    One page at a time per website, with the site's own crawl delay."""
    out = {}
    for u in urls:
        if time.time() > CHAIN_DEADLINE[0]:
            break
        key = page_key(u)
        r = crawler.fetch(u if u.lower().startswith("http") else "https://" + u)
        if r is not None and page_key(r.url) and host_of(r.url) == host:   # not bounced to the homepage
            week, how = hours_mod.parse(r.content)
            out[key] = {"w": week, "how": how} if week else None
        time.sleep(max(1.0, crawler.crawl_delay(u if u.startswith("http") else "https://" + u)))
    return out


def owner_hours(text: str | None):
    """An owner's posted "Hours: Tue-Sun 11:30am-9pm" as a week, when it reads cleanly."""
    if not text:
        return None
    w = hours_mod.from_text("Hours: " + text) or hours_mod.from_text_any("Hours: " + text)
    return w if w and hours_mod.valid(w) else None


CHAIN_DEADLINE = [float("inf")]
DEADLINE = [float("inf")]  # time.time() after which remaining sites are left for the next run


def check_host(crawler: Crawler, host: str, claimed: bool, want_hours: bool = False) -> dict:
    if time.time() > DEADLINE[0]:
        return {"skipped": True}
    code = code_for(host)
    home = crawler.fetch_home(host)
    health = site_health(crawler, host, home)
    if home is None:
        return {"reached": False, "health": health}
    closed = says_closed_for_good(home.content)
    hours = None
    if want_hours and not closed:
        try:
            week, how = find_hours(crawler, home)
            hours = {"w": week, "how": how} if week else None
        except Exception:  # noqa: BLE001 - hours are a bonus; never lose the rest of the check over them
            hours = None
    if not has_code(home.content, code):
        return {"reached": True, "verified": False, "closed": closed, "health": health, "hours": hours}
    lines = update_lines(home.content, whole_page=False)
    page = crawler.fetch(f"{home.url.split('://')[0]}://{host_of(home.url) or host}/knewdle")
    if page is not None and host_of(page.url) == host_of(home.url):
        page_lines = update_lines(page.content, whole_page=True)
        if page_lines:
            lines = page_lines  # a dedicated /knewdle page wins over the homepage section
    return {"reached": True, "verified": True, "updates": parse_updates(lines), "closed": closed, "health": health,
            "hours": hours}


def note_hours(rec_all: dict, host: str, res: dict, day: str | None = None) -> None:
    """state["hours"][host] = {"w": week, "how": how, "found": date}; kept HOURS_KEEP_DAYS if later visits miss."""
    day = day or today().isoformat()
    if res.get("hours"):
        rec_all[host] = {"w": res["hours"]["w"], "how": res["hours"]["how"], "found": day}
    elif res.get("reached") and res.get("closed"):
        rec_all.pop(host, None)
    old = rec_all.get(host)
    if old and (dt.date.fromisoformat(day) - dt.date.fromisoformat(old["found"])).days > HOURS_KEEP_DAYS:
        rec_all.pop(host, None)


def note_health(health: dict, host: str, status: str | None, day: str | None = None) -> None:
    """Keeps a running record of a website's trouble: {"bad": kind, "first": date, "last": date, "n": checks}."""
    day = day or today().isoformat()
    if not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", host or ""):
        return                           # not a clean host name: don't judge it
    if status is None:
        return                           # couldn't tell this time: keep what we had
    if status == "ok":
        health.pop(host, None)
        return
    rec = health.get(host)
    if rec is None or rec.get("bad") != status:
        health[host] = {"bad": status, "first": day, "last": day, "n": 1}
    elif rec["last"] != day:
        rec["last"] = day
        rec["n"] += 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="check every website today, not a seventh")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--minutes", type=float, default=38, help="stop starting new sites after this long, and save")
    args = ap.parse_args()

    config = load_json(CONFIG, {})
    crawler = Crawler(config.get("bot_info_url", "https://github.com/"))
    state = load_json(STATE, {"claims": {}})
    claims: dict = state.setdefault("claims", {})
    optout = {l.strip().lower().removeprefix("www.") for l in (OPTOUT.read_text().splitlines() if OPTOUT.exists() else [])
              if l.strip() and not l.startswith("#")}
    by_host = listings()
    day = today().toordinal() % 7
    due = [h for h in by_host if h in claims or args.all
           or int(hashlib.sha256(h.encode()).hexdigest(), 16) % 7 == day]
    due = [h for h in due if not any(h == o or h.endswith("." + o) for o in optout)]
    print(f"{len(by_host)} ramen websites; checking {len(due)} today ({sum(1 for h in due if h in claims)} claimed)")

    t0 = time.time()
    DEADLINE[0] = t0 + args.minutes * 60
    found = 0
    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = {pool.submit(check_host, crawler, h, h in claims, len(by_host[h]) == 1): h for h in due}
        for f in as_completed(futures):
            h = futures[f]
            try:
                res = f.result()
            except Exception as e:  # noqa: BLE001
                res = {"reached": False, "err": type(e).__name__}
            if res.get("skipped"):
                continue
            health_rec = state.setdefault("site_health", {})
            for junk in [k for k in health_rec if k != k.strip()]:
                health_rec.pop(junk)
            note_health(health_rec, h, res.get("health"))
            note_hours(state.setdefault("hours", {}), h, res)
            closed_sites: dict = state.setdefault("closed_sites", {})
            if res.get("reached"):
                if res.get("closed"):
                    if h not in closed_sites:
                        print(f"  ⚠ website says closed for good: {h}")
                    closed_sites[h] = today().isoformat()
                else:
                    closed_sites.pop(h, None)
            rec = claims.get(h)
            if res.get("verified"):
                if rec is None:
                    rec = claims[h] = {"since": today().isoformat()}
                    print(f"  ✓ newly verified: {h}")
                    found += 1
                rec["checked"] = today().isoformat()
                rec["misses"] = 0
                upd = res.get("updates", {})
                if upd != rec.get("updates"):
                    rec["updates"] = upd
                    rec["updated"] = today().isoformat()
            elif rec is not None and res.get("reached"):
                rec["misses"] = rec.get("misses", 0) + 1
                if rec["misses"] >= MISSES_TO_UNCLAIM:
                    print(f"  ✗ code removed, unverified: {h}")
                    del claims[h]
    # Chains: hours from each store's own page on the chain's website (the page the listing links to).
    pages_rec: dict = state.setdefault("page_hours", {})
    chain_urls: dict[str, dict[str, str]] = {}
    for h, ps in by_host.items():
        if len(ps) > 1 and not any(h == o or h.endswith("." + o) for o in optout):
            for p in ps:
                k = page_key(p.get("website"))
                if k:
                    chain_urls.setdefault(h, {})[k] = p["website"]
    due_chains = [h for h in chain_urls if args.all or int(hashlib.sha256(h.encode()).hexdigest(), 16) % 7 == day]
    print(f"chains: {len(chain_urls)} websites with store pages; checking {len(due_chains)} today "
          f"({sum(len(chain_urls[h]) for h in due_chains)} pages)")
    CHAIN_DEADLINE[0] = time.time() + 15 * 60
    with ThreadPoolExecutor(max_workers=12) as pool:
        futs = {pool.submit(check_chain_pages, crawler, h, list(chain_urls[h].values())): h for h in due_chains}
        for f in as_completed(futs):
            try:
                res = f.result()
            except Exception:  # noqa: BLE001
                continue
            for k, v in res.items():
                note_hours(pages_rec, k, {"reached": True, "hours": v})
    for k in list(pages_rec):
        note_hours(pages_rec, k, {})
    # Closures that have passed drop off by themselves.
    for rec in claims.values():
        c = rec.get("updates", {}).get("closed")
        if c and c["to"] < today().isoformat():
            rec["updates"].pop("closed")
    print(f"done in {time.time() - t0:.0f}s: {len(claims)} verified shops ({found} new today)")

    hours_rec = state.setdefault("hours", {})
    for h in list(hours_rec):
        note_hours(hours_rec, h, {})          # age out old finds
    hours_pub = {h: r["w"] for h, r in sorted(hours_rec.items()) if len(by_host.get(h, [])) == 1}
    for h, rec in claims.items():          # an owner's posted hours win
        w = owner_hours(rec.get("updates", {}).get("hours"))
        if w:
            hours_pub[h] = w
    live_pages = {page_key(p.get("website")) for ps in by_host.values() if len(ps) > 1 for p in ps}
    pages_pub = {k: r["w"] for k, r in sorted(pages_rec.items()) if k in live_pages}
    print(f"hours: {len(pages_pub)} chain store pages with hours")
    print(f"hours: {len(hours_pub)} single-location websites with hours "
          f"({sum(1 for h in by_host if len(by_host[h]) == 1)} single-location websites in all)")
    published = [{"h": h, "since": r["since"], "u": r.get("updated"), **r.get("updates", {})}
                 for h, r in sorted(claims.items())]
    index = sorted(({"n": p["name"], "a": p.get("address"), "h": h} for h, ps in by_host.items() for p in ps),
                   key=lambda x: x["n"].lower())
    if args.dry_run:
        print(json.dumps(published[:5], indent=1))
        return 0
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=1, ensure_ascii=False))
    CLAIMED.write_text(json.dumps({"v": 1, "generated": today().isoformat(), "shops": published},
                                  separators=(",", ":"), ensure_ascii=False))
    HOURS.write_text(json.dumps({"v": 1, "generated": today().isoformat(),
                                 "about": "Opening hours as shown on each shop's own website, read weekly by Knewdle NOW.",
                                 "license": "Open Database License (ODbL 1.0); includes shop websites listed in OpenStreetMap "
                                            "(© OpenStreetMap contributors) and Overture Maps (CDLA-Permissive-2.0).",
                                 "shops": hours_pub, "pages": pages_pub},
                                separators=(",", ":")))
    INDEX.write_text(json.dumps({"generated": today().isoformat(), "shops": index},
                                separators=(",", ":"), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
