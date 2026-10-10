"""Ramen counters inside food halls, markets and Japanese market food courts (see halls.json).

Maps usually list the building ("Optimist Hall"), not the ramen counter inside it. Each hall's OWN vendor
directory is read politely (robots.txt and Crawl-delay honoured, only the hall's own domain, a capped number
of pages). A vendor counts when its name or its short description on the directory says ramen (or another
ramen word). Vendors share the hall's address, turned into a map point by the free Census geocoder.
Only facts are kept: the vendor's name and which hall it's in.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .crawl import Crawler
from .names import APP_NAME, CHAIN_WORDS, EXTRA_WORDS
from .places import RAMEN_WORD, Geocoder, addresses_in_text, host_of

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "halls.json"
MAX_PAGES = 60
MAX_DELAY = 20
SOON_OR_GONE = re.compile(r"coming soon|opening soon|permanently closed|now closed|has closed|closing", re.IGNORECASE)
NOT_A_VENDOR = re.compile(r"^(?:eat|drink|eat\s*&\s*drink|eat and drink|vendors?|our vendors|directory|tenants?|"
                          r"shops?|dine|dining|restaurants?|food|food hall|menu|menus|hours|events?|visit|about|"
                          r"contact|order|order now|order online|view menu|learn more|more info|website|"
                          r"read more|see more|shop|stores?|map|parking|careers|faq)$", re.IGNORECASE)
# Grocery listings on market pages ("Ramen Cup 3var.", "Shoyu Ramen 5P"), not vendors.
PRODUCT = re.compile(r"\d+\s*(?:var|p|pk|pc|pcs|ct|oz|g|ml|lb)\b\.?|\bcup\b|\bpack\b|\binstant\b|\bbag\b|"
                     r"\bbox\b|\bnoodle soup mix\b|\$\s*\d", re.IGNORECASE)
# Lines that are opening hours, phone numbers or service notes, not vendor names.
NOT_NAME_LINE = re.compile(r"\d{1,2}(?::\d\d)?\s*(?:am|pm)\b|^\(?\d{3}\)?[-. ]\d{3}|\b(?:mon|tue|wed|thu|fri|sat|sun)"
                           r"(?:day)?s?\b|catering|delivery|ordering|order online|takeout|pick ?up|reservations?|"
                           r"gift cards?|parking|available|food stalls|stalls|vendors|neighborhood|location",
                           re.IGNORECASE)
HEADINGS = ["h1", "h2", "h3", "h4", "h5", "h6"]


def ramen_text(s: str) -> bool:
    return bool(RAMEN_WORD.search(s) or APP_NAME.search(s) or EXTRA_WORDS.search(s) or CHAIN_WORDS.search(s))


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _clean(s: str) -> str:
    return " ".join(s.replace(" ", " ").split()).strip(" -–—|:·•")


def _is_name(text: str, hall: str) -> bool:
    return (2 <= len(text) <= 60 and not NOT_A_VENDOR.match(text) and text.lower() != hall.lower()
            and not PRODUCT.search(text) and not NOT_NAME_LINE.search(text)
            and not text.lower().startswith(("welcome", "open ", "hours", "©", "copyright", "follow us")))


def _short_name(name: str) -> str:
    """'MATSUNOKI RAMEN- "Ramen & Fried Chicken"' -> 'MATSUNOKI RAMEN' (drop a tagline after a dash/colon)."""
    return re.split(r"\s*[-–—:|]\s*[\"“]|\s+[–—|]\s+", name)[0].strip() or name


def vendors_on_page(html: bytes, hall: str) -> list[tuple[str, str]]:
    """(vendor name, its short description) for each vendor card on a directory page whose name or
    description says ramen. A card = a heading (or a title-like element) plus the small block around it."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "header", "form"]):
        tag.decompose()
    found: dict[str, str] = {}
    titles = soup.find_all(HEADINGS) + soup.find_all(class_=re.compile(r"title|name|vendor|tenant", re.I))
    for el in titles:
        name = _clean(el.get_text(" ", strip=True))
        if not _is_name(name, hall):
            continue
        # Grow from the title to the smallest surrounding block that holds a description (but not the whole page).
        # Stop before reaching a block that also holds another vendor's title (that's the list, not the card).
        block = el
        for _ in range(4):
            parent = block.parent
            if parent is None or len(parent.get_text(" ", strip=True)) > 700:
                break
            own = set(el.find_all(HEADINGS)) | {el}
            if any(t not in own for t in parent.find_all(HEADINGS)):
                break
            block = parent
        desc = _clean(block.get_text(" ", strip=True))
        if block is el:
            # Flat layout (title, then its description as the next element): take that description.
            nxt = el.find_next_sibling()
            if nxt is not None and nxt.name not in HEADINGS and len(nxt.get_text(" ", strip=True)) <= 300:
                desc = name + " " + _clean(nxt.get_text(" ", strip=True))
        if len(desc) > 700:
            desc = name
        # A card holding several titles is a list, not one vendor: judge by the name alone then.
        if len(block.find_all(HEADINGS)) > 2:
            desc = name
        if not ramen_text(name + " " + desc):
            continue
        if SOON_OR_GONE.search(desc) or PRODUCT.search(desc[:160]):
            continue  # not open yet, or a grocery listing ("Ramen Cup 3var. $1.99")
        found.setdefault(_short_name(name), desc[:200])
    # Page builders (Wix, Squarespace…) often style vendor names as plain text, not headings: also read the page
    # line by line, taking a short name-like line followed by a description that says ramen.
    # Only when the page has no vendor titles at all (otherwise this would pick up section labels).
    lines = [_clean(l) for l in soup.get_text("\n", strip=True).split("\n")] if not found else []
    lines = [l for l in lines if l]
    for i, line in enumerate(lines):
        if not ramen_text(line) or SOON_OR_GONE.search(line):
            continue
        if _is_name(line, hall) and len(line) <= 40 and i + 1 < len(lines) and len(lines[i + 1]) > 25:
            found.setdefault(line, lines[i + 1][:200])      # the name itself says ramen
        elif len(line) > 25:
            for j in (i - 1, i - 2, i - 3):                 # a description: its vendor's name sits just above
                if j < 0:
                    break
                if NOT_NAME_LINE.search(lines[j]):
                    continue                                # skip hours/phone lines between name and description
                if _is_name(lines[j], hall) and len(lines[j]) <= 40 and not lines[j].endswith((".", "!", "?", ",")):
                    found.setdefault(lines[j], line[:200])
                break
    # Same vendor written two ways ("RAMEN SETAGAYA", "Ramen Setagaya"): keep one.
    unique: dict[str, tuple[str, str]] = {}
    for name, desc in found.items():
        unique.setdefault(name.lower(), (name if not name.isupper() else name.title(), desc))
    return list(unique.values())


def vendor_page(html: bytes, hall: str) -> tuple[str, str] | None:
    """A one-vendor page: its name (the page's main heading) if the page's opening text says ramen."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "header", "form"]):
        tag.decompose()
    h = soup.find("h1") or soup.find("h2")
    name = _clean(h.get_text(" ", strip=True)) if h else ""
    if not _is_name(name, hall):
        return None
    text = _clean(soup.get_text(" ", strip=True))
    start = text.find(name)
    opening = text[max(0, start): start + 1500] if start >= 0 else text[:1500]
    if not ramen_text(name + " " + opening) or SOON_OR_GONE.search(opening[:400]):
        return None
    return name, opening[:200]


def read_hall(crawler: Crawler, hall: dict, geocode: Geocoder, log=print) -> list[dict]:
    site = host_of(hall["start"][0])
    pattern = re.compile(hall["pages"], re.IGNORECASE) if hall.get("pages") else None
    delay = min(crawler.crawl_delay(hall["start"][0]), MAX_DELAY)
    budget = int(hall.get("max_pages", MAX_PAGES))
    vendors: dict[str, str] = {}
    to_read: list[str] = []
    pages_read = 0
    for url in hall["start"]:
        r = crawler.fetch(url)
        pages_read += 1
        if r is None:
            log(f"    {hall['name']}: couldn't read {url} (not allowed or unavailable)")
            continue
        for name, desc in vendors_on_page(r.content, hall["name"]):
            vendors.setdefault(name, desc)
        if pattern:
            soup = BeautifulSoup(r.content, "html.parser")
            for a in soup.find_all("a", href=True):
                href = urljoin(r.url, a["href"]).split("#")[0]
                if pattern.search(href) and (host_of(href) == site or host_of(href).endswith("." + site)):
                    to_read.append(href)
        time.sleep(delay)
    if pattern:
        for sm in crawler.sitemaps(hall["start"][0]):
            r = crawler.fetch(sm)
            if r is not None:
                to_read += [u for u in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", r.text) if pattern.search(u)]
    for url in list(dict.fromkeys(to_read)):
        if pages_read >= budget:
            break
        time.sleep(delay)
        r = crawler.fetch(url)
        pages_read += 1
        if r is None:
            continue
        got = vendor_page(r.content, hall["name"])
        if got:
            vendors.setdefault(*got)

    if not vendors:
        log(f"  {hall['name']}: no ramen vendors ({pages_read} pages read)")
        return []
    locs = [l for l, _ in addresses_in_text(hall["address"])]
    if locs and hall.get("point"):
        locs[0].lat, locs[0].lon = hall["point"]  # a fixed map point for addresses the geocoder can't place
    if not locs or not geocode.locate(locs[0]):
        log(f"  {hall['name']}: couldn't place the hall's address ({hall['address']})")
        return []
    loc = locs[0]
    out = []
    for name, desc in vendors.items():
        out.append({
            "id": f"hall_{_slug(hall['name'])}_{_slug(name)}",
            "name": name,
            "lat": loc.lat,
            "lon": loc.lon,
            "address": f"Inside {hall['name']}, {loc.address}",
            "website": hall["start"][0],
            "f": "hall",
            "in": hall["name"],
        })
    log(f"  {hall['name']}: {', '.join(vendors)} ({pages_read} pages read)")
    return out


def read_all(crawler: Crawler, geocode: Geocoder, log=print) -> dict[str, list[dict]]:
    from concurrent.futures import ThreadPoolExecutor
    halls = json.loads(CONFIG.read_text())["halls"]
    results: dict[str, list[dict]] = {}

    def one(h):
        try:
            return h["name"], read_hall(crawler, h, geocode, log)
        except Exception as e:  # noqa: BLE001 - one hall's site trouble mustn't stop the rest
            log(f"  {h['name']}: failed ({type(e).__name__}: {e})")
            return h["name"], None
    with ThreadPoolExecutor(max_workers=6) as pool:
        for name, places in pool.map(one, halls):
            if places is not None:
                results[name] = places
    return results
