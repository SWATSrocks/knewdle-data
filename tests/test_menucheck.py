"""Offline tests: classifier on realistic menus, and the crawler against a local test website."""
import http.server
import io
import threading
from functools import partial

from menucheck.classify import classify
from menucheck.crawl import Crawler, is_blocked_host

# --- menus modelled on the real Charlotte-area examples ---------------------------------------

KAIRU_LIKE = """
SUSHI ROLLS
California Roll 8.95
Spicy Tuna Roll 9.95
Rainbow Roll 15.95
Dragon Roll 16.95
NIGIRI
Salmon 6.50
Tuna 7.00
Yellowtail 7.00
POKE BOWLS
Classic Poke $16
Spicy Poke $16
RAMEN
Tonkotsu $16 Egg noodles, pork broth, soy tare, chashu, shoyu egg
Black Tonkotsu $17 Black garlic pork broth, chashu, shoyu egg
Shoyu Ramen $16 Chicken broth, soy, chashu, shoyu egg
Garlic Shoyu Ramen $17
Tonkotsu Red $17 Chili sauce, chashu, shoyu egg
Tofu and Veggie Ramen $16 Mushroom broth, fried tofu, broccoli
PHO
Combination Pho $17
Brisket Pho $16
HIBACHI
Chicken Hibachi $18
Steak Hibachi $24
Shrimp Hibachi $22
Teriyaki Chicken $17
Yaki Udon $15
Kids Plain Ramen Noodles $8
""" + "\n".join(f"Special Roll {i} {10 + i}.95" for i in range(120))

RUSANS_LIKE = "\n".join(f"Roll number {i} 12.95" for i in range(400)) + """
Tokyo Shoyu Ramen 15.95 egg noodles in pork soy ramen soup, pork belly
Yasai Miso Ramen 15.95 egg noodles in miso ramen soup, vegetables
Seafood Champon Ramen 17.95 calamari, scallops, shrimp
"""

RAMEN_SHOP = """
Our Ramen
Tonkotsu Ramen $15
Spicy Miso Ramen $16
Shio Ramen $14
Tsukemen $17
Vegan Ramen $15
Tantanmen $16
Sides
Gyoza $7
Karaage $8
Edamame $5
"""

ONE_SPECIAL = """
Burgers
Classic Burger $14
Smash Burger $15
Fried Chicken Sandwich $13
Specials
Short Rib Ramen Bowl $18
Salads
Ramen Salad $11 crunchy ramen noodles, cabbage, sesame dressing
House Salad $9
"""

SACRAMENTO = """
Sacramento Grill - Welcome to Sacramento's best sushi!
Sacramento Roll 12.95
Teriyaki Bowl 13.95
Ramen Salad 10.95
Ramen Burger 15
"""

MARKETING = """
Welcome! We are proud to have served the best sushi and the most amazing ramen in Charlotte since 2009, come see us today for lunch or dinner.
Ramen
California Roll 8.95
"""


def test_kairu_like_serves_ramen():
    v = classify(KAIRU_LIKE)
    assert v.kind == "serves", v
    assert v.dishes >= 6


def test_rusans_like_serves_ramen():
    v = classify(RUSANS_LIKE)
    assert v.kind == "serves" and v.dishes == 3, v


def test_dedicated_shop():
    v = classify(RAMEN_SHOP)
    assert v.kind == "shop" and v.dishes == 6, v


def test_one_special_and_salad_dont_count():
    v = classify(ONE_SPECIAL)
    assert v.kind == "none" and v.dishes == 1, v


def test_sacramento_and_non_bowls_ignored():
    v = classify(SACRAMENTO)
    assert v.kind == "none" and v.dishes == 0, v


def test_marketing_sentence_and_heading_ignored():
    v = classify(MARKETING)
    assert v.kind == "none" and v.dishes == 0, v


def test_same_dish_twice_counts_once():
    v = classify("Tonkotsu Ramen $15\nTONKOTSU RAMEN ..... 15.00\nShoyu Ramen $14")
    assert v.dishes == 2, v


def test_blocked_hosts():
    assert is_blocked_host("https://www.doordash.com/store/x")
    assert is_blocked_host("https://order.online/store/x")
    assert is_blocked_host("https://www.yelp.com/biz/x")
    assert is_blocked_host("https://www.facebook.com/kairu")
    assert not is_blocked_host("https://kairusushi.com/menu")
    assert not is_blocked_host("https://example.square.site/")


# --- crawler against a tiny local website -------------------------------------------------------

def _pdf_with_text(text: str) -> bytes:
    """A minimal one-page PDF containing [text]."""
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    w = PdfWriter()
    page = w.add_blank_page(612, 792)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): w._add_object(font)})})
    lines = "".join(f"BT /F1 12 Tf 50 {700 - 20 * i} Td ({l}) Tj ET\n" for i, l in enumerate(text.split("\n")))
    stream = DecodedStreamObject()
    stream.set_data(lines.encode())
    page[NameObject("/Contents")] = w._add_object(stream)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


SITES = {
    # Site A: menu on a linked page, allowed.
    "a": {
        "/robots.txt": (200, "text/plain", b"User-agent: *\nDisallow: /private\n"),
        "/": (200, "text/html", b"<html><body><nav><a href='/menu'>Our Menu</a><a href='/cart'>Cart</a>"
                                b"</nav><h1>Kai Sushi &amp; Noodle</h1></body></html>"),
        "/menu": (200, "text/html", ("<html><body><h2>Ramen</h2>" + "".join(
            f"<p>{d} ${p}</p>" for d, p in [("Tonkotsu Ramen", 15), ("Miso Ramen", 15), ("Shoyu Ramen", 14),
                                             ("California Roll", 9), ("Gyoza", 7)]) + "</body></html>").encode()),
    },
    # Site B: robots.txt says no to our bot.
    "b": {
        "/robots.txt": (200, "text/plain", b"User-agent: KnewdleNOW-MenuCheck\nDisallow: /\n"),
        "/": (200, "text/html", b"<html><body>Tonkotsu Ramen $15 Shoyu Ramen $14</body></html>"),
    },
    # Site C: menu is a PDF.
    "c": {
        "/robots.txt": (404, "text/plain", b""),
        "/": (200, "text/html", b"<html><body><a href='/files/dinner-menu.pdf'>Dinner Menu (PDF)</a></body></html>"),
        "/files/dinner-menu.pdf": (200, "application/pdf",
                                   _pdf_with_text("Spicy Miso Ramen 16.00\nTonkotsu Ramen 15.00\nTeriyaki 14.00")),
    },
}


class _Handler(http.server.BaseHTTPRequestHandler):
    def __init__(self, site, *a, **kw):
        self.site = site
        super().__init__(*a, **kw)

    def do_GET(self):  # noqa: N802
        status, ctype, body = SITES[self.site].get(self.path, (404, "text/plain", b"nope"))
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def _serve(site):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), partial(_Handler, site))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_crawler_follows_menu_link():
    srv, base = _serve("a")
    try:
        r = Crawler("https://example.org/bot", delay=0).read_site(base + "/")
        assert r.ok and len(r.pages) == 2, r
        assert classify("\n".join(p.text for p in r.pages)).dishes == 3
        assert not any("/cart" in p.url for p in r.pages)
    finally:
        srv.shutdown()


def test_crawler_respects_robots():
    srv, base = _serve("b")
    try:
        r = Crawler("https://example.org/bot", delay=0).read_site(base + "/")
        assert not r.ok and r.blocked_by_robots, r
    finally:
        srv.shutdown()


def test_crawler_reads_pdf_menu():
    srv, base = _serve("c")
    try:
        r = Crawler("https://example.org/bot", delay=0).read_site(base + "/")
        assert r.ok and len(r.pages) == 2, r
        assert classify(r.pages[1].text).dishes == 2, r.pages[1].text
    finally:
        srv.shutdown()
