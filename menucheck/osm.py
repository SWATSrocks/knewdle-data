"""OpenStreetMap's US ramen, fetched once a day here instead of from every phone.

    python -m menucheck.osm

Public Overpass servers are often too busy to answer a phone within seconds (a study in 24 metros: they failed
in 15). From here it asks one state at a time, patiently, retrying the ones that failed, and keeps each state's
last good answer (up to 30 days) when its servers stay busy. The app just downloads the result:
docs/osm_ramen.json, in Overpass's own JSON shape, with only the tags the app uses.

Data © OpenStreetMap contributors, available under the Open Database License (ODbL 1.0).
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "osm_ramen.json"
ENDPOINTS = ["https://overpass-api.de/api/interpreter", "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
             "https://overpass.private.coffee/api/interpreter"]
# Same rule as the app always used: tagged ramen, or a ramen word in the name.
NAME = "ramen|ramyun|ラーメン|らーめん|拉麺|拉麵|tonkotsu|ichiran|ippudo|menya|men-ya|jinya|kizuki|santouka|hinodeya|totto"
KEEP_TAGS = ("name", "name:en", "cuisine", "addr:housenumber", "addr:street", "addr:city", "addr:state",
             "opening_hours", "phone", "contact:phone", "website", "contact:website")
REGIONS = ["AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY",
           "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH",
           "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "PR"]
STATE = ROOT / "state" / "osm_regions.json"
LOG = ROOT / "state" / "osm_log.txt"
KEEP_DAYS = 30        # a state whose servers keep failing keeps its last answer this long


def query(region: str) -> str:
    """One state at a time: small enough that a busy server still answers."""
    return f"""[out:json][timeout:170];
area["ISO3166-2"="US-{region}"]->.a;
(
  nwr["amenity"~"^(restaurant|fast_food|food_court)$"]["cuisine"~"ramen",i](area.a);
  nwr["amenity"~"^(restaurant|fast_food|food_court)$"]["name"~"{NAME}",i](area.a);
);
out center tags;"""


HEADERS = {"User-Agent": "knewdle-data/1.0 (daily ramen list for the Knewdle NOW app; https://github.com/SWATSrocks/knewdle-data)"}


def fetch_region(region: str, log, end: float) -> dict | None:
    for ep in ENDPOINTS:
        if time.time() > end:
            return None
        t0 = time.time()
        try:
            r = requests.post(ep, data={"data": query(region)}, headers=HEADERS, timeout=(15, 190))
            if r.status_code == 200 and r.text.lstrip().startswith("{"):
                d = r.json()
                if "runtime error" in str(d.get("remark", "")):
                    log(f"  {region} {ep.split('/')[2]}: cut short")
                else:
                    log(f"  {region} {ep.split('/')[2]}: {len(d.get('elements', []))} in {time.time() - t0:.0f}s")
                    return d
            else:
                log(f"  {region} {ep.split('/')[2]}: HTTP {r.status_code} after {time.time() - t0:.0f}s")
        except requests.RequestException as e:
            log(f"  {region} {ep.split('/')[2]}: {type(e).__name__} after {time.time() - t0:.0f}s")
        time.sleep(5)
    return None


def slim(d: dict) -> list[dict]:
    out = []
    for e in d.get("elements", []):
        tags = e.get("tags") or {}
        if not (tags.get("name") or tags.get("name:en")):
            continue
        lat = e.get("lat", (e.get("center") or {}).get("lat"))
        lon = e.get("lon", (e.get("center") or {}).get("lon"))
        if lat is None or lon is None:
            continue
        out.append({"type": e["type"], "id": e["id"], "lat": round(lat, 6), "lon": round(lon, 6),
                    "tags": {k: tags[k] for k in KEEP_TAGS if tags.get(k)}})
    out.sort(key=lambda x: (x["type"], x["id"]))
    return out


def main(budget_min: float = 45) -> int:
    end = time.time() + budget_min * 60
    lines: list[str] = []

    def log(m):
        print(m); lines.append(m)

    today = dt.date.today()
    try:
        regions = json.loads(STATE.read_text())
    except Exception:  # noqa: BLE001
        regions = {}
    # Oldest answers first, so a bad day still moves the whole country forward.
    todo = sorted(REGIONS, key=lambda r: regions.get(r, {}).get("day", ""))
    for rnd in range(3):
        failed = []
        for reg in todo:
            if regions.get(reg, {}).get("day") == today.isoformat():
                continue
            d = fetch_region(reg, log, end)
            if d is None:
                failed.append(reg)
                continue
            els = slim(d)
            old = regions.get(reg, {}).get("elements", [])
            if old and len(els) < 0.7 * len(old):
                log(f"  {reg}: only {len(els)} vs {len(old)} before; keeping the last answer")
                continue
            regions[reg] = {"day": today.isoformat(), "elements": els}
            time.sleep(2)
        todo = failed
        if not todo or time.time() > end:
            break
        time.sleep(60)
    for reg in list(regions):
        if (today - dt.date.fromisoformat(regions[reg]["day"])).days > KEEP_DAYS:
            regions.pop(reg)
    seen, els = set(), []
    for reg in sorted(regions):
        for e in regions[reg]["elements"]:
            if (e["type"], e["id"]) not in seen:
                seen.add((e["type"], e["id"])); els.append(e)
    fresh = sum(1 for r in regions.values() if r["day"] == today.isoformat())
    log(f"OpenStreetMap: {len(els)} ramen places from {len(regions)} states ({fresh} refreshed today; "
        f"not answered today: {', '.join(todo) or 'none'})")
    STATE.write_text(json.dumps(regions, separators=(",", ":"), ensure_ascii=False))
    LOG.write_text("\n".join(lines) + "\n")
    if els:
        OUT.write_text(json.dumps({
            "v": 1, "generated": today.isoformat(),
            "about": "Ramen places in the US from OpenStreetMap, refreshed daily for the Knewdle NOW app.",
            "license": "Data © OpenStreetMap contributors, Open Database License (ODbL 1.0): https://www.openstreetmap.org/copyright",
            "elements": sorted(els, key=lambda x: (x["type"], x["id"]))}, separators=(",", ":"), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
