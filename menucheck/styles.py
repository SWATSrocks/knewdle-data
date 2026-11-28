"""Which ramen styles each ramen shop serves, read from its own website menu (published as docs/styles.json).

Also notes the shop's own "join the waitlist" link when its website posts one (styles.json "wait"), for the app's
Join the waitlist button. We only read the shop's own site; the app just opens that link.

    python -m menucheck.styles [--max-sites 6000] [--workers 24] [--focus lat,lon] [--dry-run]

Covers every shop the app can show that has a website: places from the menu check, OpenStreetMap ramen shops
and Knewdle's own finds (chains, new websites). Same polite reader as the menu check (robots.txt, one site at a
time per host). Keyed like hours.json: the bare website ("shop.com") or, when the listing points at a page,
host + path ("toasttab.com/local/order/x"), so the app can look up a shop from any source.
Re-reads a site every 30 days; the first run reads everything.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

from .classify import classify
from .crawl import Crawler, is_blocked_host
from .run import CONFIG, ROOT, days_since, load_json, load_optout, today

OUT = ROOT / "docs" / "styles.json"
STATE = ROOT / "state" / "styles_state.json"
RECHECK_DAYS = 30


def key_of(website: str) -> str | None:
    """"https://www.shop.com/" -> "shop.com"; "https://toasttab.com/local/order/x" -> "toasttab.com/local/order/x"."""
    if not website:
        return None
    u = website if website.startswith("http") else "https://" + website
    p = urlparse(u)
    host = (p.netloc or "").lower().removeprefix("www.")
    if not host:
        return None
    path = (p.path or "").rstrip("/").lower()
    return host + path if path else host


def sites() -> dict[str, tuple[str, str, float | None, float | None]]:
    """key -> (website, shop name, lat, lon) for every ramen place with a website."""
    out: dict[str, tuple[str, str, float | None, float | None]] = {}

    def add(site, name, la=None, lo=None):
        k = key_of(site or "")
        if k and k not in out and not is_blocked_host(site if site.startswith("http") else "https://" + site):
            out[k] = (site, name or "", la, lo)

    menu = load_json(ROOT / "state" / "menu_state.json", {}).get("places", {})
    for v in menu.values():
        if v.get("kind") in ("shop", "serves"):
            add(v.get("website"), v.get("name"), v.get("lat"), v.get("lon"))
    osm = load_json(ROOT / "docs" / "osm_ramen.json", {}).get("elements", [])
    for e in osm:
        t = e.get("tags") or {}
        la = e.get("lat", (e.get("center") or {}).get("lat"))
        lo = e.get("lon", (e.get("center") or {}).get("lon"))
        add(t.get("website") or t.get("contact:website"), t.get("name"), la, lo)
    pub = load_json(ROOT / "docs" / "menu_ramen.json", {})
    for r in pub.get("more", []) + pub.get("places", []):
        add(r.get("w"), r.get("n"), r.get("la"), r.get("lo"))
    return out


def read_styles(crawler: Crawler, website: str, name: str) -> dict:
    site = crawler.read_site(website)
    if not site.ok:
        return {"err": site.error or "unreachable"}
    best = []
    wait = next((p.waitlist for p in site.pages if p.waitlist), None)
    for page in site.pages:
        v = classify(page.text, name)
        if v.kind != "none" and len(v.styles) > len(best):
            best = v.styles
    combined = classify("\n".join(p.text for p in site.pages), name)
    if combined.kind != "none":
        best = sorted(set(best) | set(combined.styles))
    rec = {"styles": best, "wk": 1}
    if wait:
        rec["wait"] = wait
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-sites", type=int, default=6000)
    ap.add_argument("--workers", type=int, default=24)
    ap.add_argument("--focus", default="", help="'lat,lon': read sites near here first")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    config = load_json(CONFIG, {})
    crawler = Crawler(config.get("bot_info_url", "https://github.com/"))
    optout = load_optout()
    state: dict = load_json(STATE, {"sites": {}})
    seen: dict = state.setdefault("sites", {})
    all_sites = sites()

    due = []
    for k, (site, name, la, lo) in all_sites.items():
        host = k.split("/", 1)[0]
        if any(host == o or host.endswith("." + o) for o in optout):
            continue
        prev = seen.get(k)
        # "wk": read since waitlist links were added (sites read before that get one more read now).
        if prev is None or not prev.get("wk") or days_since(prev.get("checked")) >= RECHECK_DAYS:
            due.append((0 if prev is None else 1, prev.get("checked", "") if prev else "", k))
    due.sort()
    if args.focus.strip():
        flat, flon = (float(x) for x in args.focus.split(","))

        def miles(k):
            _, _, la, lo = all_sites[k]
            if la is None or lo is None:
                return 1e9
            return math.hypot((la - flat) * 69.0, (lo - flon) * 69.0 * math.cos(math.radians(flat)))

        due.sort(key=lambda d: miles(d[2]))
    due = due[: args.max_sites]
    print(f"{len(all_sites)} ramen websites; {len(due)} to read now")

    t0, done, found = time.time(), 0, 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(read_styles, crawler, all_sites[k][0], all_sites[k][1]): k for _, _, k in due}
        for f in as_completed(futs):
            k = futs[f]
            try:
                rec = f.result()
            except Exception as e:  # one bad site never stops the run
                rec = {"err": str(e)[:120]}
            rec["checked"] = today()
            seen[k] = rec
            done += 1
            found += bool(rec.get("styles"))
            if done % 250 == 0:
                print(f"  {done}/{len(due)} read, {found} with styles ({time.time() - t0:.0f}s)", flush=True)

    # Forget sites no longer listed anywhere.
    for k in list(seen):
        if k not in all_sites:
            del seen[k]
    published = {k: v["styles"] for k, v in sorted(seen.items()) if v.get("styles")}
    waits = {k: v["wait"] for k, v in sorted(seen.items()) if v.get("wait")}
    print(f"Done: {done} read in {time.time() - t0:.0f}s; {len(published)} sites with known styles, {len(waits)} with a waitlist link")
    if args.dry_run:
        return 0
    STATE.write_text(json.dumps(state, separators=(",", ":")))
    OUT.write_text(json.dumps({"v": 1, "generated": today(), "sites": published, "wait": waits}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
