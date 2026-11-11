"""The closed list's levels: hidden, "may be closed" warnings, and "found open in person" overrides."""
import datetime as dt
import json
import pathlib
import tempfile

from menucheck import closed
from menucheck.claims import SITE_GONE, note_health

TODAY = dt.date(2026, 10, 17)
EXTRA = {"app_known": [
    {"name": "Gone Ramen", "lat": 35.10, "lon": -80.10, "website": "http://goneramen.com", "address": "1 A St"},
    {"name": "Busy Ramen", "lat": 35.20, "lon": -80.20, "website": "https://busyramen.com/", "address": "2 B St"},
    {"name": "Quiet Ramen", "lat": 35.30, "lon": -80.30, "website": None, "address": "3 C St"},
    {"name": "Twice Ramen", "lat": 35.40, "lon": -80.40, "website": "https://twiceramen.com", "address": "4 D St"},
]}


def _run(automatic=(), reported=([], [], {}), site=(), confirmed=None, manual=()):
    old = closed.MENU_STATE
    closed.MENU_STATE = pathlib.Path(tempfile.mkdtemp()) / "none.json"
    try:
        return closed.build(EXTRA, list(manual), reported, list(automatic), lambda places, per_host: list(site),
                            today=TODAY, confirmed=confirmed)
    finally:
        closed.MENU_STATE = old


def test_shop_key_matches_the_app():
    assert closed.shop_key("Kairu", "https://www.KairuSushi.com/menu", 35.4434, -80.8639) == "kairusushi.com@35.44,-80.86"
    assert closed.shop_key("Ramen Soul!", None, 35.582, -80.879) == "ramen-soul@35.58,-80.88"


def test_found_open_in_person_beats_automatic_signals():
    fsq = {"n": "Busy Ramen", "la": 35.20, "lo": -80.20, "why": "foursquare"}
    out = _run([fsq], ([], [], {"busyramen.com@35.20,-80.20": "2026-10-01"}))
    assert out["shops"] == [] and out["open"] == [{"k": "busyramen.com@35.20,-80.20", "last": "2026-10-01"}]
    # ...but not when that visit was long ago.
    out = _run([fsq], ([], [], {"busyramen.com@35.20,-80.20": "2026-01-01"}))
    assert [e["why"] for e in out["shops"]] == ["foursquare"]


def test_website_trouble_is_a_warning_and_two_warnings_hide():
    site = [{"h": "goneramen.com", "why": "site-dns", "t": "x"}, {"h": "twiceramen.com", "why": "site-parked", "t": "x"}]
    report = {"k": "twiceramen.com@35.40,-80.40", "why": "report", "t": "A visitor said it has closed"}
    out = _run(reported=([], [report], {}), site=site)
    assert {e.get("h") or e.get("k") for e in out["maybe"]} == {"goneramen.com", "twiceramen.com", "twiceramen.com@35.40,-80.40"}
    assert [(e["n"], e["why"]) for e in out["shops"]] == [("Twice Ramen", "signals")]


def test_site_health_needs_repeated_checks_over_days():
    d = pathlib.Path(tempfile.mkdtemp())
    health = {}
    note_health(health, "goneramen.com", "dns", "2026-10-01")
    note_health(health, "busyramen.com", "down", "2026-10-01")
    note_health(health, "busyramen.com", "down", "2026-10-08")
    (d / "claims.json").write_text(json.dumps({"site_health": health}))
    old = closed.CLAIMS
    closed.CLAIMS = d / "claims.json"
    try:
        per_host = {"goneramen.com": 1, "busyramen.com": 1}
        assert closed.site_trouble([], per_host) == []             # one check / one week isn't enough
        note_health(health, "goneramen.com", "dns", "2026-10-08")
        note_health(health, "busyramen.com", "down", "2026-10-15")
        (d / "claims.json").write_text(json.dumps({"site_health": health}))
        assert sorted(e["why"] for e in closed.site_trouble([], per_host)) == ["site-dns", "site-down"]
        assert closed.site_trouble([], {"goneramen.com": 5, "busyramen.com": 1})[0]["h"] == "busyramen.com"  # chains skipped
    finally:
        closed.CLAIMS = old


def test_for_sale_pages():
    assert SITE_GONE.search("This domain is for sale! Buy this domain today")
    assert SITE_GONE.search("Account Suspended. This account has been suspended.")
    assert not SITE_GONE.search("Tonkotsu ramen, open daily 11-9. Order online!")


def test_developer_confirmed_open_drops_warnings():
    dns = {"h": "goneramen.com", "why": "site-dns", "t": "Its website's domain no longer exists"}
    out = _run(site=[dns])
    assert out["maybe"][0]["label"] == "Gone Ramen"                       # readable name for Pulse
    # Confirmed by website, by shop key, or by name + spot: the warning goes either way.
    for c in ({"h": "goneramen.com"}, {"k": "goneramen.com@35.10,-80.10"}, {"n": "Gone Ramen", "la": 35.1, "lo": -80.1}):
        assert _run(site=[dns], confirmed=[c])["maybe"] == []
    # Automatic hides and single-visitor warnings go too; hand-kept closed.txt entries and 2+ phone reports don't.
    fsq = {"n": "Busy Ramen", "la": 35.20, "lo": -80.20, "why": "foursquare"}
    rep = {"k": "busyramen.com@35.20,-80.20", "n": "Busy Ramen", "count": 1, "why": "report", "t": "A visitor said"}
    man = {"n": "Busy Ramen", "la": 35.20, "lo": -80.20, "why": "manual"}
    out = _run([fsq], ([], [rep], {}), manual=[man], confirmed=[{"h": "busyramen.com"}])
    assert [e["why"] for e in out["shops"]] == ["manual"] and out["maybe"] == []
    # Other shops keep theirs.
    assert len(_run(site=[dns], confirmed=[{"h": "busyramen.com"}])["maybe"]) == 1


def test_apply_confirmed_to_published_file():
    out = {"shops": [{"h": "goneramen.com", "why": "website"}], "maybe": [{"h": "goneramen.com", "why": "site-dns"}]}
    listed = closed.Listed(closed.listed_places(EXTRA))
    assert closed.apply_confirmed(out, closed.Confirmed([], listed)) == 0
    assert closed.apply_confirmed(out, closed.Confirmed([{"h": "goneramen.com"}], listed)) == 2
    assert out == {"shops": [], "maybe": []}
