"""Spot a shop's own "join the waitlist" link on its website (for the app's Join the waitlist button).

Only links the shop itself posts on its own site: we never crawl Yelp, Toast or any waitlist service, and the
app just opens the link, where the person joins on that service themselves.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

# Waitlist services: host -> what the URL must also contain ("" = the host alone is a waitlist).
SERVICES: dict[str, str] = {
    "waitwhile.com": "",
    "nextmeapp.com": "",
    "waitlist.me": "",
    "tablesready.com": "",
    "carbonaraapp.com": "",
    "yelp.com": "/waitlist",
    "toasttab.com": "waitlist",
    "sevenrooms.com": "waitlist",
    "opentable.com": "waitlist",
    "squareup.com": "waitlist",
    "square.site": "waitlist",
}
SAYS_WAITLIST = re.compile(r"\b(join (the |our )?(wait ?list|line|queue)|wait ?list|get in line|join line)\b", re.I)
NOT_IT = re.compile(r"\b(cater|career|job|hiring|newsletter|gift|private event|reserv)", re.I)


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def is_waitlist_url(url: str, label: str = "") -> bool:
    if not url.startswith("https://"):
        return False
    h = _host(url)
    low = url.lower()
    for svc, must in SERVICES.items():
        if h == svc or h.endswith("." + svc):
            if svc == "yelp.com":
                return "/waitlist/" in low
            return must == "" or must in low or bool(SAYS_WAITLIST.search(label))
    return False


def clean(url: str) -> str:
    """A Yelp waitlist embedded as a widget ("/waitlist/x/widget?theme=dark") -> its normal page."""
    if "yelp.com/waitlist/" in url.lower():
        url = url.split("?")[0]
        url = re.sub(r"/widget/?$", "", url)
    return url


def find_waitlist(soup, base: str) -> str | None:
    """The first waitlist link (or embedded waitlist widget) on a parsed page, else None."""
    for a in soup.find_all("a", href=True):
        label = " ".join(a.get_text(" ", strip=True).split())[:80]
        href = urljoin(base, a["href"]).split("#")[0]
        if NOT_IT.search(label):
            continue
        if is_waitlist_url(href, label):
            return clean(href)
    for f in soup.find_all("iframe", src=True):
        src = urljoin(base, f["src"]).split("#")[0]
        if is_waitlist_url(src):
            return clean(src)
    return None
