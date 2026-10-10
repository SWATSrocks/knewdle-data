"""Finds ramen places the app's other sources miss, and publishes them in menu_ramen.json ("more").

    python -m menucheck.discover [--skip names,foursquare,chains,halls,web] [--chain NAME] [--max-domains 1500] [--dry-run]

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

CREDITS = ("Places from Overture Maps Foundation (CDLA-Permissive-2.0). Contains data from Foursquare Open "
           "Source Places, (c) Foursquare Labs, Inc. (Apache License 2.0, see NOTICE-foursquare.txt). "
           "Menu checks, chain locations and new-website finds by Knewdle NOW.")
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


def run_web(state: dict, crawler: Crawler, geocode: Geocoder, max_domains: int, optout: set[str],
            save=lambda: None, read_budget_s: float = 25 * 60) -> Counter:
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
    save()  # keep the domain list even if reading runs out of time
    tally = Counter()
    deadline = time.time() + read_budget_s
    pool = ThreadPoolExecutor(max_workers=16)
    futures = {pool.submit(read_domain, crawler, d, geocode): d for d in due}
    try:
        for f in as_completed(futures, timeout=max(60.0, deadline - time.time())):
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
            if tally.total() % 100 == 0:
                save()
    except TimeoutError:
        print(f"  out of time: {sum(1 for f in futures if not f.done())} domains left for next run")
    finally:
        for f in futures:
            f.cancel()
        pool.shutdown(wait=False, cancel_futures=True)
    return tally


def run_chains(state: dict, crawler: Crawler, geocode: Geocoder, only: str | None) -> Counter:
    from .chains import read_all
    print("Chains: reading store locators…")
    chains: dict = state.setdefault("chains", {})
    results = read_all(crawler, geocode, only)
    if not only:
        import json as _json
        from .chains import CONFIG as _CHAINS
        configured = {c["name"] for c in _json.loads(_CHAINS.read_text())["chains"]}
        for name in list(chains):
            if name not in configured:
                del chains[name]
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


def run_halls(state: dict, crawler: Crawler, geocode: Geocoder) -> Counter:
    from .halls import CONFIG as HALLS, read_all
    print("Halls: reading food hall and market vendor directories…")
    halls: dict = state.setdefault("halls", {})
    results = read_all(crawler, geocode)
    configured = {h["name"] for h in json.loads(HALLS.read_text())["halls"]}
    for name in list(halls):
        if name not in configured:
            del halls[name]
    tally = Counter()
    for name, places in results.items():
        prev = halls.get(name, {}).get("places", [])
        if places is None:
            # Couldn't read the directory this time: keep last time's list for up to 30 days.
            if prev and days_since(halls[name].get("checked")) < 30:
                tally[name] = len(prev)
            else:
                halls.pop(name, None)
            continue
        halls[name] = {"checked": today(), "places": places}
        if places:
            tally[name] = len(places)
    return tally


def run_names(state: dict) -> tuple[Counter, list[dict]]:
    from .names import fetch_ramen_places
    from .candidates import latest_release
    from .names import RULES_VERSION
    prev = state.get("overture", {})
    latest = latest_release()
    if prev.get("release") == latest and prev.get("rules") == RULES_VERSION and "app_known" in state:
        print(f"Names: Overture {latest} already scanned with these rules; skipping (new release monthly)")
        return Counter(prev.get("tally", {})), []
    print("Names: scanning Overture…")
    t0 = time.time()
    release, places = fetch_ramen_places(latest)
    tally = Counter(p["f"] for p in places)
    print(f"  Overture {release}: {dict(tally)} ({time.time() - t0:.0f}s)")
    state["overture"] = {"release": release, "rules": RULES_VERSION, "tally": dict(tally),
                         "places": [p for p in places if p["f"] != "app"]}
    state["app_known"] = [{k: p.get(k) for k in ("name", "lat", "lon", "website", "address")}
                          for p in places if p["f"] == "app"]
    return tally, places


def run_foursquare(state: dict) -> Counter:
    from . import foursquare as fsq
    if not fsq.token():
        print("Foursquare: no HF_TOKEN secret, skipping")
        return Counter({"skipped (no HF_TOKEN)": 1})
    prev = state.get("foursquare", {})
    release = fsq.latest_release()
    if prev.get("release") == release and prev.get("rules") == fsq.RULES_VERSION:
        print(f"Foursquare: release {release} already read; skipping (new releases about monthly)")
        return Counter(prev.get("tally", {}))
    print(f"Foursquare: reading release {release}…")
    t0 = time.time()
    places = fsq.fetch(release)
    tally = Counter({"ramen places": len(places)})
    print(f"  {len(places)} US ramen places ({time.time() - t0:.0f}s)")
    state["foursquare"] = {"release": release, "rules": fsq.RULES_VERSION, "tally": dict(tally), "places": places}
    notice = fsq.notice_text()
    if notice:
        (ROOT / "docs" / "NOTICE-foursquare.txt").write_text(notice)
    return tally


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
    for name, rec in sorted(state.get("halls", {}).items()):
        for p in rec.get("places", []):
            add(p, "hall", {"in": p.get("in", name)})
    for p in state.get("overture", {}).get("places", []):
        add(p, p["f"])
    for p in state.get("foursquare", {}).get("places", []):
        add(p, "fsq")
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


def write_outputs(state: dict, published: list[dict], why: Counter, run_stats: dict, dry_run: bool,
                  quiet: bool = False) -> None:
    state["published"] = published
    stats = load_json(STATS, {})
    stats["more"] = {"generated": today(), "published": len(published), "by_way_found": dict(why), **run_stats}
    stats["more_published"] = len(published)
    if not quiet:
        print(json.dumps(stats["more"], indent=2, ensure_ascii=False), flush=True)
    if dry_run:
        return
    EXTRA.parent.mkdir(parents=True, exist_ok=True)
    # Reading threads may still be adding geocodes: save a copy (copying a dict is atomic in CPython).
    snapshot = {**state, "geocode": dict(state.get("geocode", {}))}
    EXTRA.write_text(json.dumps(snapshot, separators=(",", ":"), ensure_ascii=False))
    data = load_json(OUT, {"v": 1, "places": []})
    data["more"] = published
    data["generated"] = today()
    data["credits"] = CREDITS
    OUT.write_text(json.dumps(data, separators=(",", ":"), ensure_ascii=False))
    STATS.write_text(json.dumps(stats, indent=2, ensure_ascii=False))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip", default="", help="comma list of: names,foursquare,chains,halls,web")
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

    def save():
        published, why = build_published(state, optout)
        write_outputs(state, published, why, run_stats, args.dry_run, quiet=True)

    steps = [("names", lambda: run_names(state)[0]),
             ("foursquare", lambda: run_foursquare(state)),
             ("chains", lambda: run_chains(state, crawler, geocode, args.chain)),
             ("halls", lambda: run_halls(state, crawler, geocode)),
             ("web", lambda: run_web(state, crawler, geocode, args.max_domains, optout, save))]
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
