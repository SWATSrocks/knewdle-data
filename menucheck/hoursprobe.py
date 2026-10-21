"""One-off: why do shop websites have no hours we can read? Samples sites and chains, saves state/hours_probe.json."""
from __future__ import annotations

import json
import random
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from . import hours as H
from .claims import listings, load_json, STATE, find_hours
from .crawl import Crawler
from .places import host_of

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "state" / "hours_probe.json"
LOCATION_URL = re.compile(r"/(locations?|stores?|restaurants?|find-us|visit)/[^/?#]+", re.I)


def windows(text: str, n=4):
    out = []
    for m in re.finditer(r"hours|open(?:ing)?\b|mon(?:day)?\b|tue(?:s|sday)?\b", text, re.I):
        out.append(text[max(0, m.start() - 80): m.start() + 260])
        if len(out) >= n: break
    return out


def probe_site(c: Crawler, host: str) -> dict:
    home = c.fetch_home(host)
    if home is None:
        return {"h": host, "reached": False}
    soup = BeautifulSoup(home.content, "html.parser")
    ld = len(soup.find_all("script", type=lambda v: v and "ld+json" in v))
    for t in soup(["script", "style", "noscript"]): t.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    week, how = find_hours(c, home)
    imgs = [i.get("alt", "") for i in soup.find_all("img") if re.search(r"hour", (i.get("alt", "") + i.get("src", "")), re.I)]
    gen = soup.find("meta", attrs={"name": "generator"})
    return {"h": host, "reached": True, "url": home.url, "len": len(text), "ld": ld, "found": how,
            "gen": gen.get("content") if gen else None, "hour_imgs": imgs[:3], "win": windows(text)}


def probe_chain(c: Crawler, host: str, urls: list[str]) -> dict:
    paths = sorted({u for u in urls if urlparse(u).path.strip("/")})
    maps = c.sitemaps("https://" + host)
    locs = []
    for sm in maps[:3]:
        r = c.fetch(sm)
        if r is None: continue
        found = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", r.text)
        subs = [u for u in found if u.endswith(".xml")]
        for s in subs[:6]:
            rr = c.fetch(s)
            if rr is not None: found += re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", rr.text)
        locs += [u for u in found if LOCATION_URL.search(u)]
    tests = []
    for u in (paths[:2] + locs[:2]):
        r = c.fetch(u)
        if r is None:
            tests.append({"u": u, "ok": False}); continue
        w, how = H.parse(r.content)
        soup = BeautifulSoup(r.content, "html.parser")
        geo = bool(re.search(r'"(latitude|geo)"', r.text))
        tests.append({"u": u, "ok": True, "how": how, "geo": geo, "fmt": H.fmt(w) if w else None})
    return {"h": host, "listing_paths": len(paths), "sitemaps": len(maps), "location_urls": len(set(locs)), "tests": tests}


def main():
    c = Crawler("https://github.com/SWATSrocks/knewdle-data")
    st = load_json(STATE, {})
    hrs, health = st.get("hours", {}), st.get("site_health", {})
    b = listings()
    single = [h for h in b if len(b[h]) == 1 and h not in hrs and h not in health]
    random.seed(7)
    sample = random.sample(single, min(160, len(single)))
    # full listing URLs for chains
    urls: dict[str, list[str]] = {}
    ex = load_json(ROOT / "state/extra_state.json", {})
    srcs = [p.get("website") for p in ex.get("app_known", [])] + [p.get("w") for p in ex.get("published", [])]
    srcs += [s.get("website") for s in load_json(ROOT / "state/menu_state.json", {"places": {}})["places"].values()]
    for u in srcs:
        h = host_of(u or "")
        if h: urls.setdefault(h, []).append(u.strip())
    chains = sorted((h for h in b if len(b[h]) > 1), key=lambda h: -len(b[h]))[:40]
    with ThreadPoolExecutor(12) as pool:
        sites = list(pool.map(lambda h: probe_site(c, h), sample))
        chain = list(pool.map(lambda h: probe_chain(c, h, urls.get(h, [])), chains))
    for x in chain: x["n"] = len(b[x["h"]])
    OUT.write_text(json.dumps({"sites": sites, "chains": chain}, indent=1, ensure_ascii=False))
    r = [s for s in sites if s["reached"]]
    print(len(sites), "sampled;", len(r), "reached;", sum(1 for s in r if s["found"]), "found now")


if __name__ == "__main__":
    main()
