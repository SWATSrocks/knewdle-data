"""City pages: one web page per US city with ramen, built from the same data the app uses.

    python -m menucheck.citypages [--dry-run]

Writes docs/ramen/ (GitHub Pages): an index of states, a page per state, and a page per city with 3+ ramen spots
("Ramen in Charlotte, NC"), plus docs/sitemap.xml. Each page lists the spots with live "Open now" status (worked out in
the visitor's browser from the shop's own posted hours, in the shop's time zone) and a Get-the-app button.

People search Google for "ramen near me" / "ramen open now <city>" every day; these pages answer that and send them
to the app. Files are only rewritten when their content changes, so daily runs don't churn the repo.
"""
from __future__ import annotations

import argparse
import html
import json
import math
import re
import unicodedata
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote, urlparse

from .crawl import is_blocked_host

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
OUT = DOCS / "ramen"
SITE = "https://swatsrocks.github.io/knewdle-data"
PLAY = "https://play.google.com/store/apps/details?id=com.swatsrocks.knewdle"
MIN_CITY = 3          # a city gets its own page with this many spots (smaller towns are listed on the state page)
NEARBY_MILES = 15

STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "DC": "Washington, DC", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii",
    "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming", "PR": "Puerto Rico",
}
NAME_TO_ABBR = {v.lower(): k for k, v in STATES.items()} | {"washington dc": "DC", "district of columbia": "DC"}

E, C, M, P = "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles"
STATE_TZ = {
    **{s: E for s in "CT DE DC GA ME MD MA NH NJ NY NC OH PA RI SC VT VA WV".split()},
    **{s: C for s in "AL AR IL IA LA MN MS MO OK WI".split()},
    **{s: M for s in "CO MT NM UT WY".split()},
    **{s: P for s in "CA NV WA".split()},
    "AZ": "America/Phoenix", "HI": "Pacific/Honolulu", "AK": "America/Anchorage", "PR": "America/Puerto_Rico",
}


def tz_for(state: str, lat: float, lon: float) -> str:
    """The shop's time zone (states split between zones use a rough longitude/latitude line)."""
    if state == "FL": return C if lon < -85.0 and lat > 29.5 else E
    if state == "TN": return E if lon > -85.4 else C
    if state == "KY": return E if lon > -86.2 else C
    if state == "IN": return C if (lon < -86.9 and (lat > 41.0 or lat < 38.4)) else E
    if state == "MI": return C if lon < -87.5 and lat > 45.0 else E
    if state == "TX": return M if lon < -104.8 else C
    if state in ("KS", "NE"): return M if lon < -101.4 else C
    if state in ("ND", "SD"): return M if lon < -100.6 else C
    if state == "ID": return P if lat > 45.6 else M
    if state == "OR": return M if lon < -117.4 and lat < 44.6 else P
    return STATE_TZ.get(state, E)


# ---------------------------------------------------------------- data

def _norm_name(n: str) -> str:
    n = unicodedata.normalize("NFKD", n or "").encode("ascii", "ignore").decode().lower()
    n = re.sub(r"\b(ramen|restaurant|house|bar|shop|noodles?|kitchen|the|and|sushi|japanese|cuisine|izakaya)\b", " ", n)
    return re.sub(r"[^a-z0-9]+", "", n)


def host_of(w: str | None) -> str | None:
    if not w:
        return None
    u = w if "://" in w else "https://" + w
    h = (urlparse(u).hostname or "").lower().removeprefix("www.")
    return h or None


def page_key(w: str | None) -> str | None:
    if not w:
        return None
    u = urlparse(w if "://" in w else "https://" + w)
    h = (u.hostname or "").lower().removeprefix("www.")
    path = (u.path or "").rstrip("/").lower()
    if not h or not path or re.search(r"\.(pdf|jpe?g|png)$", path):
        return None
    return h + path


def miles(a: tuple[float, float], b: tuple[float, float]) -> float:
    dlat = (a[0] - b[0]) * 69.0
    dlon = (a[1] - b[1]) * 69.0 * math.cos(math.radians((a[0] + b[0]) / 2))
    return math.hypot(dlat, dlon)


def split_address(a: str | None) -> tuple[str, str, str] | None:
    """"123 Main St, Charlotte, NC" -> ("123 Main St", "Charlotte", "NC")."""
    if not a:
        return None
    parts = [p.strip() for p in a.split(",") if p.strip()]
    if len(parts) < 3:
        return None
    st = re.sub(r"\s*\d{5}(?:-\d{4})?$", "", parts[-1]).strip()
    st = st.upper() if len(st) == 2 else NAME_TO_ABBR.get(st.lower(), "")
    if st not in STATES:
        return None
    city = parts[-2]
    if re.search(r"\d", city):   # "Suite 100" slipped into the city slot
        if len(parts) < 4 or re.search(r"\d", parts[-3]):
            return None
        city = parts[-3]
    city = " ".join(w if w.isupper() and len(w) <= 3 else w.capitalize() if w.islower() or w.isupper() else w
                    for w in city.split())
    return ", ".join(parts[:-2]), city, st


def load_spots() -> list[dict]:
    """Every ramen spot the app shows (minus closed ones), once each."""
    extra = json.loads((ROOT / "state" / "extra_state.json").read_text())
    menu = json.loads((DOCS / "menu_ramen.json").read_text())
    osm = json.loads((DOCS / "osm_ramen.json").read_text())
    hours = json.loads((DOCS / "hours.json").read_text())
    closed = json.loads((DOCS / "closed.json").read_text())

    raw: list[dict] = []
    for p in extra.get("app_known", []):
        raw.append({"n": p["name"], "la": p["lat"], "lo": p["lon"], "a": p.get("address"), "w": p.get("website"),
                    "kind": "shop"})
    for p in menu.get("places", []):
        raw.append({"n": p["n"], "la": p["la"], "lo": p["lo"], "a": p.get("a"), "w": p.get("w"),
                    "kind": "shop" if p.get("k") == "shop" else "menu"})
    for p in menu.get("more", []):
        raw.append({"n": p["n"], "la": p["la"], "lo": p["lo"], "a": p.get("a"), "w": p.get("w"),
                    "kind": "hall" if p.get("f") == "hall" else "shop", "in": p.get("in"), "soon": bool(p.get("so"))})
    for e in osm.get("elements", []):
        t = e.get("tags", {})
        lat, lon = e.get("lat") or (e.get("center") or {}).get("lat"), e.get("lon") or (e.get("center") or {}).get("lon")
        if not (t.get("name") and lat and t.get("addr:city") and t.get("addr:street")):
            continue
        st = t.get("addr:state", "")
        st = st.upper() if len(st) == 2 else NAME_TO_ABBR.get(st.lower(), "")
        if st not in STATES:
            continue
        street = " ".join(x for x in (t.get("addr:housenumber"), t.get("addr:street")) if x)
        raw.append({"n": t["name"], "la": lat, "lo": lon, "a": f"{street}, {t['addr:city']}, {st}",
                    "w": t.get("website") or t.get("contact:website"), "kind": "shop", "osm": True})

    closed_hosts = {host_of(c.get("w")) for c in closed.get("shops", []) if c.get("w")} | \
                   {c["h"] for c in closed.get("shops", []) if c.get("h")}
    closed_hosts.discard(None)
    closed_pts = [(c["la"], c["lo"], _norm_name(c.get("n", ""))) for c in closed.get("shops", []) if "la" in c]
    for c in closed.get("shops", []):
        m = re.match(r"^(.*)@(-?\d+\.\d+),(-?\d+\.\d+)$", c.get("k", ""))
        if m:
            closed_pts.append((float(m.group(2)), float(m.group(3)), _norm_name(c.get("n", ""))))

    def is_closed(s: dict) -> bool:
        nn = _norm_name(s["n"])
        for la, lo, cn in closed_pts:
            if abs(la - s["la"]) < 0.01 and abs(lo - s["lo"]) < 0.01 and miles((la, lo), (s["la"], s["lo"])) < 0.15 \
                    and (not cn or not nn or cn == nn or cn in nn or nn in cn):
                return True
        h = host_of(s.get("w"))
        # A closed website only hides single-location shops on it (a chain's site stays up for its other stores).
        return bool(h and h in closed_hosts and sum(1 for x in raw if host_of(x.get("w")) == h) == 1)

    by_cell: dict[tuple[int, int], list[dict]] = defaultdict(list)
    spots: list[dict] = []
    for s in raw:
        try:
            s["la"], s["lo"] = float(s["la"]), float(s["lo"])
        except (TypeError, ValueError):
            continue
        addr = split_address(s.get("a"))
        if not addr or is_closed(s):
            continue
        s["street"], s["city"], s["st"] = addr
        cell = (int(s["la"] * 200), int(s["lo"] * 200))   # ~550 m cells
        nn, h = _norm_name(s["n"]), host_of(s.get("w"))
        dupe = None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for o in by_cell.get((cell[0] + dx, cell[1] + dy), []):
                    d = miles((o["la"], o["lo"]), (s["la"], s["lo"]))
                    on = _norm_name(o["n"])
                    same_name = nn and on and (nn == on or (len(nn) >= 4 and (nn in on or on in nn)))
                    if d < 0.12 and (same_name or (h and h == host_of(o.get("w")))) or d < 0.02:
                        dupe = o
        if dupe:
            # Keep the most useful facts from both listings.
            if dupe["kind"] == "menu" and s["kind"] == "shop":
                dupe["kind"] = "shop"
            if not dupe.get("w") and s.get("w"):
                dupe["w"] = s["w"]
            continue
        by_cell[cell].append(s)
        spots.append(s)

    for s in spots:
        wk = (hours.get("pages", {}).get(page_key(s.get("w")) or "") or
              hours.get("shops", {}).get(host_of(s.get("w")) or ""))
        s["h"] = wk if isinstance(wk, list) and len(wk) == 7 else None
        s["tz"] = tz_for(s["st"], s["la"], s["lo"])
        w = s.get("w")
        s["site"] = w if w and not is_blocked_host(w if "://" in w else "https://" + w) else None
    return spots


# ---------------------------------------------------------------- page bits

def slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-") or "city"


def esc(s) -> str:
    return html.escape(str(s or ""), quote=True)


def clock(m: int) -> str:
    m %= 1440
    if m == 0: return "midnight"
    if m == 720: return "noon"
    h, mi = divmod(m, 60)
    return f"{h % 12 or 12}{':%02d' % mi if mi else ''} {'AM' if h < 12 else 'PM'}"


DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def pretty_hours(h: list) -> list[str]:
    rows, i = [], 0
    while i < 7:
        j = i
        while j + 1 < 7 and h[j + 1] == h[i]:
            j += 1
        label = DAYS[i] if i == j else f"{DAYS[i]}–{DAYS[j]}"
        times = "Closed" if not h[i] else ", ".join(f"{clock(o)}–{clock(c)}" for o, c in h[i])
        rows.append(f"<b>{label}</b> {esc(times)}")
        i = j + 1
    return rows


def late_night(h: list | None) -> bool:
    return bool(h) and any(c >= 22 * 60 for day in h for _, c in day)


def play_link(medium: str) -> str:
    return PLAY + "&referrer=" + quote(f"utm_source=web&utm_medium={medium[:32]}", safe="")


BOWL_SVG = ('<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 11h18a9 9 0 0 1-18 0z"/>'
            '<path d="M13 3l-2 6M18 3.5l-4 5.5"/></svg>')
PLAY_SVG = ('<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 3.5v17l10-8.5z" fill="#3DAA5C"/>'
            '<path d="M4 3.5l10 9 3.2-2.8L6.3 2.6c-.9-.5-1.7-.3-2.3.9z" fill="#4285F4"/>'
            '<path d="M4 20.5l10-8 3.2 2.8-10.9 6.1c-.9.5-1.7.3-2.3-.9z" fill="#E63946"/>'
            '<path d="M17.2 9.7l3 1.7c1 .6 1 1.6 0 2.2l-3 1.7-3.2-2.8z" fill="#FFC93C"/></svg>')


def logo(href: str) -> str:
    return (f'<a class="logo" href="{href}" aria-label="Knewdle NOW ramen pages">'
            '<span class="word">Knewdle</span><span class="now">NOW</span></a>')


def head(title: str, desc: str, url: str, depth: int, jsonld: dict | None = None) -> str:
    a = "../" * depth + "assets/"
    ld = f'<script type="application/ld+json">{json.dumps(jsonld, ensure_ascii=False, separators=(",", ":"))}</script>' if jsonld else ""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<meta name="description" content="{esc(desc)}">
<link rel="canonical" href="{url}">
<meta property="og:title" content="{esc(title)}">
<meta property="og:description" content="{esc(desc)}">
<meta property="og:url" content="{url}">
<meta property="og:type" content="website">
<meta property="og:image" content="{SITE}/ramen/assets/share.png">
<meta name="theme-color" content="#FFC93C">
<link rel="icon" href="{a}icon.svg" type="image/svg+xml">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@500;700;800;900&display=swap" rel="stylesheet">
<link rel="stylesheet" href="{a}site.css">
{ld}
</head>
<body>"""


def topbar(depth: int, medium: str) -> str:
    return f"""<header class="top">{logo("../" * depth or "./")}
<a class="cta-sm" href="{play_link(medium)}">{PLAY_SVG}<span>Get the app</span></a></header>"""


def get_app(depth: int, medium: str, where: str) -> str:
    a = "../" * depth + "assets/"
    return f"""<section class="getapp">
  <div class="ga-text">
    <div class="kicker">FREE ON GOOGLE PLAY</div>
    <h2>Find ramen open <em>right now</em>{(' in ' + esc(where)) if where else ''}</h2>
    <ul>
      <li><b>Open now, at a glance.</b> Live status from each shop's own hours.</li>
      <li><b>Hidden ramen too.</b> We read menus, so the sushi bar with one great bowl shows up.</li>
      <li><b>Map mode, Ramen Passport &amp; 64 style badges.</b> Stamp shops in person and collect them all.</li>
    </ul>
    <a class="cta" href="{play_link(medium)}">{PLAY_SVG}<span><small>GET IT ON</small>Google Play</span></a>
  </div>
  <div class="qr"><img src="{a}qr.png" width="168" height="168" alt="QR code: Knewdle NOW on Google Play"><span>Scan with your phone</span></div>
</section>"""


def footer(depth: int) -> str:
    r = "../" * depth
    return f"""<footer>
  <p><a href="{r}">All ramen cities</a> · <a href="{SITE}/shops/">Own a ramen shop? Claim your listing</a> · <a href="{SITE}/">About the data</a></p>
  <p class="credits">Places from Overture Maps Foundation (CDLA-Permissive-2.0), OpenStreetMap (© OpenStreetMap contributors, ODbL)
  and Foursquare Open Source Places (Apache 2.0). Hours as posted on each shop's own website; always worth a quick call before a long drive.
  Knewdle NOW is made by SWATS Rocks · <a href="mailto:info@swats.rocks">info@swats.rocks</a></p>
</footer>
<script src="{r}assets/site.js" defer></script>
</body></html>
"""


def card(s: dict, show_city: bool = False) -> str:
    kind = {"shop": ("Ramen shop", "k-shop"), "menu": ("Ramen on the menu", "k-menu"),
            "hall": (f"Inside {s.get('in') or 'a food hall'}", "k-hall")}[s["kind"]]
    pills = [f'<span class="pill {kind[1]}">{esc(kind[0])}</span>']
    if s.get("soon"):
        pills.append('<span class="pill k-soon">Coming soon</span>')
    if late_night(s["h"]):
        pills.append('<span class="pill k-late">Late night</span>')
    addr = f"{s['street']}, {s['city']}, {s['st']}"
    q = quote(f"{s['n']}, {addr}")
    links = [f'<a class="btn" href="https://www.google.com/maps/search/?api=1&amp;query={q}" rel="nofollow">Directions</a>']
    if s.get("site"):
        links.append(f'<a class="btn ghost" href="{esc(s["site"])}" rel="nofollow noopener">Website</a>')
    hours_html = ""
    data = ""
    if s["h"]:
        hours_html = f'<details class="hours"><summary>Hours</summary><p>{"<br>".join(pretty_hours(s["h"]))}</p></details>'
        data = f' data-h="{esc(json.dumps(s["h"], separators=(",", ":")))}" data-tz="{s["tz"]}"'
    return f"""<article class="spot"{data} data-kind="{s['kind']}">
  <div class="spot-top"><h3>{esc(s['n'])}</h3><span class="status">{'' if s['h'] else '<i>Hours not posted</i>'}</span></div>
  <p class="addr">{esc(addr)}</p>
  <div class="pills">{''.join(pills)}</div>
  {hours_html}
  <div class="links">{''.join(links)}</div>
</article>"""


def jsonld_list(name: str, url: str, spots: list[dict]) -> dict:
    items = []
    for i, s in enumerate(spots[:50]):
        r = {"@type": "Restaurant", "name": s["n"], "servesCuisine": "Ramen",
             "address": {"@type": "PostalAddress", "streetAddress": s["street"], "addressLocality": s["city"],
                         "addressRegion": s["st"], "addressCountry": "US"},
             "geo": {"@type": "GeoCoordinates", "latitude": round(s["la"], 5), "longitude": round(s["lo"], 5)}}
        if s.get("site"):
            r["url"] = s["site"]
        items.append({"@type": "ListItem", "position": i + 1, "item": r})
    return {"@context": "https://schema.org", "@type": "CollectionPage", "name": name, "url": url,
            "mainEntity": {"@type": "ItemList", "numberOfItems": len(spots), "itemListElement": items}}


def sort_spots(spots: list[dict]) -> list[dict]:
    # Ramen shops first, then hidden-menu spots; within each, ones with posted hours first, then A–Z.
    order = {"shop": 0, "hall": 1, "menu": 2}
    return sorted(spots, key=lambda s: (order[s["kind"]], s["h"] is None, s["n"].lower()))


def city_page(city: str, st: str, spots: list[dict], nearby: list[dict], others: list[tuple[str, str, int]]) -> str:
    where = f"{city}, {st}"
    url = f"{SITE}/ramen/{st.lower()}/{slug(city)}/"
    n = len(spots)
    shops = sum(1 for s in spots if s["kind"] != "menu")
    hidden = n - shops
    late = sum(1 for s in spots if late_night(s["h"]))
    title = f"Ramen in {where}: {n} spots, open now | Knewdle NOW"
    desc = (f"{n} ramen spots in {where}" + (f", including {hidden} with hidden ramen on the menu" if hidden else "") +
            ". See which are open right now, hours, late-night ramen and directions.")
    medium = f"{st.lower()}-{slug(city)}"
    body = [head(title, desc, url, 2, jsonld_list(f"Ramen in {where}", url, spots)), topbar(2, medium)]
    facts = [f"<b>{n}</b> ramen spot{'s' if n != 1 else ''}"]
    if hidden:
        facts.append(f"<b>{hidden}</b> hidden on the menu")
    if late:
        facts.append(f"<b>{late}</b> open late")
    body.append(f"""<main>
<section class="hero">
  <nav class="crumbs"><a href="../../">Ramen</a> › <a href="../">{esc(STATES[st])}</a> › {esc(city)}</nav>
  <div class="kicker">RAMEN IN</div>
  <h1>{esc(city)}<span class="st">, {st}</span></h1>
  <p class="facts">{' · '.join(facts)} · <span class="live" data-live>checking who's open…</span></p>
  <a class="cta" href="{play_link(medium)}">{PLAY_SVG}<span><small>GET IT ON</small>Google Play</span></a>
</section>
<div class="filters" role="group" aria-label="Filter spots">
  <button class="chip on" data-f="all">All</button><button class="chip" data-f="open">Open now</button>
  <button class="chip" data-f="late">Late night</button><button class="chip" data-f="shop">Ramen shops</button>
  <button class="chip" data-f="menu">Hidden on the menu</button>
</div>
<section class="spots" data-list>
{''.join(card(s) for s in sort_spots(spots))}
</section>
<p class="empty" data-empty hidden>Nothing matches right now. Try another filter.</p>""")
    if nearby:
        body.append(f"""<h2 class="sec">Ramen near {esc(city)}</h2>
<section class="spots nearby">{''.join(card(s, True) for s in nearby)}</section>""")
    body.append(get_app(2, medium, where))
    if others:
        links = "".join(f'<a href="../{slug(c)}/">{esc(c)} <i>{k}</i></a>' for c, _, k in others)
        body.append(f'<h2 class="sec">More ramen in {esc(STATES[st])}</h2><nav class="cities">{links}</nav>')
    body.append("</main>" + footer(2))
    return "\n".join(body)


def state_page(st: str, cities: list[tuple[str, int, bool]], small: dict[str, list[dict]], total: int) -> str:
    name = STATES[st]
    url = f"{SITE}/ramen/{st.lower()}/"
    title = f"Ramen in {name}: {total} spots in {len(cities)} cities | Knewdle NOW"
    desc = f"Find ramen across {name}: {total} spots, with live open-now status, hours and directions, city by city."
    medium = f"{st.lower()}-state"
    links = "".join((f'<a href="{slug(c)}/">{esc(c)} <i>{n}</i></a>' if page else "") for c, n, page in cities)
    smalls = ""
    if small:
        smalls = '<h2 class="sec">Smaller towns</h2><section class="spots">' + "".join(
            card(s, True) for c in sorted(small) for s in sort_spots(small[c])) + "</section>"
    return "\n".join([head(title, desc, url, 1), topbar(1, medium), f"""<main>
<section class="hero">
  <nav class="crumbs"><a href="../">Ramen</a> › {esc(name)}</nav>
  <div class="kicker">RAMEN IN</div>
  <h1>{esc(name)}</h1>
  <p class="facts"><b>{total}</b> ramen spots across <b>{len(cities)}</b> cities and towns</p>
  <a class="cta" href="{play_link(medium)}">{PLAY_SVG}<span><small>GET IT ON</small>Google Play</span></a>
</section>
<h2 class="sec">Pick a city</h2>
<nav class="cities big">{links}</nav>
{smalls}
{get_app(1, medium, name)}
</main>""", footer(1)])


def index_page(states: list[tuple[str, int, int]], top: list[tuple[str, str, int]], total: int,
               every: list[tuple[str, str, int]]) -> str:
    url = f"{SITE}/ramen/"
    title = "Ramen open near me, city by city | Knewdle NOW"
    desc = f"{total:,} ramen spots across the US, including hidden ramen on sushi and fusion menus. Find ramen open right now in your city."
    st_links = "".join(f'<a href="{s.lower()}/">{esc(STATES[s])} <i>{n}</i></a>' for s, n, _ in states)
    top_links = "".join(f'<a href="{st.lower()}/{slug(c)}/">{esc(c)}, {st} <i>{n}</i></a>' for c, st, n in top)
    all_links = "".join(f'<a href="{st.lower()}/{slug(c)}/" hidden>{esc(c)}, {st} <i>{n}</i></a>' for c, st, n in every)
    return "\n".join([head(title, desc, url, 0), topbar(0, "index"), f"""<main>
<section class="hero big">
  <div class="kicker">FIND RAMEN</div>
  <h1>Open <em>right now</em></h1>
  <p class="facts"><b>{total:,}</b> ramen spots across the US, including the hidden bowls on sushi, fusion and noodle-house menus.</p>
  <a class="cta" href="{play_link('index')}">{PLAY_SVG}<span><small>GET IT ON</small>Google Play</span></a>
</section>
<h2 class="sec">Find your city</h2>
<label class="find"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.5"/><path d="M15.5 15.5L21 21"/></svg>
<input type="search" data-find placeholder="Charlotte, Austin, Brooklyn…" aria-label="Find your city" autocomplete="off"></label>
<nav class="cities big" data-all hidden>{all_links}</nav>
<nav class="cities big" data-top>{top_links}</nav>
<h2 class="sec">Every state</h2>
<nav class="cities">{st_links}</nav>
{get_app(0, "index", "")}
</main>""", footer(0)])


# ---------------------------------------------------------------- build

def write_if_changed(path: Path, text: str, dry: bool, counter: dict) -> None:
    if path.exists() and path.read_text() == text:
        counter["same"] += 1
        return
    counter["written"] += 1
    if not dry:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def build(dry: bool = False) -> dict:
    spots = load_spots()
    by_city: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for s in spots:
        by_city[(s["st"], s["city"])].append(s)
    # Brooklyn and "brooklyn" are the same city.
    merged: dict[tuple[str, str], list[dict]] = defaultdict(list)
    names: dict[tuple[str, str], str] = {}
    for (st, c), lst in by_city.items():
        k = (st, slug(c))
        merged[k] += lst
        if k not in names or len(lst) > len(by_city.get((st, names[k]), [])):
            names[k] = c
    counter = {"written": 0, "same": 0}
    pages: list[str] = [f"{SITE}/ramen/"]
    wanted: set[Path] = set()
    by_state: dict[str, list[tuple[str, int, bool]]] = defaultdict(list)
    small: dict[str, dict[str, list[dict]]] = defaultdict(dict)
    city_list = sorted(merged.items(), key=lambda kv: -len(kv[1]))
    for (st, sl), lst in city_list:
        page = len(lst) >= MIN_CITY
        by_state[st].append((names[(st, sl)], len(lst), page))
        if not page:
            small[st][names[(st, sl)]] = lst
    for (st, sl), lst in city_list:
        if len(lst) < MIN_CITY:
            continue
        city = names[(st, sl)]
        center = (sum(s["la"] for s in lst) / len(lst), sum(s["lo"] for s in lst) / len(lst))
        inside = {id(s) for s in lst}
        near = sorted((s for s in spots if id(s) not in inside and abs(s["la"] - center[0]) < 0.3
                       and miles(center, (s["la"], s["lo"])) <= NEARBY_MILES),
                      key=lambda s: miles(center, (s["la"], s["lo"])))[:12]
        others = sorted(((c, s2, n) for c, n, p in by_state[st] if p and c != city for s2 in [st]),
                        key=lambda x: -x[2])[:30]
        path = OUT / st.lower() / sl / "index.html"
        wanted.add(path)
        write_if_changed(path, city_page(city, st, lst, near, others), dry, counter)
        pages.append(f"{SITE}/ramen/{st.lower()}/{sl}/")
    states = []
    for st, cities in by_state.items():
        total = sum(n for _, n, _ in cities)
        cities.sort(key=lambda x: (-x[1], x[0]))
        path = OUT / st.lower() / "index.html"
        wanted.add(path)
        write_if_changed(path, state_page(st, cities, small[st], total), dry, counter)
        pages.append(f"{SITE}/ramen/{st.lower()}/")
        states.append((st, total, len(cities)))
    states.sort(key=lambda x: STATES[x[0]])
    top = [(names[k], k[0], len(v)) for k, v in city_list[:24]]
    wanted.add(OUT / "index.html")
    every = sorted(((names[k], k[0], len(v)) for k, v in city_list if len(v) >= MIN_CITY), key=lambda x: (x[0], x[1]))
    write_if_changed(OUT / "index.html", index_page(states, top, len(spots), every), dry, counter)
    # Pages for cities that dropped below the minimum (or closed) are removed.
    removed = 0
    for old in OUT.glob("*/**/index.html") if OUT.exists() else []:
        if old not in wanted:
            removed += 1
            if not dry:
                old.unlink()
                try:
                    old.parent.rmdir()
                except OSError:
                    pass
    sitemap = ('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' +
               "".join(f"<url><loc>{u}</loc></url>\n" for u in sorted(pages)) + "</urlset>\n")
    write_if_changed(DOCS / "sitemap.xml", sitemap, dry, counter)
    stats = {"spots": len(spots), "city_pages": sum(1 for _, l in city_list if len(l) >= MIN_CITY),
             "states": len(states), "removed": removed, **counter}
    print(json.dumps(stats))
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    build(ap.parse_args().dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
