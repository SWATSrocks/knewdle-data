"""Finds ramen places the app's other sources miss, and publishes them in menu_ramen.json ("more").

    python -m menucheck.discover [--skip names,chains,web] [--chain NAME] [--max-domains 1500] [--dry-run]

  names  - Overture places that show ramen by other words, brands or their website address (names.py)
  chains - ramen chains' own store locators (chains.py, chains.json)
  web    - ramen websites seen in public certificate logs (ctlog.py)

State lives in state/extra_state.json, so each run only re-reads what's due.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .crawl import Crawler
from .places import Geocoder, PlaceIndex, host_of

ROOT = Path(__file__).resolve().parent.parent
EXTRA = ROOT / "state" / "extra_state.json"
MENU_STATE = ROOT / "state" / "menu_state.json"
OUT = ROOT / "docs" / "menu_ramen.json"
STATS = ROOT / "docs" / "stats.json"
CONFIG = ROOT / "config.json"
OPTOUT = ROOT / "optout.txt"

DOMAIN_RECHECK_DAYS = {"ok": 60, "no US address": 60, "unreachable": 30, "empty": 45, "parked": 45}
DEFAULT_RECHECK = 120
NEW_FOR_DAYS = 120       # a website first seen within this many days (after the first run) is marked new


def today() -> str:
    return dt.date.today().isoformat()


def days_since(d: str | None) -> int:
    return 10_000 if not d else (dt.date.today() - dt.date.fromisoformat(d)).days


def load_json(p: Path, default):
    return json.loads(p.read_text()) if p.exists() else default


def load_optout() -> set[str]:
    if not OPTOUT.exists():
        return set()
    return {l.strip().lower().removeprefix("www.") for l in OPTOUT.read_text().splitlines()
            if l.strip() and not l.startswith("#")}


def opted_out(website: str | None, optout: set[str]) -> bool:
    h = host_of(website or "")
    return bool(h) and any(h == o or h.endswith("." + o) for o in optout)


def run_web(state: dict, crawler: Crawler, geocode: Geocoder, max_domains: int, optout: set[str]) -> Counter:
    from .ctlog import all_domains, read_domain
    domains: dict = state.setdefault("domains", {})
    first_run = "baseline" not in state
    print("Certificate logs: searching crt.sh…")
    seen, by, ok = all_domains()
    print(f"  {len(seen)} ramen-named domains in certificate logs ({len(ok)} searches answered)")
    if not ok:
        return Counter({"crt.sh unavailable": 1})
    answered_before = set(state.get("patterns_answered", []))
    for d, cert_day in seen.items():
        if d not in domains:
            # Only a search that also answered on an earlier run can tell us a domain is truly new;
            # otherwise it's just the first time that search worked.
            domains[d] = {"first": today(), **({} if first_run or (by[d] & answered_before) else {"old": 1})}
        rec = domains[d]
        if cert_day and (not rec.get("cert") or cert_day < rec["cert"]):
            rec["cert"] = cert_day
    state["patterns_answered"] = sorted(answered_before | ok)
    if first_run:
        state["baseline"] = today()

    due = [d for d, rec in domains.items()
           if not opted_out(d, optout)
           and days_since(rec.get("checked")) >= DOMAIN_RECHECK_DAYS.get(rec.get("status"), DEFAULT_RECHECK)]
    # Newest first, so brand-new shops are looked at before the backlog.
    due.sort(key=lambda d: domains[d].get("first", ""), reverse=True)
    due = due[:max_domains]
    print(f"  reading {len(due)} of them this run")
    tally = Counter()
    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = {pool.submit(read_domain, crawler, d, geocode): d for d in due}
        for f in as_completed(futures):
            d = futures[f]
            try:
                res = f.result()
            except Exception as e:  # noqa: BLE001
                res = {"status": "error", "err": type(e).__name__}
            rec = domains[d]
            for k in ("status", "name", "website", "locations", "to", "err"):
                rec.pop(k, None)
            rec.update(res)
            rec["checked"] = today()
            tally[res["status"]] += 1
            if res["status"] == "ok":
                print(f"  🍜 {d}: {res['name']} ({len(res['locations'])} location(s))")
    return tally


def run_chains(state: dict, crawler: Crawler, geocode: Geocoder, only: str | None) -> Counter:
    from .chains import read_all
    print("Chains: reading store locators…")
    chains: dict = state.setdefault("chains", {})
    results = read_all(crawler, geocode, only)
    tally = Counter()
    for name, places in results.items():
        prev = chains.get(name, {}).get("places", [])
        # A site having a bad day (0 found) keeps last time's list for up to 60 days.
        if not places and prev and days_since(chains[name].get("checked")) < 60:
            tally[name] = len(prev)
            continue
        chains[name] = {"checked": today(), "places": places}
        tally[name] = len(places)
    return tally


def run_names(state: dict) -> tuple[Counter, list[dict]]:
    from .names import fetch_ramen_places
    print("Names: scanning Overture…")
    t0 = time.time()
    release, places = fetch_ramen_places()
    tally = Counter(p["f"] for p in places)
    print(f"  Overture {release}: {dict(tally)} ({time.time() - t0:.0f}s)")
    state["overture"] = {"release": release, "places": [p for p in places if p["f"] != "app"]}
    state["app_known"] = [{k: p[k] for k in ("name", "lat", "lon", "website")} for p in places if p["f"] == "app"]
    return tally, places


def build_published(state: dict, optout: set[str]) -> tuple[list[dict], Counter]:
    """One list, nothing twice, nothing the app already shows from Overture or the menu check."""
    known = PlaceIndex(state.get("app_known", []))
    for s in load_json(MENU_STATE, {"places": {}}).get("places", {}).values():
        if s.get("kind") in ("shop", "serves"):
            known.add(s)
    out, why = [], Counter()

    def add(entry: dict, f: str, extra: dict | None = None):
        p = {"name": entry["name"], "lat": entry["lat"], "lon": entry["lon"], "website": entry.get("website")}
        if opted_out(p["website"], optout) or known.has(p):
            return
        known.add(p)
        why[f] += 1
        out.append({"id": entry["id"], "n": entry["name"], "la": entry["lat"], "lo": entry["lon"],
                    "a": entry.get("address"), "w": entry.get("website"), "f": f, **(extra or {})})

    # Chains first (their own site is the best word on where they are), then Overture, then new websites.
    for name, rec in sorted(state.get("chains", {}).items()):
        for p in rec.get("places", []):
            add(p, "chain")
    for p in state.get("overture", {}).get("places", []):
        add(p, p["f"])
    baseline = state.get("baseline")
    for d, rec in sorted(state.get("domains", {}).items()):
        if rec.get("status") != "ok":
            continue
        is_new = bool(baseline and not rec.get("old") and rec.get("first", "") > baseline and days_since(rec.get("first")) <= NEW_FOR_DAYS)
        for loc in rec.get("locations", []):
            extra = {"s": rec.get("first")}
            if is_new:
                extra["nw"] = 1
            if loc.get("soon"):
                extra["so"] = 1
            add({"id": f"web_{d}_{loc['key']}", "name": rec.get("name") or d, "lat": loc["lat"], "lon": loc["lon"],
                 "address": loc.get("address"), "website": rec.get("website") or f"https://{d}/"}, "web", extra)
    out.sort(key=lambda e: e["id"])
    return out, why


def write_outputs(state: dict, published: list[dict], why: Counter, run_stats: dict, dry_run: bool) -> None:
    state["published"] = published
    stats = load_json(STATS, {})
    stats["more"] = {"generated": today(), "published": len(published), "by_way_found": dict(why), **run_stats}
    stats["more_published"] = len(published)
    print(json.dumps(stats["more"], indent=2, ensure_ascii=False))
    if dry_run:
        return
    EXTRA.parent.mkdir(parents=True, exist_ok=True)
    EXTRA.write_text(json.dumps(state, separators=(",", ":"), ensure_ascii=False))
    data = load_json(OUT, {"v": 1, "places": []})
    data["more"] = published
    data["generated"] = today()
    OUT.write_text(json.dumps(data, separators=(",", ":"), ensure_ascii=False))
    STATS.write_text(json.dumps(stats, indent=2, ensure_ascii=False))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip", default="", help="comma list of: names,chains,web")
    ap.add_argument("--chain", default=None, help="testing: only read chains whose name contains this")
    ap.add_argument("--max-domains", type=int, default=1500)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    skip = {s.strip() for s in args.skip.split(",") if s.strip()}

    config = load_json(CONFIG, {})
    crawler = Crawler(config.get("bot_info_url", "https://github.com/"))
    state = load_json(EXTRA, {})
    geocode = Geocoder(state.setdefault("geocode", {}))
    optout = load_optout()
    run_stats = {}

    steps = [("names", lambda: run_names(state)[0]),
             ("chains", lambda: run_chains(state, crawler, geocode, args.chain)),
             ("web", lambda: run_web(state, crawler, geocode, args.max_domains, optout))]
    for name, step in steps:
        if name in skip:
            continue
        t0 = time.time()
        try:
            run_stats[name] = dict(step())
        except Exception as e:  # noqa: BLE001 - one source failing mustn't lose the others
            print(f"! {name} failed: {type(e).__name__}: {e}", file=sys.stderr)
            run_stats[name] = {"failed": f"{type(e).__name__}: {e}"[:200]}
        print(f"  ({name}: {time.time() - t0:.0f}s)")
        # Save after each step, so a later step timing out can't lose this one's work.
        published, why = build_published(state, optout)
        write_outputs(state, published, why, run_stats, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
