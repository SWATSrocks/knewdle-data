"""Shops that have closed for good, so the app can hide them (docs/closed.json). Run daily after claims.

Four signals, from strongest to weakest:
  * manual:     closed.txt, places you've confirmed are closed
  * reports:    2+ different phones tapped "Closed?" in the app (from the ratings Worker)
  * foursquare: Foursquare's open data marks the place closed, while another source still lists it
  * website:    the shop's own homepage says "permanently closed" / "closed our doors" (single-location sites only)

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

from .places import PlaceIndex, host_of

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


def reports() -> list[dict]:
    try:
        req = urllib.request.Request(REPORTS_URL, headers={"User-Agent": "knewdle-data"})
        with urllib.request.urlopen(req, timeout=30) as r:
            shops = json.load(r).get("shops", [])
    except Exception as e:  # noqa: BLE001 - the Worker being down mustn't stop the rest
        print(f"  ! couldn't read closed reports: {type(e).__name__}", file=sys.stderr)
        return []
    return [{"k": s["k"], "n": s.get("name") or None, "why": "reports", "count": s.get("n")} for s in shops if s.get("k")]


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
            out.append({"n": c["name"], "la": c["lat"], "lo": c["lon"], "why": "foursquare"})
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


def main() -> int:
    extra = load_json(EXTRA, {})
    listed = listed_places(extra)
    entries = manual() + reports() + foursquare_closed(extra, listed) + website_closed(listed)
    counts: dict[str, int] = {}
    for e in entries:
        counts[e["why"]] = counts.get(e["why"], 0) + 1
    print(f"closed list: {len(entries)} entries {counts}")
    OUT.write_text(json.dumps({"v": 1, "generated": dt.date.today().isoformat(), "shops": entries},
                              separators=(",", ":"), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
