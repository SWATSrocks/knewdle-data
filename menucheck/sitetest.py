"""Check one website by hand and explain each step (for "why isn't this shop showing up?").

    python -m menucheck.sitetest https://www.example.com/ ["Shop Name"]

Prints which links the bot followed or skipped and why, what each page gave, and the final verdict.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from . import crawl
from .classify import classify

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    url = sys.argv[1]
    name = sys.argv[2] if len(sys.argv) > 2 else ""
    cfg = ROOT / "config.json"
    info = json.loads(cfg.read_text()).get("bot_info_url", "https://github.com/") if cfg.exists() else "https://github.com/"
    c = crawl.Crawler(info)

    # Trace the link-following decisions the same way read_site makes them.
    orig_get = c._get

    def traced_get(u, limit):
        try:
            r = orig_get(u, limit)
        except Exception as e:  # noqa: BLE001
            print(f"  GET {u} -> {type(e).__name__}: {e}")
            raise
        print(f"  GET {u} -> {'not 200 / blocked' if r is None else f'{r.status_code} {r.headers.get(chr(99)+'ontent-type', '')} {len(r.content)} bytes'}")
        return r

    c._get = traced_get  # type: ignore[method-assign]
    orig_allowed = c.allowed
    shown: set[str] = set()

    def traced_allowed(u):
        ok = orig_allowed(u)
        if not ok:
            from urllib.parse import urlparse
            q = urlparse(u)
            base = f"{q.scheme}://{q.netloc}"
            print(f"  robots: NOT allowed {u} (robots.txt reachable: {c._robots.get(base) is not None})")
            if base not in shown:
                shown.add(base)
                try:
                    r = c.session.get(base + "/robots.txt", timeout=15)
                    print(f"    {base}/robots.txt -> {r.status_code}:\n      " + r.text[:800].replace("\n", "\n      "))
                except Exception as e:  # noqa: BLE001
                    print(f"    {base}/robots.txt -> {type(e).__name__}")
        return ok

    c.allowed = traced_allowed  # type: ignore[method-assign]

    print(f"Reading {url}")
    site = c.read_site(url)
    print(f"ok={site.ok} error={site.error} robots={site.blocked_by_robots} pages={len(site.pages)}")
    for p in site.pages:
        v = classify(p.text, name)
        print(f"\n--- {p.url}: {len(p.text)} chars -> {v}")
        print("   ", p.text[:400].replace("\n", " | "))
        if "ramen" in p.text.lower():
            i = p.text.lower().index("ramen")
            print("    ramen context:", p.text[max(0, i - 150): i + 250].replace("\n", " | "))
    if site.pages:
        print("\nAll pages together:", classify("\n".join(p.text for p in site.pages), name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
