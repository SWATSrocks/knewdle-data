"""One-off study: what does OpenStreetMap add on top of Overture + Knewdle's own finds?

For a set of US metros, asks Overpass exactly what the app asks (ramen within 25 km), times every server, and
compares the shops with Overture's ramen (same rule as the app) and the published Knewdle finds.
Writes state/osm_compare.txt.
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

import requests

from . import candidates as C
from .places import same_place

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "state" / "osm_compare.txt"
R_M = 25000
METROS = {
    "New York": (40.7484, -73.9857), "Los Angeles": (34.0522, -118.2437), "Chicago": (41.8781, -87.6298),
    "Houston": (29.7604, -95.3698), "Phoenix": (33.4484, -112.0740), "Philadelphia": (39.9526, -75.1652),
    "San Antonio": (29.4241, -98.4936), "San Diego": (32.7157, -117.1611), "Dallas": (32.7767, -96.7970),
    "Austin": (30.2672, -97.7431), "Charlotte": (35.2271, -80.8431), "Lake Norman": (35.5849, -80.8101),
    "Raleigh": (35.7796, -78.6382), "Atlanta": (33.7490, -84.3880), "Seattle": (47.6062, -122.3321),
    "Denver": (39.7392, -104.9903), "Columbus": (39.9612, -82.9988), "Nashville": (36.1627, -86.7816),
    "Portland": (45.5152, -122.6784), "Boston": (42.3601, -71.0589), "Miami": (25.7617, -80.1918),
    "Minneapolis": (44.9778, -93.2650), "Salt Lake City": (40.7608, -111.8910), "Greenville SC": (34.8526, -82.3940),
}
ENDPOINTS = ["https://overpass-api.de/api/interpreter", "https://overpass.private.coffee/api/interpreter",
             "https://maps.mail.ru/osm/tools/overpass/api/interpreter"]
NAME = ("ramen|ramyun|ラーメン|らーめん|拉麺|拉麵|tonkotsu|ichiran|ippudo|menya|men-ya|jinya|kizuki|santouka|hinodeya|totto")


def overpass(lat, lon, log):
    q = f"""[out:json][timeout:25];
(
  nwr["amenity"~"restaurant|fast_food|food_court"]["cuisine"~"ramen",i](around:{R_M},{lat},{lon});
  nwr["amenity"~"restaurant|fast_food|food_court"]["name"~"{NAME}",i](around:{R_M},{lat},{lon});
);
out center tags 300;"""
    result = None
    for ep in ENDPOINTS:
        t0 = time.time()
        try:
            r = requests.post(ep, data={"data": q}, timeout=30, headers={"User-Agent": "knewdle-data study"})
            ok = r.status_code == 200 and r.text.lstrip().startswith("{")
            log.append((ep.split("/")[2], round(time.time() - t0, 1), r.status_code if not ok else "ok"))
            if ok and result is None:
                result = r.json()
        except Exception as e:  # noqa: BLE001
            log.append((ep.split("/")[2], round(time.time() - t0, 1), type(e).__name__))
        time.sleep(3)
    if result is None:
        return None
    out = []
    for e in result.get("elements", []):
        t = e.get("tags", {})
        la = e.get("lat") or e.get("center", {}).get("lat"); lo = e.get("lon") or e.get("center", {}).get("lon")
        if la is None or not t.get("name"):
            continue
        out.append({"name": t["name"], "lat": la, "lon": lo, "website": t.get("website") or t.get("contact:website"),
                    "phone": t.get("phone") or t.get("contact:phone"), "hours": t.get("opening_hours"),
                    "ramen_tag": "ramen" in t.get("cuisine", "").lower()})
    return out


def overture(release):
    path = C.PLACES.format(release=release)
    con = C._connect()
    cols = C._columns(con, path)
    cat = C._category_expr(cols)
    closed = "AND coalesce(operating_status, 'open') NOT LIKE '%closed%'" if "operating_status" in cols else ""
    boxes = []
    for lat, lon in METROS.values():
        dlat = R_M / 111000; dlon = R_M / (111000 * math.cos(math.radians(lat)))
        boxes.append(f"(bbox.xmin BETWEEN {lon-dlon} AND {lon+dlon} AND bbox.ymin BETWEEN {lat-dlat} AND {lat+dlat})")
    sql = f"""SELECT names."primary" AS name, bbox.ymin AS lat, bbox.xmin AS lon, websites[1] AS website, phones[1] AS phone,
                     {cat} AS cat
              FROM read_parquet('{path}', hive_partitioning=1)
              WHERE ({' OR '.join(boxes)}) {closed}
                AND ({cat} LIKE '%ramen%' OR regexp_matches(lower(names."primary"), '(^|[^a-z])({NAME})'))"""
    rows = con.execute(sql).fetchall()
    return [{"name": r[0], "lat": r[1], "lon": r[2], "website": r[3], "phone": r[4]} for r in rows if r[0]]


def near(p, lat, lon):
    return math.hypot((p["lat"] - lat) * 111000, (p["lon"] - lon) * 111000 * math.cos(math.radians(lat))) <= R_M


def main():
    release = C.latest_release()
    ovt = overture(release)
    finds = []
    for f in ("docs/menu_ramen.json",):
        d = json.loads((ROOT / f).read_text())
        finds += [{"name": p["n"], "lat": p["la"], "lon": p["lo"], "website": p.get("w")} for p in d["places"]]
    ex = json.loads((ROOT / "state/extra_state.json").read_text())
    finds += [{"name": p["n"], "lat": p["la"], "lon": p["lo"], "website": p.get("w")} for p in ex.get("published", [])]
    lines = [f"Overture {release}: {len(ovt)} ramen places in the {len(METROS)} metros; {len(finds)} Knewdle finds US-wide", ""]
    tot = {"osm": 0, "only": 0, "fails": 0, "hours": 0, "hours_only": 0, "phone_add": 0, "web_add": 0, "ovt": 0}
    timing = []
    only_examples = []
    for city, (lat, lon) in METROS.items():
        log = []
        osm = overpass(lat, lon, log)
        timing += log
        o_here = [p for p in ovt if near(p, lat, lon)]
        f_here = [p for p in finds if near(p, lat, lon)]
        tot["ovt"] += len(o_here)
        if osm is None:
            tot["fails"] += 1
            lines.append(f"{city}: every Overpass server failed {log}")
            continue
        only = []
        for p in osm:
            m = next((q for q in o_here + f_here if same_place(p, q)), None)
            if p["hours"]: tot["hours"] += 1
            if m is None:
                only.append(p)
                if p["hours"]: tot["hours_only"] += 1
            else:
                if p["phone"] and not m.get("phone"): tot["phone_add"] += 1
                if p["website"] and not m.get("website"): tot["web_add"] += 1
        tot["osm"] += len(osm); tot["only"] += len(only)
        lines.append(f"{city}: OSM {len(osm)}, Overture {len(o_here)}, finds {len(f_here)}, OSM-only {len(only)}, "
                     f"OSM with hours {sum(1 for p in osm if p['hours'])}  {log}")
        only_examples += [f"   {city}: {p['name']} ({p['lat']:.4f},{p['lon']:.4f}) site={p['website']} ramen_tag={p['ramen_tag']}"
                          for p in only]
    lines += ["", f"TOTAL {tot}", "",
              "Server answers: " + json.dumps({ep: {
                  "ok": sum(1 for e, _, s in timing if e == ep and s == "ok"),
                  "fail": sum(1 for e, _, s in timing if e == ep and s != "ok"),
                  "avg_s_ok": round(sum(t for e, t, s in timing if e == ep and s == "ok") /
                                    max(1, sum(1 for e, _, s in timing if e == ep and s == "ok")), 1)}
                  for ep in {e for e, _, _ in timing}}), "", "OSM-only shops:"] + only_examples
    OUT.write_text("\n".join(lines))
    print("\n".join(lines[:40]))


if __name__ == "__main__":
    main()
