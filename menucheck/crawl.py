"""Polite reader for a restaurant's OWN website: homepage plus a few menu pages/PDFs.

Rules it always follows:
  * robots.txt is checked for every host before any page is fetched (and honoured).
  * It identifies itself honestly (see USER_AGENT) with a link explaining what it does and how to opt out.
  * Third-party platforms (delivery apps, review sites, ordering systems, social media) are never read:
    their terms don't allow it, and we only want the restaurant's own word anyway.
  * At most a handful of requests per restaurant per check, with timeouts and size limits.
"""
from __future__ import annotations

import io
import re
import threading
import time
import urllib.robotparser
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BOT_NAME = "KnewdleNOW-MenuCheck"
USER_AGENT = f"{BOT_NAME}/1.0 (+{{info_url}})"

TIMEOUT = 15
MAX_HTML = 3_000_000
MAX_PDF = 8_000_000
MAX_MENU_PAGES = 4

# Never read these: delivery/ordering/review/social platforms and link-in-bio pages.
BLOCKED_HOSTS = (
    "doordash.com", "ubereats.com", "grubhub.com", "seamless.com", "postmates.com", "caviar.com",
    "yelp.com", "tripadvisor.", "opentable.", "resy.com", "exploretock.com", "toasttab.com",
    "order.online", "chownow.com", "clover.com", "menufy.com", "beyondmenu.com", "menupages.com",
    "allmenus.com", "singleplatform.com", "zmenu.com", "sluurpy.", "restaurantji.com",
    "facebook.com", "fb.com", "instagram.com", "tiktok.com", "twitter.com", "x.com",
    "linktr.ee", "linkin.bio", "google.com", "goo.gl", "g.page", "maps.app.goo.gl",
    "slicelife.com", "foodboss.com", "wanderlog.com", "ezcater.com", "giftly.com",
    "toast.site", "wikipedia.org", "wikimedia.org", "wikidata.org", "squareup.com", "order.app.hiro.io",
    "spoton.com", "owner.com", "orderonlinemenus.com", "menusifu.com", "chinesemenuonline.com",
)

# Website builders often keep menu PDFs/images on these asset hosts; they're still the restaurant's own files.
ASSET_HOSTS = (
    "squarespace-cdn.com", "static1.squarespace.com", "wixstatic.com", "website-files.com",
    "wp.com", "cdn.shopify.com", "img1.wsimg.com", "godaddysites.com", "weebly.com", "square.site",
    "cloudfront.net", "amazonaws.com",
)

MENU_HINT = re.compile(r"menu|food|dinner|lunch|ramen|noodle|eat|kitchen", re.IGNORECASE)


@dataclass
class Page:
    url: str
    text: str


@dataclass
class CrawlResult:
    ok: bool
    pages: list[Page] = field(default_factory=list)
    error: str | None = None
    blocked_by_robots: bool = False


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def is_blocked_host(url: str) -> bool:
    h = _host(url)
    return any(h == b or h.endswith("." + b) or (b.endswith(".") and b in h) for b in BLOCKED_HOSTS)


def _site_key(host: str) -> str:
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


class Crawler:
    def __init__(self, info_url: str, delay: float = 1.0):
        self.ua = USER_AGENT.format(info_url=info_url)
        self.delay = delay
        self._local = threading.local()
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    @property
    def session(self) -> requests.Session:
        """One HTTP session per worker thread (sessions aren't guaranteed thread-safe)."""
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            s.headers.update({"User-Agent": self.ua, "Accept-Language": "en-US,en;q=0.8"})
            self._local.session = s
        return s

    # ---------- robots.txt ----------

    def robots_reachable(self, url: str) -> bool:
        """False when robots.txt couldn't even be fetched (network trouble), as opposed to a real "no"."""
        p = urlparse(url)
        self.allowed(url)
        return self._robots.get(f"{p.scheme}://{p.netloc}") is not None

    def allowed(self, url: str) -> bool:
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        if base not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                r = self.session.get(base + "/robots.txt", timeout=(6, TIMEOUT), allow_redirects=True)
                if r.status_code in (401, 403):
                    rp.disallow_all = True
                elif r.status_code >= 400:
                    rp.allow_all = True          # no robots.txt = no restrictions (standard behaviour)
                else:
                    rp.parse(r.text.splitlines())
            except requests.RequestException:
                rp = None                         # can't tell: play safe and skip this host
            self._robots[base] = rp
        rp = self._robots[base]
        return bool(rp and rp.can_fetch(self.ua, url) and rp.can_fetch(BOT_NAME, url))

    # ---------- fetching ----------

    def _get(self, url: str, limit: int) -> requests.Response | None:
        r = self.session.get(url, timeout=(6, TIMEOUT), stream=True, allow_redirects=True)
        if r.status_code != 200:
            r.close()
            return None
        if is_blocked_host(r.url):   # redirected to a delivery app etc.
            r.close()
            return None
        body = io.BytesIO()
        for chunk in r.iter_content(64 * 1024):
            body.write(chunk)
            if body.tell() > limit:
                break
        r._content = body.getvalue()  # noqa: SLF001 - keep the (size-capped) body
        return r

    def _page_text(self, r: requests.Response) -> tuple[str, list[str]]:
        ctype = r.headers.get("content-type", "").lower()
        if "pdf" in ctype or r.url.lower().split("?")[0].endswith(".pdf"):
            return _pdf_text(r.content), []
        soup = BeautifulSoup(r.content, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg", "iframe"]):
            tag.decompose()
        links = []
        for a in soup.find_all("a", href=True):
            label = " ".join(a.get_text(" ", strip=True).split())[:60]
            href = urljoin(r.url, a["href"])
            if href.startswith("http") and (MENU_HINT.search(label) or MENU_HINT.search(urlparse(href).path)):
                links.append(href)
        text = soup.get_text("\n", strip=True)
        return text, links

    def read_site(self, website: str) -> CrawlResult:
        url = website if website.startswith("http") else "https://" + website
        if is_blocked_host(url):
            return CrawlResult(False, error="third-party platform")
        # Listings often have an old form of the address ("http://name.com"); the live site may only
        # answer at https:// and/or www. Try the listed address first, then those variants.
        home = None
        last_error = "site unreachable"
        for candidate in _address_variants(url):
            if not self.allowed(candidate):
                if self.robots_reachable(candidate):
                    return CrawlResult(False, error="robots.txt", blocked_by_robots=True)
                continue
            try:
                home = self._get(candidate, MAX_HTML)
            except requests.RequestException as e:
                last_error = type(e).__name__
                continue
            if home is not None:
                break
            last_error = "no homepage"
        if home is None:
            return CrawlResult(False, error=last_error)

        home_text, links = self._page_text(home)
        result = CrawlResult(True, [Page(home.url, home_text)])
        site = _site_key(_host(home.url))
        seen = {home.url}
        for link in _rank_menu_links(links):
            if len(result.pages) > MAX_MENU_PAGES or link in seen:
                continue
            seen.add(link)
            h = _host(link)
            own = _site_key(h) == site or any(h.endswith(a) for a in ASSET_HOSTS)
            if not own or is_blocked_host(link) or not self.allowed(link):
                continue
            time.sleep(self.delay)
            try:
                is_pdf = link.lower().split("?")[0].endswith(".pdf")
                r = self._get(link, MAX_PDF if is_pdf else MAX_HTML)
            except requests.RequestException:
                continue
            if r is None:
                continue
            text, _ = self._page_text(r)
            if text:
                result.pages.append(Page(r.url, text))
        return result


def _address_variants(url: str) -> list[str]:
    p = urlparse(url)
    host = (p.hostname or "").lower()
    bare = host.removeprefix("www.")
    path = p.path or "/"
    if p.query:
        path += "?" + p.query
    out = [url]
    for scheme in ("https", "http"):
        for h in (host, "www." + bare if not host.startswith("www.") else bare):
            v = f"{scheme}://{h}{path}"
            if v not in out:
                out.append(v)
    return out


def _rank_menu_links(links: list[str]) -> list[str]:
    def score(u: str) -> int:
        p = u.lower()
        s = 0
        if "menu" in p:
            s += 5
        if "ramen" in p or "noodle" in p:
            s += 4
        if p.split("?")[0].endswith(".pdf"):
            s += 3
        if "dinner" in p or "food" in p:
            s += 2
        if any(x in p for x in ("cart", "checkout", "login", "account", "gift", "career", "job", "privacy")):
            s -= 10
        return s
    uniq = list(dict.fromkeys(links))
    return [u for u in sorted(uniq, key=score, reverse=True) if score(u) > 0]


def _pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((p.extract_text() or "") for p in reader.pages[:12])
    except Exception:  # noqa: BLE001 - broken PDFs are common; just skip them
        return ""
