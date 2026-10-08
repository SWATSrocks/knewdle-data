"""Monthly menu check for Knewdle NOW.

    python -m menucheck.run [--max-sites 6000] [--workers 24] [--candidates-limit N] [--dry-run]

1. Gets candidate restaurants from Overture (see candidates.py).
2. Re-checks the ones that are new or due (ramen places monthly, others every few months),
   reading only each restaurant's own website, politely (see crawl.py).
3. Publishes docs/menu_ramen.json: places whose own menu shows ramen. The app downloads this file.

State lives in state/menu_state.json so each run only does the work that's due.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

from .classify import RULES_VERSION, classify
from .crawl import Crawler, is_blocked_host

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state" / "menu_state.json"
OUT = ROOT / "docs" / "menu_ramen.json"
STATS = ROOT / "docs" / "stats.json"
CONFIG = ROOT / "config.json"
OPTOUT = ROOT / "optout.txt"

# Days before a place is checked again, by last result.
RECHECK_DAYS = {"shop": 30, "serves": 30, "none": 90, "error": 30, "robots": 120, "platform": 180}


def today() -> str:
    return dt.date.today().isoformat()


def days_since(d: str | None) -> int:
    if not d:
        return 10_000
    return (dt.date.today() - dt.date.fromisoformat(d)).days


def load_json(p: Path, default):
    return json.loads(p.read_text()) if p.exists() else default


def host_of(url: str) -> str:
    u = url if url.startswith("http") else "https://" + url
    return (urlparse(u).hostname or "").lower().removeprefix("www.")


TRACKING = ("rwg_token", "utm_", "fbclid", "gclid", "msclkid", "mc_", "_ga", "y_source")


def clean_url(url: str | None) -> str | None:
    """Drops tracking codes (e.g. Google's rwg_token) from links before they're published."""
    if not url:
        return url
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
    p = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not k.lower().startswith(TRACKING)]
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(q), p.fragment))


def site_key(url: str) -> str:
    u = url if url.startswith("http") else "https://" + url
    p = urlparse(u)
    host = (p.netloc or "").lower().removeprefix("www.")
    return host + (p.path or "/").rstrip("/").lower()


def load_optout() -> set[str]:
    if not OPTOUT.exists():
        return set()
    return {l.strip().lower().removeprefix("www.") for l in OPTOUT.read_text().splitlines()
            if l.strip() and not l.startswith("#")}


def check_group(crawler: Crawler, places: list[dict]) -> list[tuple[dict, dict]]:
    """Places sharing one website (e.g. a small chain) are checked once, sequentially."""
    site = crawler.read_site(places[0]["website"])
    if not site.ok:
        status = "robots" if site.blocked_by_robots else ("platform" if site.error == "third-party platform" else "error")
        rec = {"kind": status, "dishes": 0, "menu": None, "err": site.error}
    else:
        best = None
        for page in site.pages:
            v = classify(page.text)
            if best is None or v.dishes > best[0].dishes:
                best = (v, page.url)
        # A menu split across pages: also judge everything together and keep the stronger answer.
        combined = classify("\n".join(p.text for p in site.pages))
        v, url = best
        if combined.dishes > v.dishes:
            v, url = combined, (url if v.dishes else site.pages[-1].url)
        rec = {"kind": v.kind, "dishes": v.dishes, "broths": v.broths, "items": v.menu_items,
               "menu": url if v.kind != "none" else None, "err": None,
               "examples": v.examples, "reason": v.reason, "rv": RULES_VERSION}
    return [(p, rec) for p in places]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-sites", type=int, default=25000, help="max websites to read this run")
    ap.add_argument("--workers", type=int, default=24)
    ap.add_argument("--candidates-limit", type=int, default=None, help="testing: only take N candidates")
    ap.add_argument("--release", default=None, help="Overture release (default: latest)")
    ap.add_argument("--focus", default="", help="'lat,lon': check places near here first (e.g. to test one city)")
    ap.add_argument("--dry-run", action="store_true", help="don't write files")
    args = ap.parse_args()

    config = load_json(CONFIG, {})
    info_url = config.get("bot_info_url", "https://github.com/")
    state: dict = load_json(STATE, {"places": {}})
    places_state: dict = state.setdefault("places", {})
    optout = load_optout()

    from .candidates import fetch_candidates  # imported here so tests don't need duckdb
    t0 = time.time()
    release, candidates = fetch_candidates(args.release, args.candidates_limit)
    print(f"Overture {release}: {len(candidates)} candidate places with websites ({time.time() - t0:.0f}s)")

    current = {c["id"]: c for c in candidates}
    # Who's due? New places first, then the oldest checks.
    due = []
    for c in candidates:
        h = host_of(c["website"])
        if any(h == o or h.endswith("." + o) for o in optout):
            continue
        prev = places_state.get(c["id"])
        if prev is not None and prev.get("kind") in ("shop", "serves") and is_blocked_host(c["website"]):
            prev = None  # its website is now on the skip list: re-evaluate (it will be dropped)
        if prev is None or prev.get("website") != c["website"]:
            due.append((0, c))
        elif prev.get("kind") in ("none", "shop") and prev.get("rv", 1) < RULES_VERSION:
            due.append((1, c))  # judged under older, stricter rules: look again
        elif days_since(prev.get("checked")) >= RECHECK_DAYS.get(prev.get("kind", "error"), 30):
            due.append((2, c))
    due.sort(key=lambda x: (x[0], places_state.get(x[1]["id"], {}).get("checked") or ""))
    if args.focus.strip():
        flat, flon = (float(v) for v in args.focus.split(","))

        def miles(c: dict) -> float:
            dlat = (c["lat"] - flat) * 69.0
            dlon = (c["lon"] - flon) * 69.0 * math.cos(math.radians(flat))
            return math.hypot(dlat, dlon)

        due.sort(key=lambda x: miles(x[1]))  # nearest first, everything else after
        print(f"Focus {flat},{flon}: {sum(1 for _, c in due if miles(c) < 60)} due within 60 miles")

    # Places listing the exact same website (e.g. one small chain's site) are read once.
    groups: dict[str, list[dict]] = defaultdict(list)
    for _, c in due:
        key = site_key(c["website"])
        if key in groups or len(groups) < args.max_sites:
            groups[key].append(c)
    print(f"Due: {len(due)} places on {len(groups)} websites this run")

    crawler = Crawler(info_url)
    done = 0
    found = defaultdict(int)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(check_group, crawler, ps) for ps in groups.values()]
        for f in as_completed(futures):
            try:
                results = f.result()
            except Exception as e:  # noqa: BLE001 - one bad site must not stop the run
                print("  ! site failed:", e, file=sys.stderr)
                continue
            for place, rec in results:
                found[rec["kind"]] += 1
                examples = rec.get("examples")
                places_state[place["id"]] = {
                    **{k: place[k] for k in ("name", "lat", "lon", "address", "website")},
                    **{k: v for k, v in rec.items() if k not in ("examples", "reason")},
                    "checked": today(),
                }
                if rec["kind"] in ("shop", "serves"):
                    print(f"  🍜 {place['name']} ({rec['kind']}: {rec.get('reason')}) e.g. {examples}")
            done += 1
            if done % 250 == 0:
                print(f"  …{done}/{len(groups)} websites")

    # Forget places Overture no longer lists (closed/removed), then publish.
    for pid in list(places_state):
        if pid not in current:
            del places_state[pid]
    published = [
        {
            "id": pid, "n": s["name"], "la": s["lat"], "lo": s["lon"], "a": s.get("address"),
            "w": clean_url(s.get("website")), "m": clean_url(s.get("menu")), "k": s["kind"], "d": s["dishes"],
            **({"b": s["broths"]} if s.get("broths", 0) >= 2 else {}),
            "c": s["checked"],
        }
        for pid, s in sorted(places_state.items())
        if s.get("kind") in ("shop", "serves")
    ]
    kinds = defaultdict(int)
    for s in places_state.values():
        kinds[s.get("kind", "?")] += 1
    stats = {"generated": today(), "overture_release": release, "candidates": len(candidates),
             "checked_this_run": sum(found.values()), "results_this_run": dict(found),
             "all_known": dict(kinds), "published": len(published)}
    print(json.dumps(stats, indent=2))

    if not args.dry_run:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(state, separators=(",", ":"), ensure_ascii=False))
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps({
            "v": 1, "generated": today(), "overture": release, "count": len(published),
            "about": "Places whose own website menu lists ramen. Places from Overture Maps Foundation "
                     "(CDLA-Permissive-2.0); menu checks by Knewdle NOW.",
            "places": published,
        }, separators=(",", ":"), ensure_ascii=False))
        STATS.write_text(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
