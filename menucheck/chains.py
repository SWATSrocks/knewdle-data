"""Ramen chains' own store locators (see chains.json).

Each chain's website is read politely: robots.txt honoured (including Crawl-delay), only the chain's own
domain, at most MAX_PAGES pages per chain per run. Only facts are kept: the chain's name and each
location's address and map point. Locations marked "coming soon" are skipped until they open.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .crawl import Crawler
from .places import Geocoder, Location, RAMEN_WORD, find_locations, host_of

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "chains.json"
MAX_PAGES = 150
MAX_DELAY = 20          # a site asking for more than this between requests gets fewer pages instead
LOCATION_HINT = re.compile(r"location|find[- ]?us|visit|stores?\b|restaurants|our[- ]shops|shops\b|branches", re.IGNORECASE)


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _links(html: bytes, base: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for a in soup.find_all("a", href=True):
        href = urljoin(base, a["href"]).split("#")[0]
        if href.startswith("http"):
            out.append((href, " ".join(a.get_text(" ", strip=True).split())[:80]))
    return out


def _sitemap_urls(crawler: Crawler, url: str, depth: int = 0) -> list[str]:
    if url.endswith(".gz"):
        return []
    r = crawler.fetch(url)
    if r is None:
        return []
    locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", r.text if hasattr(r, "text") else "")
    if depth == 0 and "<sitemapindex" in r.text:
        out = []
        for sm in locs[:40]:
            out += _sitemap_urls(crawler, sm, 1)
        return out
    return locs[:5000]


def read_chain(crawler: Crawler, chain: dict, geocode: Geocoder, log=print) -> list[dict]:
    site_host = host_of(chain["site"])
    pattern = re.compile(chain["pages"], re.IGNORECASE) if chain.get("pages") else None
    require = re.compile(rf"(?<![a-z]){re.escape(chain['require'])}", re.IGNORECASE) if chain.get("require") else None
    delay = min(crawler.crawl_delay(chain["site"]), MAX_DELAY)
    budget = MAX_PAGES if delay <= 5 else max(10, int(MAX_PAGES * 5 / delay))

    found: dict[str, Location] = {}
    pages_read = 0
    to_read: list[str] = []
    seen: set[str] = set()

    def take(locs: list[Location], text: str, per_page: bool):
        for loc in locs:
            if loc.soon:
                continue
            if require and per_page and not require.search(text):
                continue
            found.setdefault(loc.key, loc)

    for start in chain["start"]:
        if pages_read >= budget:
            break
        seen.add(start)
        r = crawler.fetch(start)
        pages_read += 1
        if r is None:
            log(f"    {chain['name']}: couldn't read {start} (not allowed or unavailable)")
            continue
        locs, text, _ = find_locations(r.content)
        if require:
            # A group's page listing several kinds of restaurant: keep addresses in a ramen section.
            from .places import addresses_in_text
            keep = set()
            for loc, pos in addresses_in_text(text):
                if require.search(text[max(0, pos - 300): pos + 200]):
                    keep.add(loc.key)
            locs = [l for l in locs if l.key in keep]
        take(locs, text, per_page=False)
        for href, label in _links(r.content, r.url):
            h = host_of(href)
            if h != site_host and not h.endswith("." + site_host):
                continue
            if pattern and pattern.search(href):
                to_read.append(href)
            elif chain.get("follow") and (LOCATION_HINT.search(label) or LOCATION_HINT.search(urlparse(href).path)):
                to_read.append(href)
        time.sleep(delay)

    if pattern:
        for sm in list(dict.fromkeys(chain.get("sitemaps", []) + crawler.sitemaps(chain["site"]))):
            to_read += [u for u in _sitemap_urls(crawler, sm) if pattern.search(u)]

    for url in dict.fromkeys(to_read):
        if pages_read >= budget:
            log(f"    {chain['name']}: stopped at {budget} pages")
            break
        if url in seen:
            continue
        seen.add(url)
        time.sleep(delay)
        r = crawler.fetch(url)
        pages_read += 1
        if r is None:
            continue
        locs, text, _ = find_locations(r.content)
        if len(locs) > 1 and pattern:
            # A per-location page often also shows the other stores in its footer: the first address
            # on the page (or the one in its structured data) is this page's store.
            locs = locs[:1]
        take(locs, text, per_page=True)

    out = []
    for loc in found.values():
        if not geocode.locate(loc):
            continue
        out.append({
            "id": f"chain_{_slug(chain['name'])}_{loc.key}",
            "name": chain["name"],
            "lat": loc.lat,
            "lon": loc.lon,
            "address": loc.address,
            "website": chain["site"],
            "f": "chain",
        })
    log(f"  {chain['name']}: {len(out)} US locations ({pages_read} pages read)")
    return out


def read_all(crawler: Crawler, geocode: Geocoder, only: str | None = None, log=print) -> dict[str, list[dict]]:
    chains = json.loads(CONFIG.read_text())["chains"]
    results = {}
    for chain in chains:
        if only and only.lower() not in chain["name"].lower():
            continue
        try:
            results[chain["name"]] = read_chain(crawler, chain, geocode, log)
        except Exception as e:  # noqa: BLE001 - one chain's site trouble mustn't stop the rest
            log(f"  {chain['name']}: failed ({type(e).__name__}: {e})")
    return results


__all__ = ["read_all", "read_chain", "RAMEN_WORD"]
