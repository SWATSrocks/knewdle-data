"""OpenStreetMap's US ramen, fetched once a day here instead of from every phone.

    python -m menucheck.osm

Public Overpass servers are often too busy to answer a phone within seconds (a study in 24 metros: they failed
in 15). From here one patient request can wait minutes and retry, so the app just downloads the result:
docs/osm_ramen.json, in Overpass's own JSON shape (so the app reads it the way it read Overpass), with only the
tags the app uses. If every server fails, or the answer looks cut short, yesterday's file stays.

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
MIN_SHARE = 0.7       # a result under 70% of yesterday's count is treated as cut short
QUERY = f"""[out:json][timeout:900][maxsize:1073741824];
area["ISO3166-1"="US"][admin_level=2]->.us;
(
  nwr["amenity"~"^(restaurant|fast_food|food_court)$"]["cuisine"~"ramen",i](area.us);
  nwr["amenity"~"^(restaurant|fast_food|food_court)$"]["name"~"{NAME}",i](area.us);
);
out center tags;"""
HEADERS = {"User-Agent": "knewdle-data/1.0 (daily ramen list for the Knewdle NOW app; https://github.com/SWATSrocks/knewdle-data)"}


def fetch(log=print, budget_s: float = 45 * 60) -> dict | None:
    end = time.time() + budget_s
    for attempt in range(3):
        for ep in ENDPOINTS:
            if time.time() > end - 120:
                log("  out of time for today")
                return None
            t0 = time.time()
            try:
                r = requests.post(ep, data={"data": QUERY}, headers=HEADERS, timeout=(20, max(60, min(960, end - time.time()))))
                if r.status_code == 200 and r.text.lstrip().startswith("{"):
                    d = r.json()
                    if "runtime error" in str(d.get("remark", "")):
                        log(f"  {ep}: cut short ({d['remark'][:80]})")
                    else:
                        log(f"  {ep}: {len(d.get('elements', []))} places in {time.time() - t0:.0f}s")
                        return d
                else:
                    log(f"  {ep}: HTTP {r.status_code} after {time.time() - t0:.0f}s")
            except requests.RequestException as e:
                log(f"  {ep}: {type(e).__name__} after {time.time() - t0:.0f}s")
            time.sleep(20)
        time.sleep(120 * (attempt + 1))
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


def load_previous() -> list[dict]:
    try:
        return json.loads(OUT.read_text()).get("elements", [])
    except Exception:  # noqa: BLE001
        return []


def main() -> int:
    prev = load_previous()
    d = fetch()
    if d is None:
        print(f"OpenStreetMap: no server answered; keeping the last list ({len(prev)} places)")
        return 0
    els = slim(d)
    if prev and len(els) < MIN_SHARE * len(prev):
        print(f"OpenStreetMap: only {len(els)} places vs {len(prev)} before; looks cut short, keeping the last list")
        return 0
    OUT.write_text(json.dumps({
        "v": 1, "generated": dt.date.today().isoformat(),
        "about": "Ramen places in the US from OpenStreetMap, refreshed daily for the Knewdle NOW app.",
        "license": "Data © OpenStreetMap contributors, Open Database License (ODbL 1.0): https://www.openstreetmap.org/copyright",
        "elements": els}, separators=(",", ":"), ensure_ascii=False))
    print(f"OpenStreetMap: {len(els)} ramen places published ({len(els) - len(prev):+d} vs last list)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
