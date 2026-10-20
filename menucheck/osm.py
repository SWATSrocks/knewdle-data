"""OpenStreetMap's US ramen, read from Geofabrik's free daily OpenStreetMap extracts.

    python -m menucheck.osm restaurants.geojsonseq

The phone used to ask public Overpass servers, which were too busy to answer most of the time (a study in 24
metros: they failed in 15, and even asking one state at a time from here, they answered for 8 of 52). So the
workflow downloads Geofabrik's US region extracts, keeps only restaurants (osmium), and this picks out the ramen
with the same rule the app always used. The app just downloads docs/osm_ramen.json, in Overpass's JSON shape.

Data © OpenStreetMap contributors, available under the Open Database License (ODbL 1.0).
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "osm_ramen.json"
NAME = "ramen|ramyun|ラーメン|らーめん|拉麺|拉麵|tonkotsu|ichiran|ippudo|menya|men-ya|jinya|kizuki|santouka|hinodeya|totto"
NAME_RX = re.compile(rf"(?<![a-z])(?:{NAME})", re.IGNORECASE)
KEEP_TAGS = ("name", "name:en", "cuisine", "addr:housenumber", "addr:street", "addr:city", "addr:state",
             "opening_hours", "phone", "contact:phone", "website", "contact:website")
MIN_SHARE = 0.7       # a result under 70% of the last count is treated as broken, and the last list stays


def is_ramen(tags: dict) -> bool:
    if tags.get("amenity") not in ("restaurant", "fast_food", "food_court"):
        return False
    if "ramen" in (tags.get("cuisine") or "").lower():
        return True
    return bool(NAME_RX.search(tags.get("name") or "") or NAME_RX.search(tags.get("name:en") or ""))


def _center(geom: dict) -> tuple[float, float] | None:
    t, c = geom.get("type"), geom.get("coordinates")
    if t == "Point":
        return c[1], c[0]
    rings = c[0] if t == "Polygon" else c[0][0] if t == "MultiPolygon" else None
    if not rings:
        return None
    pts = rings[:-1] if len(rings) > 1 and rings[0] == rings[-1] else rings
    return sum(p[1] for p in pts) / len(pts), sum(p[0] for p in pts) / len(pts)


def element(feature: dict) -> dict | None:
    """One osmium-export feature -> the slim Overpass-style element the app reads, or None if it isn't ramen."""
    props = feature.get("properties") or {}
    tags = {k: v for k, v in props.items() if not k.startswith("@")}
    if not (tags.get("name") or tags.get("name:en")) or not is_ramen(tags):
        return None
    c = _center(feature.get("geometry") or {})
    if c is None:
        return None
    typ = props.get("@type") or "node"
    oid = props.get("@id")
    if isinstance(oid, str):
        oid = int(re.sub(r"\D", "", oid) or 0)
    if typ == "area":  # osmium area ids: way*2 or relation*2+1
        typ, oid = ("way", oid // 2) if oid % 2 == 0 else ("relation", oid // 2)
    return {"type": typ, "id": oid, "lat": round(c[0], 6), "lon": round(c[1], 6),
            "tags": {k: tags[k] for k in KEEP_TAGS if tags.get(k)}}


def slim(d: dict) -> list[dict]:
    """Overpass JSON -> slim elements (kept for tests and one-off use)."""
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


def main(argv: list[str]) -> int:
    src = Path(argv[0])
    seen, els, n = set(), [], 0
    with src.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip().lstrip("\x1e")
            if not line:
                continue
            n += 1
            try:
                e = element(json.loads(line))
            except Exception:  # noqa: BLE001
                continue
            if e and (e["type"], e["id"]) not in seen:
                seen.add((e["type"], e["id"])); els.append(e)
    els.sort(key=lambda x: (x["type"], x["id"]))
    try:
        prev = len(json.loads(OUT.read_text()).get("elements", []))
    except Exception:  # noqa: BLE001
        prev = 0
    print(f"{n} US restaurants on OpenStreetMap; {len(els)} ramen ({len(els) - prev:+d} vs last list)")
    if prev and len(els) < MIN_SHARE * prev:
        print("Looks broken (too few); keeping the last list")
        return 1
    OUT.write_text(json.dumps({
        "v": 1, "generated": dt.date.today().isoformat(),
        "about": "Ramen places in the US from OpenStreetMap (via Geofabrik extracts), refreshed twice a week for the Knewdle NOW app.",
        "license": "Data © OpenStreetMap contributors, Open Database License (ODbL 1.0): https://www.openstreetmap.org/copyright",
        "elements": els}, separators=(",", ":"), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
