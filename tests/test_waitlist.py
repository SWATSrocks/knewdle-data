from bs4 import BeautifulSoup

from menucheck.waitlist import find_waitlist, is_waitlist_url


def page(html):
    return BeautifulSoup(html, "html.parser")


def test_services():
    assert is_waitlist_url("https://www.yelp.com/waitlist/marufuku-ramen-san-francisco-3")
    assert not is_waitlist_url("https://www.yelp.com/biz/marufuku-ramen-san-francisco-3")
    assert is_waitlist_url("https://waitwhile.com/locations/ramen-shop")
    assert is_waitlist_url("https://www.toasttab.com/local/ramen-x/waitlist")
    assert not is_waitlist_url("https://www.toasttab.com/local/order/ramen-x")
    assert is_waitlist_url("https://www.toasttab.com/local/ramen-x/join", "Join the waitlist")
    assert not is_waitlist_url("http://waitwhile.com/locations/x")          # https only
    assert not is_waitlist_url("https://example.com/waitlist")              # unknown service


def test_find_on_page():
    html = '<a href="/menu">Menu</a><a href="https://www.yelp.com/waitlist/kazan-bh">Join our waitlist</a>'
    assert find_waitlist(page(html), "https://shop.com/") == "https://www.yelp.com/waitlist/kazan-bh"
    assert find_waitlist(page('<iframe src="https://waitwhile.com/welcome/abc"></iframe>'), "https://s.com/") \
        == "https://waitwhile.com/welcome/abc"
    assert find_waitlist(page('<a href="https://www.yelp.com/biz/x">Yelp</a>'), "https://s.com/") is None
    # A reservations link that happens to be on a waitlist-capable service is not a waitlist
    assert find_waitlist(page('<a href="https://www.opentable.com/r/x?waitlist=1">Reservations</a>'), "https://s.com/") is None


def test_yelp_widget_cleaned():
    html = '<iframe src="https://www.yelp.com/waitlist/amakara-dublin/widget?theme=dark"></iframe>'
    assert find_waitlist(page(html), "https://s.com/") == "https://www.yelp.com/waitlist/amakara-dublin"
