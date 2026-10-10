"""Offline tests for the extra finders: addresses, name rules, chain locators and new-website reading."""
import json

from menucheck import chains as chains_mod
from menucheck.crawl import Crawler
from menucheck.ctlog import ramen_domain, read_domain, registrable
from menucheck.names import why
from menucheck.places import PlaceIndex, addresses_in_text, find_locations

from test_menucheck import SITES, _serve


class FakeGeocoder:
    """Pretends every address is found (no network in tests)."""
    def __init__(self):
        self.calls = 0

    def locate(self, loc):
        self.calls += 1
        if loc.lat is None:
            loc.lat, loc.lon = 35.0 + self.calls / 1000, -80.0
        return True


def test_addresses_and_coming_soon():
    text = """1234 W Main St, Suite 5
Charlotte, NC 28202
Our new spot: 19 Old Fulton St. Brooklyn, New York 11201 coming soon!
21726 Catawba Ave B-2, Cornelius, NC 28031
3000 Broadway, New York, NY 10027
$15 Tonkotsu 2 eggs, 12 Ramen Street lovers"""
    got = [(l.oneline, l.soon) for l, _ in addresses_in_text(text)]
    assert got == [
        ("1234 W Main St Suite 5, Charlotte, NC 28202", False),
        ("19 Old Fulton St., Brooklyn, NY 11201", True),
        ("21726 Catawba Ave B-2, Cornelius, NC 28031", False),
        ("3000 Broadway, New York, NY 10027", False),
    ], got


def test_structured_data_and_map_pin():
    html = """<html><head><title>Kaiyo Ramen | Home</title></head><body>
      <p>55 Elm St, Austin, TX 78701</p><a href="https://www.google.com/maps/place/x/@30.2672,-97.7431,17z">Map</a>
    </body></html>"""
    locs, text, title = find_locations(html)
    # The pin is only a fallback: the free geocoder's answer for the address comes first.
    assert title == "Kaiyo Ramen" and len(locs) == 1 and locs[0].lat is None and locs[0].pin == (30.2672, -97.7431), locs


def test_address_without_zip_and_lowercase_or_ignored():
    got = [l.oneline for l, _ in addresses_in_text("2170 S Atlantic Blvd,\nMonterey Park, CA\nTea or coffee, 12 Main St, Portland or anywhere")]
    assert got == ["2170 S Atlantic Blvd, Monterey Park, CA"], got


def test_name_rules():
    jr = "japanese_restaurant"
    assert why("JINYA Ramen Bar", "", [], None, None) == "app"          # the app finds these already
    assert why("Tsujita LA Artisan Noodle", jr, [], None, None) == "name"
    assert why("Mazesoba Hero", "restaurant", [], None, None) == "name"
    assert why("中華そば 一心", jr, [], None, None) == "name"
    assert why("Some Place", "restaurant", [], "Q6065417", None) == "brand"     # Ippudo's Wikidata id
    assert why("Kaiyo", jr, ["https://kaiyoramen.com/"], None, None) == "site"
    assert why("Kairu Sushi and Noodle Bar", jr + " sushi_restaurant", [], None, None, jr) == "noodle"
    assert why("Udon Noodle Bar", jr, [], None, None, jr) is None
    assert why("Thai Noodle House", "asian_restaurant", [], None, None) is None
    assert why("Sacramento Sushi", jr, ["http://sacramentosushi.com"], None, None) is None
    # Not somewhere to eat, or ramen only by accident of spelling:
    assert why("Go Outdoor Amenities", "outdoor_furniture_store", ["http://gooutdooramenities.com"], None, None) is None
    assert why("Dr. Pramenko", "doctor", ["http://www.drpramenko.com"], None, None) is None
    assert why("Gramener Inc", "software_development", ["gramener.com"], None, None) is None
    assert why("Kinton Guns", "gun_store", [], None, None) is None
    assert why("Walala 兰州拉面 Noodle House", "chinese_restaurant", [], None, None) is None


def test_domains():
    assert registrable("*.kaiyoramen.com") == "kaiyoramen.com"
    assert registrable("www.ramenbar.co.uk") == "ramenbar.co.uk"
    assert ramen_domain("kaiyoramen.com") and not ramen_domain("sacramento.com")
    assert registrable("ramen.example.com") == "example.com" and not ramen_domain("example.com")


def test_place_index_dedupes():
    idx = PlaceIndex([{"name": "JINYA Ramen Bar", "lat": 35.2, "lon": -80.8, "website": "https://www.jinyaramenbar.com/"}])
    assert idx.has({"name": "JINYA Ramen Bar - South End", "lat": 35.2003, "lon": -80.8, "website": None})
    assert not idx.has({"name": "Kaiyo", "lat": 35.25, "lon": -80.8, "website": None})


# --- chain locator against a local test site ---------------------------------------------------

SITES["chain"] = {
    "/robots.txt": (200, "text/plain", b"User-agent: *\nDisallow: /private\nSitemap: /sitemap.xml\n"),
    "/locations/": (200, "text/html", b"<html><body><h1>Our locations</h1>"
                    b"<a href='/locations/south-end/'>South End</a><a href='/locations/uptown/'>Uptown</a>"
                    b"<a href='/private/x'>x</a></body></html>"),
    "/sitemap.xml": (200, "application/xml", b"<urlset><url><loc>/locations/ballantyne/</loc></url></urlset>"),
    "/locations/south-end/": (200, "text/html", b"<html><body>JINYA South End<p>1600 Camden Rd, Charlotte, NC 28203</p>"
                              b"<footer>Also: 100 N Tryon St, Charlotte, NC 28202</footer></body></html>"),
    "/locations/uptown/": (200, "text/html", b"<html><body>Uptown<p>100 N Tryon St, Charlotte, NC 28202</p></body></html>"),
    "/locations/ballantyne/": (200, "text/html", b"<html><body>Ballantyne COMING SOON<p>7900 Ballantyne Commons Pkwy, Charlotte, NC 28277</p></body></html>"),
}


def test_chain_locator_reads_location_pages():
    srv, base = _serve("chain")
    try:
        chain = {"name": "Test Ramen Chain", "site": base + "/", "start": [base + "/locations/"],
                 "pages": r"/locations/[a-z0-9-]+/?$", "sitemaps": [base + "/sitemap.xml"]}
        out = chains_mod.read_chain(Crawler("https://example.org/bot", delay=0), chain, FakeGeocoder(), log=lambda *a: None)
        addrs = sorted(p["address"] for p in out)
        # The sitemap's /locations/ballantyne/ is relative, so not followed; it's also "coming soon".
        assert addrs == ["100 N Tryon St, Charlotte, NC", "1600 Camden Rd, Charlotte, NC"], addrs
        assert all(p["f"] == "chain" and p["name"] == "Test Ramen Chain" for p in out)
    finally:
        srv.shutdown()


# --- new-website reading --------------------------------------------------------------------------

SITES["newshop"] = {
    "/robots.txt": (404, "text/plain", b""),
    "/": (200, "text/html", b"<html><head><meta property='og:site_name' content='Kaiyo Ramen'></head><body>"
          b"<h1>Kaiyo Ramen</h1><p>Tonkotsu, shoyu and miso ramen made fresh every day. Opening this fall in South End.</p>"
          b"<a href='/contact'>Contact &amp; hours</a></body></html>"),
    "/contact": (200, "text/html", b"<html><body>Find us: 2000 South Blvd, Suite 100, Charlotte, NC 28203. "
                 b"Coming soon!</body></html>"),
}
SITES["parked"] = {
    "/robots.txt": (404, "text/plain", b""),
    "/": (200, "text/html", b"<html><body>" + b"This domain is for sale! Buy this domain today. " * 10 + b"ramen</body></html>"),
}


def test_new_website_with_address_on_contact_page():
    srv, base = _serve("newshop")
    try:
        res = read_domain(Crawler("https://example.org/bot", delay=0), base.removeprefix("http://"), FakeGeocoder())
        assert res["status"] == "ok" and res["name"] == "Kaiyo Ramen", res
        assert res["locations"][0]["address"] == "2000 South Blvd Suite 100, Charlotte, NC"
        assert res["locations"][0]["soon"] is True
    finally:
        srv.shutdown()


def test_parked_domain_skipped():
    srv, base = _serve("parked")
    try:
        res = read_domain(Crawler("https://example.org/bot", delay=0), base.removeprefix("http://"), FakeGeocoder())
        assert res["status"] == "parked", res
    finally:
        srv.shutdown()


def test_chains_config_is_valid():
    import re
    cfg = json.loads(chains_mod.CONFIG.read_text())
    assert len(cfg["chains"]) >= 20
    for c in cfg["chains"]:
        assert c["site"].startswith("https://") and c["start"]
        if c.get("pages"):
            re.compile(c["pages"])


# --- food halls ---------------------------------------------------------------------------------------

def test_hall_directory_cards():
    from menucheck.halls import vendors_on_page
    html = b"""<html><body><nav><a>Eat</a><a>Vendors</a></nav><h1>Tenants</h1>
      <div class="grid">
        <div class="card"><h3>Bao &amp; Broth</h3><p>A selection of steamed buns and ramen bowls.</p></div>
        <div class="card"><h3>Papi Queso</h3><p>Gourmet grilled cheese.</p></div>
        <div class="card"><h3>The Dumpling Lady</h3><p>Sichuan dumplings and noodles.</p></div>
        <div class="card"><h3>Menya Hall Counter</h3><p>Opening soon!</p></div>
        <div class="card"><h3>Tsukemen Lab</h3><p>Dipping noodles.</p></div>
      </div><footer>Ramen nights every Friday</footer></body></html>"""
    got = [n for n, _ in vendors_on_page(html, "Optimist Hall")]
    assert got == ["Bao & Broth", "Tsukemen Lab"], got


def test_hall_vendor_page():
    from menucheck.halls import vendor_page
    page = b"<html><body><header>Ponce City Market</header><h1>Okiboru</h1><p>Traditional ramen and tsukemen from Atlanta.</p></body></html>"
    assert vendor_page(page, "Ponce City Market")[0] == "Okiboru"
    soon = b"<html><body><h1>Okiboru</h1><p>Coming soon: ramen and tsukemen.</p></body></html>"
    assert vendor_page(soon, "Ponce City Market") is None
    other = b"<html><body><h1>Botiwalla</h1><p>Indian street food.</p></body></html>"
    assert vendor_page(other, "Ponce City Market") is None


def test_halls_config_is_valid():
    import re
    from menucheck.halls import CONFIG
    from menucheck.places import addresses_in_text
    cfg = json.loads(CONFIG.read_text())
    for h in cfg["halls"]:
        assert h["start"] and all(u.startswith("https://") for u in h["start"]), h
        assert addresses_in_text(h["address"]), h["address"]
        if h.get("pages"):
            re.compile(h["pages"])
