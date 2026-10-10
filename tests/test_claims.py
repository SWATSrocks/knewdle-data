"""Offline tests for shop claims: codes, finding the code, reading updates, closure dates."""
import datetime as dt

from menucheck.claims import closed_range, code_for, has_code, parse_updates, update_lines

D = dt.date(2026, 10, 10)


def test_code_is_per_website_and_ignores_www():
    assert code_for("kairusushi.com") == code_for("www.KairuSushi.com")
    assert code_for("kairusushi.com") != code_for("otherramen.com")
    assert code_for("kairusushi.com").startswith("kn-") and len(code_for("kairusushi.com")) == 9


def test_code_found_as_text_or_meta():
    c = code_for("kairusushi.com")
    assert has_code(f"<footer>Knewdle NOW verified: {c}</footer>", c)
    assert has_code(f'<head><meta name="knewdle-verify" content="{c}"></head>', c)
    assert not has_code("<footer>Knewdle NOW verified: kn-000000</footer>", c)


def test_updates_section_on_homepage():
    html = """<html><body><h1>Kairu</h1><p>Welcome!</p><h2>Knewdle NOW updates</h2>
      <p>Special: Spicy miso tonkotsu, $15 see https://spam.example</p><p>Closed: Nov 27-28</p>
      <p>Hours: Tue-Sun 11:30am-9pm</p><p>Note: Late-night ramen Fridays!</p><p>Contact us</p>
      <p>Special: this one is outside the section</p></body></html>"""
    got = parse_updates(update_lines(html, whole_page=False), D)
    assert got == {"special": "Spicy miso tonkotsu, $15 see", "closed": {"from": "2026-11-27", "to": "2026-11-28",
                   "text": "Nov 27-28"}, "hours": "Tue-Sun 11:30am-9pm", "note": "Late-night ramen Fridays!"}, got


def test_no_section_means_no_updates():
    assert update_lines("<p>Special: not in an updates section</p>", whole_page=False) == []


def test_note_is_cut_to_120_characters():
    got = parse_updates([("note", "x" * 300)], D)
    assert len(got["note"]) == 120


def test_closed_dates():
    assert closed_range("Nov 27 - Dec 2", D) == (dt.date(2026, 11, 27), dt.date(2026, 12, 2))
    assert closed_range("December 24", D) == (dt.date(2026, 12, 24), dt.date(2026, 12, 24))
    assert closed_range("Dec 31 - Jan 2", D) == (dt.date(2026, 12, 31), dt.date(2027, 1, 2))
    assert closed_range("11/27-11/28", D) == (dt.date(2026, 11, 27), dt.date(2026, 11, 28))
    assert closed_range("Jan 5", dt.date(2026, 12, 20)) == (dt.date(2027, 1, 5), dt.date(2027, 1, 5))
    assert closed_range("for a private event", D) is None
    # Already over: not published.
    assert "closed" not in parse_updates([("closed", "Oct 1-2")], D)


def test_shared_platforms_cannot_be_claimed():
    from menucheck.claims import SHARED_HOSTS
    from menucheck.crawl import is_blocked_host
    for h in ("online.skytab.com", "kairu.square.site", "order.toasttab.com"):
        assert is_blocked_host("https://" + h) or any(x in h for x in SHARED_HOSTS), h


def test_closed_for_good_on_homepage():
    from menucheck.claims import says_closed_for_good
    assert says_closed_for_good("<p>After 8 wonderful years, we have closed our doors. Thank you, Mooresville!</p>")
    assert says_closed_for_good("<h1>Ramen Soul is permanently closed</h1>")
    assert not says_closed_for_good("<p>We're closed on Mondays. Open Tue-Sun.</p>")
    assert not says_closed_for_good("<p>Temporarily closed for renovations, we'll reopen in May!</p>")
    assert not says_closed_for_good("<p>We have closed for the holiday and reopen Friday.</p>")


def test_manual_closed_list_and_chain_guard(tmp_path=None):
    import json, tempfile
    from pathlib import Path
    from menucheck import closed
    d = Path(tempfile.mkdtemp())
    (d / "closed.txt").write_text("# note\nRamen Soul | 35.582052, -80.879841  # closed\noldramen.com\nbad line here\n")
    closed.MANUAL = d / "closed.txt"
    got = closed.manual()
    assert got == [{"n": "Ramen Soul", "la": 35.582052, "lo": -80.879841, "why": "manual"},
                   {"h": "oldramen.com", "why": "manual"}], got
    closed.CLAIMS = d / "claims.json"
    (d / "claims.json").write_text(json.dumps({"closed_sites": {"onlyshop.com": "2026-10-10", "jinyaramenbar.com": "2026-10-10"}}))
    listed = [{"name": "Only Shop", "lat": 1, "lon": 1, "website": "https://onlyshop.com"},
              {"name": "JINYA A", "lat": 2, "lon": 2, "website": "https://www.jinyaramenbar.com/"},
              {"name": "JINYA B", "lat": 3, "lon": 3, "website": "https://www.jinyaramenbar.com/"}]
    assert closed.website_closed(listed) == [{"h": "onlyshop.com", "why": "website"}]
