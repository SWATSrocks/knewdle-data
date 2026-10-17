"""Shops that have closed for good, so the app can hide them (docs/closed.json). Run daily after claims.

Hidden ("shops"), strongest first:
  * manual:     closed.txt, places you've confirmed are closed
  * reports:    someone at the shop tapped "Closed?" in the app, or 2+ different phones did (ratings Worker)
  * chain:      a chain's own location directory no longer lists that store (chaindir.py, weekly per state)
  * foursquare: Foursquare's open data marks the place closed, while another source still lists it
  * website:    the shop's own homepage says "permanently closed" / "closed our doors" (single-location sites only)
  * signals:    two different warnings below about the same listing

Shown with a "may be closed" warning ("maybe"):
  * report:     one visitor, not at the shop, said it has closed
  * site-dns / site-parked / site-down: its own website's domain is gone, is a for-sale page, or has been down
                for weeks (from the daily website visits in claims.py)

Failsafe the other way ("open"): a visitor who found it open in person (the app's arrival check or a Passport
stamp) in the last OPEN_TRUST_DAYS days beats every automatic signal above (not closed.txt or people's reports,
which the Worker already weighs against later "open" visits).

Each entry carries whatever the app needs to match it to a listing: a name and map point, a website, and/or the
app's shop key ("website-or-name@lat,lon").
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
import urllib.request
from pathlib import Path

from .places import PlaceIndex, host_of, same_place

ROOT = Path(__file__).resolve().parent.parent
MANUAL = ROOT / "closed.txt"
EXTRA = ROOT / "state" / "extra_state.json"
MENU_STATE = ROOT / "state" / "menu_state.json"
CLAIMS = ROOT / "state" / "claims_state.json"
OUT = ROOT / "docs" / "closed.json"
REPORTS_URL = "https://knewdle-ratings.swatsrocks.workers.dev/closed"


def load_json(p: Path, default):
    return json.loads(p.read_text()) if p.exists() else default


def manual() -> list[dict]:
    out = []
    if not MANUAL.exists():
        return out
    for raw in MANUAL.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.match(r"^(.+?)\s*\|\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)$", line)
        if m:
            out.append({"n": m.group(1).strip(), "la": float(m.group(2)), "lo": float(m.group(3)), "why": "manual"})
        elif re.match(r"^[a-z0-9.\-]+\.[a-z]{2,}$", line.lower()):
            out.append({"h": host_of(line), "why": "manual"})
        else:
            print(f"  ! closed.txt: couldn't read line: {raw!r}", file=sys.stderr)
    return out


def reports_all() -> tuple[list[dict], list[dict], dict[str, str]]:
    """From the app's reports: (hide, warn, {shop key: last date someone found it open in person})."""
    try:
        req = urllib.request.Request(REPORTS_URL + "?all=1", headers={"User-Agent": "knewdle-data"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r)
    except Exception as e:  # noqa: BLE001 - the Worker being down mustn't stop the rest
        print(f"  ! couldn't read closed reports: {type(e).__name__}", file=sys.stderr)
        return [], [], {}
    hide, warn = [], []
    for s in data.get("shops", []):
        if not s.get("k"):
            continue
        e = {"k": s["k"], "n": s.get("name") or None, "count": s.get("n")}
        if (s.get("n") or 0) >= 2 or (s.get("here") or 0) >= 1:
            hide.append({**e, "why": "reports"})
        else:
            warn.append({**e, "why": "report", "t": "A visitor said it has closed"})
    opens = {o["k"]: o["last"] for o in data.get("open", []) if o.get("k") and o.get("last")}
    return hide, warn, opens


def listed_places(extra: dict) -> list[dict]:
    """Everything the app can show that we know about here (to match outside closure signals against)."""
    places = [p for p in extra.get("app_known", [])]
    for s in load_json(MENU_STATE, {"places": {}}).get("places", {}).values():
        if s.get("kind") in ("shop", "serves"):
            places.append(s)
    for e in extra.get("published", []):
        places.append({"name": e["n"], "lat": e["la"], "lon": e["lo"], "website": e.get("w")})
    return [p for p in places if p.get("name") and p.get("lat") is not None]


def foursquare_closed(extra: dict, listed: list[dict]) -> list[dict]:
    """Foursquare-closed places that some other source still shows (no point listing the rest)."""
    closed = extra.get("foursquare", {}).get("closed", [])
    if not closed:
        return []
    shown = PlaceIndex(listed)
    # Foursquare sometimes keeps an old closed duplicate of a shop that's still open: if it ALSO lists the
    # same place as open, trust that and don't hide anything.
    still_open = PlaceIndex(extra.get("foursquare", {}).get("places", []))
    out = []
    for c in closed:
        p = {"name": c["name"], "lat": c["lat"], "lon": c["lon"], "website": c.get("website")}
        if shown.has(p) and not still_open.has(p):
            out.append({"n": c["name"], "la": c["lat"], "lo": c["lon"], "w": c.get("website"), "why": "foursquare"})
    return out


def website_closed(listed: list[dict]) -> list[dict]:
    sites = load_json(CLAIMS, {}).get("closed_sites", {})
    if not sites:
        return []
    per_host: dict[str, int] = {}
    for p in listed:
        h = host_of(p.get("website") or "")
        if h:
            per_host[h] = per_host.get(h, 0) + 1
    # A chain's site saying "our Tempe location has permanently closed" mustn't hide every location.
    return [{"h": h, "why": "website"} for h in sorted(sites) if per_host.get(h, 0) == 1]


def chain_closed(listed: list[dict]) -> list[dict]:
    from .chaindir import check
    from .crawl import Crawler
    info = load_json(ROOT / "config.json", {}).get("bot_info_url", "https://github.com/")
    try:
        return check(Crawler(info), listed)
    except Exception as e:  # noqa: BLE001 - a chain site being down mustn't stop the rest
        print(f"  ! chain directories failed: {type(e).__name__}: {e}", file=sys.stderr)
        return []


# How long a website must stay broken before it counts, by kind of trouble.
SITE_TROUBLE = {
    "dns":    (2, 6,  "Its website's domain no longer exists"),
    "parked": (2, 6,  "Its website is now a for-sale/parked page"),
    "down":   (3, 14, "Its website has been down for weeks"),
}
OPEN_TRUST_DAYS = 120   # found open in person this recently beats any automatic "closed" signal


def site_trouble(listed: list[dict], per_host: dict[str, int]) -> list[dict]:
    out = []
    for h, rec in sorted(load_json(CLAIMS, {}).get("site_health", {}).items()):
        need = SITE_TROUBLE.get(rec.get("bad"))
        if not need or per_host.get(h, 0) == 0 or per_host[h] > 3:
            continue                     # unknown kind, not a listing we show, or a chain's shared site
        checks, days, text = need
        span = (dt.date.fromisoformat(rec["last"]) - dt.date.fromisoformat(rec["first"])).days
        if rec.get("n", 0) >= checks and span >= days:
            out.append({"h": h, "why": "site-" + rec["bad"], "t": text, "since": rec["first"]})
    return out


def shop_key(name: str, website: str | None, lat: float, lon: float) -> str:
    """The app's Ratings.keyOf(): website host (or tidy name) @ spot to 2 decimals."""
    ident = host_of(website or "") or re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:60]
    return f"{ident[:80]}@{lat:.2f},{lon:.2f}"


class Listed:
    """The places we can show, findable by host, key and spot."""

    def __init__(self, places: list[dict]):
        self.places = places
        self.by_host: dict[str, list[dict]] = {}
        self.by_key: dict[str, list[dict]] = {}
        self.grid: dict[tuple[int, int], list[dict]] = {}
        for p in places:
            h = host_of(p.get("website") or "")
            if h:
                self.by_host.setdefault(h, []).append(p)
            self.by_key.setdefault(shop_key(p["name"], p.get("website"), p["lat"], p["lon"]), []).append(p)
            self.grid.setdefault((int(p["lat"] * 100), int(p["lon"] * 100)), []).append(p)

    def matching(self, e: dict) -> list[dict]:
        """Listings an entry refers to (by its key, its website, or name + spot)."""
        if e.get("k"):
            return self.by_key.get(e["k"], [])
        if e.get("h"):
            return self.by_host.get(e["h"], [])
        if e.get("la") is None:
            return []
        q = {"name": e.get("n") or "", "lat": e["la"], "lon": e["lo"], "website": e.get("w")}
        ci, cj = int(e["la"] * 100), int(e["lo"] * 100)
        return [p for i in (ci - 1, ci, ci + 1) for j in (cj - 1, cj, cj + 1)
                for p in self.grid.get((i, j), ()) if same_place(q, p)]


def entry_keys(e: dict, listed: Listed) -> set[str]:
    keys = {e["k"]} if e.get("k") else set()
    if e.get("la") is not None and e.get("n"):
        keys.add(shop_key(e["n"], e.get("w"), e["la"], e["lo"]))
    for p in listed.matching(e):
        keys.add(shop_key(p["name"], p.get("website"), p["lat"], p["lon"]))
    return keys


def build(extra: dict, manual_entries: list[dict], reported: tuple, automatic: list[dict],
          site_entries_fn=None, today: dt.date | None = None) -> dict:
    """The published closed.json: "shops" (hidden), "maybe" (shown with a warning), "open" (found open)."""
    today = today or dt.date.today()
    listed = Listed(listed_places(extra))
    per_host = {h: len(ps) for h, ps in listed.by_host.items()}
    hide_reports, warn_reports, opens = reported
    recent_open = {k: d for k, d in opens.items() if (today - dt.date.fromisoformat(d)).days <= OPEN_TRUST_DAYS}
    warn = warn_reports + (site_entries_fn(listed.places, per_host) if site_entries_fn else [])

    def found_open(e: dict) -> bool:
        return bool(entry_keys(e, listed) & recent_open.keys())

    hide = list(manual_entries) + hide_reports   # people's word: the Worker already drops ones found open since
    for e in automatic:
        if found_open(e):
            print(f"  ✓ kept: {e.get('n') or e.get('h')} ({e['why']}) - a visitor found it open in person")
        else:
            hide.append(e)
    kept_warn = []
    for e in warn:
        if e["why"] != "report" and found_open(e):
            continue
        kept_warn.append(e)

    # Two different kinds of warning about the same listing: hide it.
    reasons: dict[int, set[str]] = {}
    for e in kept_warn:
        kind = "site" if e["why"].startswith("site-") else e["why"]
        for p in listed.matching(e):
            reasons.setdefault(id(p), set()).add(kind)
    by_id = {id(p): p for p in listed.places}
    for pid, kinds in reasons.items():
        if len(kinds) >= 2:
            p = by_id[pid]
            hide.append({"n": p["name"], "la": p["lat"], "lo": p["lon"], "w": p.get("website"), "why": "signals"})
    return {"v": 2, "generated": today.isoformat(), "shops": hide, "maybe": kept_warn,
            "open": [{"k": k, "last": d} for k, d in sorted(recent_open.items())]}


def main() -> int:
    extra = load_json(EXTRA, {})
    listed = listed_places(extra)
    automatic = chain_closed(listed) + foursquare_closed(extra, listed) + website_closed(listed)
    out = build(extra, manual(), reports_all(), automatic, site_trouble)
    for part in ("shops", "maybe"):
        counts: dict[str, int] = {}
        for e in out[part]:
            counts[e["why"]] = counts.get(e["why"], 0) + 1
        print(f"{'hidden' if part == 'shops' else 'may be closed'}: {len(out[part])} {counts}")
    print(f"found open in person (last {OPEN_TRUST_DAYS} days): {len(out['open'])}")
    OUT.write_text(json.dumps(out, separators=(",", ":"), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
