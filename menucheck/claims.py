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
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from bs4 import BeautifulSoup

from .crawl import Crawler, is_blocked_host
from .places import host_of

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state" / "claims_state.json"
EXTRA = ROOT / "state" / "extra_state.json"
MENU_STATE = ROOT / "state" / "menu_state.json"
CLAIMED = ROOT / "docs" / "claimed.json"
INDEX = ROOT / "docs" / "shops_index.json"
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
            lst.append({"name": name, "address": address, "lat": round(lat, 5), "lon": round(lon, 5)})

    extra = load_json(EXTRA, {})
    for p in extra.get("app_known", []):
        add(p.get("name"), p.get("website"), p.get("address"), p.get("lat"), p.get("lon"))
    for s in load_json(MENU_STATE, {"places": {}}).get("places", {}).values():
        if s.get("kind") in ("shop", "serves"):
            add(s.get("name"), s.get("website"), s.get("address"), s.get("lat"), s.get("lon"))
    for e in extra.get("published", []):
        if e.get("f") != "hall":  # vendors inside halls link to the hall's site, not their own
            add(e.get("n"), e.get("w"), e.get("a"), e.get("la"), e.get("lo"))
    return by_host


def check_host(crawler: Crawler, host: str, claimed: bool) -> dict:
    code = code_for(host)
    home = crawler.fetch_home(host)
    if home is None:
        return {"reached": False}
    if not has_code(home.content, code):
        return {"reached": True, "verified": False}
    lines = update_lines(home.content, whole_page=False)
    page = crawler.fetch(f"{home.url.split('://')[0]}://{host_of(home.url) or host}/knewdle")
    if page is not None and host_of(page.url) == host_of(home.url):
        page_lines = update_lines(page.content, whole_page=True)
        if page_lines:
            lines = page_lines  # a dedicated /knewdle page wins over the homepage section
    return {"reached": True, "verified": True, "updates": parse_updates(lines)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="check every website today, not a seventh")
    ap.add_argument("--dry-run", action="store_true")
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
    found = 0
    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = {pool.submit(check_host, crawler, h, h in claims): h for h in due}
        for f in as_completed(futures):
            h = futures[f]
            try:
                res = f.result()
            except Exception as e:  # noqa: BLE001
                res = {"reached": False, "err": type(e).__name__}
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
    # Closures that have passed drop off by themselves.
    for rec in claims.values():
        c = rec.get("updates", {}).get("closed")
        if c and c["to"] < today().isoformat():
            rec["updates"].pop("closed")
    print(f"done in {time.time() - t0:.0f}s: {len(claims)} verified shops ({found} new today)")

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
    INDEX.write_text(json.dumps({"generated": today().isoformat(), "shops": index},
                                separators=(",", ":"), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
