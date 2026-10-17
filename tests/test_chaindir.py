"""Chain-directory closures, against a tiny fake chain directory served locally."""
import http.server
import json
import threading
from functools import partial

import menucheck.chaindir as cd
from menucheck.crawl import Crawler

PAGES = {
    "/robots.txt": "User-agent: *\nAllow: /\n",
    "/": '<a href="/nc">NC</a><a href="/sc">SC</a><a href="/va">VA</a><a href="/ga">GA</a><a href="/tn">TN</a>',
    "/nc": '<a href="/nc/charlotte">Charlotte</a><a href="/nc/cary">Cary</a>',
    "/nc/charlotte": '<a href="/nc/charlotte/8016-providence-rd">Arboretum</a>'
                     '<a href="/nc/charlotte/8926-jm-keynes-dr">University</a>',
    "/nc/cary": '<a href="/nc/cary/200-crossroads-blvd">Crossroads</a>',
}


class _H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = PAGES.get(self.path.rstrip("/") or "/")
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain" if self.path.endswith(".txt") else "text/html")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *a):
        pass


def _run(listed, tmp_cache, share=None):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    old = (cd.directories, cd.CACHE, cd.MAX_SHARE)
    cd.directories = lambda: [{"name": "Noodles & Company", "match": r"^noodles\s*(?:&|and)\s*co", "directory": base}]
    cd.CACHE = tmp_cache
    if share is not None:
        cd.MAX_SHARE = share
    try:
        c = Crawler("https://example.org/bot", delay=0)
        c.crawl_delay = lambda u: 0
        return cd.check(c, listed, log=lambda *a: None, today="2026-10-17")
    finally:
        cd.directories, cd.CACHE, cd.MAX_SHARE = old
        srv.shutdown()


def P(name, addr, lat=35.2, lon=-80.8):
    return {"name": name, "lat": lat, "lon": lon, "address": addr}


def test_address_parts():
    assert cd.state_of("489 River Hwy, Mooresville, NC") == "nc"
    assert cd.state_of("489 River Hwy, Mooresville, NC 28117") == "nc"
    assert cd.house_of("489 River Hwy, Mooresville, NC") == "489"
    assert cd.house_of("River Hwy, Mooresville, NC") is None


def test_store_missing_from_directory_is_closed(tmp_path=None):
    import tempfile, pathlib
    cache = pathlib.Path(tempfile.mkdtemp()) / "c.json"
    listed = [P("Noodles & Company", "8016 Providence Rd, Charlotte, NC"),
              P("Noodles and Company", "200 Crossroads Blvd, Cary, NC"),
              P("Noodles & Company", "8926 J.M. Keynes Dr, Charlotte, NC"),
              P("Noodles & Company", "1120 Parkside Main St, Cary, NC"),
              P("Noodles & Company", "489 River Hwy, Mooresville, NC", 35.594542, -80.864582),
              P("Kairu Sushi", "9709 Sam Furr Rd, Huntersville, NC")]
    got = _run(listed, cache, share=0.5)
    assert sorted(e["a"] for e in got) == ["1120 Parkside Main St, Cary, NC", "489 River Hwy, Mooresville, NC"], got
    assert all(e["why"] == "chain" for e in got)
    saved = json.loads(cache.read_text())["Noodles & Company"]["states"]["nc"]
    assert saved["stores"] == ["200", "8016", "8926"]


def test_chain_left_a_state_entirely():
    import tempfile, pathlib
    cache = pathlib.Path(tempfile.mkdtemp()) / "c.json"
    listed = [P("Noodles & Company", "8016 Providence Rd, Charlotte, NC")] * 6 + \
             [P("Noodles & Company", "100 Main St, Boise, ID")]
    got = _run(listed, cache)
    assert [e["a"] for e in got] == ["100 Main St, Boise, ID"], got


def test_too_many_missing_hides_nothing():
    import tempfile, pathlib
    cache = pathlib.Path(tempfile.mkdtemp()) / "c.json"
    listed = [P("Noodles & Company", "1 A St, Charlotte, NC"), P("Noodles & Company", "2 B St, Charlotte, NC"),
              P("Noodles & Company", "8016 Providence Rd, Charlotte, NC")]
    assert _run(listed, cache) == []
