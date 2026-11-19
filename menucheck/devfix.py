"""Address fixes from Knewdle Pulse ({n, la, lo} as published, then {a, nla, nlo}): applied to docs/menu_ramen.json so
older app versions and the city pages show the right address and spot. (Phones on 1.17.2+ also apply them live.)"""
from __future__ import annotations

import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MENU = ROOT / "docs" / "menu_ramen.json"
FIX_LIST = ROOT / "address_fixes.txt"          # hand-kept fixes (same thing as Pulse's "Fix address")
DETAILS_LIST = ROOT / "shop_details.txt"       # hand-kept name / website fixes
HOURS_LIST = ROOT / "hours_by_hand.txt"        # hand-kept opening hours (shops whose site can't be read)
HOURS_JSON = ROOT / "docs" / "hours.json"
GEO_CACHE = ROOT / "state" / "geocoded.json"   # address -> [lat, lon], so each address is looked up once
NEAR = 2e-4   # ~20 m: the published spot the fix was made against


def _hit(p: dict, f: dict) -> bool:
    if (p.get("n") or "").strip().lower() != (f.get("n") or "").strip().lower():
        return False
    try:
        la, lo = float(p["la"]), float(p["lo"])
    except (KeyError, TypeError, ValueError):
        return False
    return (abs(la - f["la"]) < NEAR and abs(lo - f["lo"]) < NEAR) or (abs(la - f["nla"]) < NEAR and abs(lo - f["nlo"]) < NEAR)


def apply(data: dict, fixes: list[dict]) -> int:
    """Rewrites matching listings in a menu_ramen.json dict; returns how many changed."""
    changed = 0
    fixes = [f for f in fixes if all(k in f for k in ("n", "la", "lo", "a", "nla", "nlo"))]
    for part in ("places", "more"):
        for p in data.get(part, []):
            for f in fixes:
                if _hit(p, f) and (p.get("a") != f["a"] or p.get("la") != f["nla"] or p.get("lo") != f["nlo"]):
                    p["a"], p["la"], p["lo"] = f["a"], f["nla"], f["nlo"]
                    changed += 1
    return changed


def apply_file(fixes: list[dict]) -> int:
    if not fixes or not MENU.exists():
        return 0
    raw = MENU.read_text()
    data = json.loads(raw)
    n = apply(data, fixes)
    if n:
        compact = "\n" not in raw.strip()
        MENU.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")) if compact
                        else json.dumps(data, ensure_ascii=False, indent=1))
    return n


_SUITE = re.compile(r"\s*(,\s*)?(ste|suite|unit|apt|#)\b[\s.#-]*[a-z0-9-]+", re.I)


def geocode(address: str) -> tuple[float, float] | None:
    """US address -> (lat, lon) with the Census Bureau's free geocoder (suite/unit dropped first). Cached."""
    cache = json.loads(GEO_CACHE.read_text()) if GEO_CACHE.exists() else {}
    if address in cache:
        return tuple(cache[address]) if cache[address] else None
    q = re.sub(r"\s+", " ", _SUITE.sub("", address)).strip()
    url = ("https://geocoding.geo.census.gov/geocoder/locations/onelineaddress?benchmark=Public_AR_Current&format=json&address="
           + urllib.parse.quote(q))
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "knewdle-data (info@swats.rocks)"}),
                                    timeout=30) as r:
            m = (json.load(r).get("result") or {}).get("addressMatches") or []
    except Exception as e:  # noqa: BLE001
        print(f"  ! geocoder: {type(e).__name__}", file=sys.stderr)
        return None
    spot = (round(float(m[0]["coordinates"]["y"]), 6), round(float(m[0]["coordinates"]["x"]), 6)) if m else None
    cache[address] = list(spot) if spot else None
    GEO_CACHE.parent.mkdir(exist_ok=True)
    GEO_CACHE.write_text(json.dumps(cache, indent=1, sort_keys=True))
    return spot


def hand_fixes() -> list[dict]:
    """address_fixes.txt: "Name | lat, lon (as listed now) | right address [| lat, lon]" per line."""
    out = []
    if not FIX_LIST.exists():
        return out
    for raw in FIX_LIST.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        try:
            name, (la, lo), addr = parts[0], [float(x) for x in parts[1].split(",")], parts[2]
            spot = tuple(float(x) for x in parts[3].split(",")) if len(parts) > 3 else geocode(addr)
        except (IndexError, ValueError):
            print(f"  ! address_fixes.txt: couldn't read: {raw!r}", file=sys.stderr)
            continue
        if not spot:
            print(f"  ! address_fixes.txt: couldn't find {addr!r} on the map", file=sys.stderr)
            continue
        out.append({"n": name, "la": la, "lo": lo, "a": addr, "nla": spot[0], "nlo": spot[1]})
    return out


def details() -> list[dict]:
    """shop_details.txt: "Name as listed | lat, lon | better name | website" per line (name or website may be blank)."""
    out = []
    if not DETAILS_LIST.exists():
        return out
    for raw in DETAILS_LIST.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")] + ["", ""]
        try:
            la, lo = (float(x) for x in parts[1].split(","))
        except ValueError:
            print(f"  ! shop_details.txt: couldn't read: {raw!r}", file=sys.stderr)
            continue
        site = parts[3]
        if site and not site.startswith("http"):
            site = "https://" + site
        out.append({"n": parts[0], "la": la, "lo": lo, "name": parts[2], "w": site})
    return out


def apply_details(data: dict, fixes: list[dict]) -> int:
    """Sets a better name and/or the website on matching listings (matched by listed or new name + spot)."""
    changed = 0
    for part in ("places", "more"):
        for p in data.get(part, []):
            for f in fixes:
                names = {(f.get("n") or "").strip().lower(), (f.get("name") or "").strip().lower()} - {""}
                try:
                    near = abs(float(p["la"]) - f["la"]) < NEAR and abs(float(p["lo"]) - f["lo"]) < NEAR
                except (KeyError, TypeError, ValueError):
                    near = False
                if not near or (p.get("n") or "").strip().lower() not in names:
                    continue
                if f.get("name") and p.get("n") != f["name"]:
                    p["n"] = f["name"]; changed += 1
                if f.get("w") and p.get("w") != f["w"]:
                    p["w"] = f["w"]; changed += 1
    return changed


def apply_details_file() -> int:
    fixes = details()
    if not fixes or not MENU.exists():
        return 0
    raw = MENU.read_text()
    data = json.loads(raw)
    n = apply_details(data, fixes)
    if n:
        compact = "\n" not in raw.strip()
        MENU.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")) if compact
                        else json.dumps(data, ensure_ascii=False, indent=1))
    return n


_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _day(tok: str) -> int | None:
    t = tok.strip().lower()[:3]
    t = {"tu": "tue", "th": "thu", "we": "wed", "mo": "mon", "fr": "fri", "sa": "sat", "su": "sun"}.get(t[:2], t) if len(t) < 3 else t
    return _DAYS.index(t) if t in _DAYS else None


def _clock(tok: str) -> int | None:
    m = re.fullmatch(r"\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm|a|p)?\s*", tok.lower())
    if not m:
        return {"noon": 720, "midnight": 1440}.get(tok.strip().lower())
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "")[:1]
    if ap == "p" and h < 12: h += 12
    if ap == "a" and h == 12: h = 0
    return h * 60 + mi if 0 <= h <= 24 and mi < 60 else None


def parse_hand_hours(text: str) -> list | None:
    """"Fri-Sat 5pm-9pm; Sun 12pm-3pm, 5pm-8pm" -> hours.json week (Monday first). None if anything is unclear."""
    week = [[] for _ in range(7)]
    for seg in [x.strip() for x in text.split(";") if x.strip()]:
        m = re.search(r"\d|noon|midnight", seg, re.I)
        if not m:
            return None
        days_part, times_part = seg[:m.start()], seg[m.start():]
        days: list[int] = []
        for chunk in re.split(r"\s*(?:,|&|\band\b)\s*", days_part.strip(), flags=re.I):
            if not chunk:
                continue
            ab = re.split(r"\s*(?:-|–|to|thru|through)\s*", chunk, flags=re.I)
            a, b = _day(ab[0]), _day(ab[-1])
            if a is None or b is None:
                return None
            days += [(a + i) % 7 for i in range(((b - a) % 7) + 1)]
        if not days:
            return None
        for span in re.split(r"\s*,\s*", times_part):
            oc = re.split(r"\s*(?:-|–|to)\s*", span, flags=re.I)
            if len(oc) != 2:
                return None
            o, c = _clock(oc[0]), _clock(oc[1])
            if o is None or c is None or o >= 1440:
                return None
            if c <= o:
                c += 1440                      # closes after midnight
            if c > 1440 + 360:
                return None
            for d in days:
                week[d].append([o, c])
    return week if any(week) else None


def _page_key(site: str) -> str:
    s = re.sub(r"^https?://", "", site.strip().lower()).removeprefix("www.").rstrip("/")
    return s


def hand_hours() -> dict[str, list]:
    """hours_by_hand.txt: "website or page | hours" per line -> {key: week}."""
    out = {}
    if not HOURS_LIST.exists():
        return out
    for raw in HOURS_LIST.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or "|" not in line:
            continue
        site, spec = [x.strip() for x in line.split("|", 1)]
        week = parse_hand_hours(spec)
        if not site or week is None:
            print(f"  ! hours_by_hand.txt: couldn't read: {raw!r}", file=sys.stderr)
            continue
        out[_page_key(site)] = week
    return out


def merge_hand_hours(shops: dict, pages: dict, hand: dict | None = None) -> int:
    """Puts hand hours into hours.json's maps: a bare website goes in "shops", a page (with a path) in "pages"."""
    hand = hand_hours() if hand is None else hand
    changed = 0
    for key, week in hand.items():
        target = pages if "/" in key else shops
        if target.get(key) != week:
            target[key] = week
            changed += 1
    return changed


def apply_hand_hours_file() -> int:
    hand = hand_hours()
    if not hand or not HOURS_JSON.exists():
        return 0
    data = json.loads(HOURS_JSON.read_text())
    n = merge_hand_hours(data.setdefault("shops", {}), data.setdefault("pages", {}), hand)
    if n:
        HOURS_JSON.write_text(json.dumps(data, separators=(",", ":")))
    return n
