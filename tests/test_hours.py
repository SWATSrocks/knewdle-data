from menucheck import hours as H
from menucheck import claims as C


def _p(html):
    w, how = H.parse(html)
    return (H.fmt(w).replace("\n", " · ") if w else None), how


def test_jsonld_spec():
    html = ('<script type="application/ld+json">{"@type":"Restaurant","openingHoursSpecification":['
            '{"dayOfWeek":["Monday","Tuesday","Wednesday","Thursday"],"opens":"11:00","closes":"21:00"},'
            '{"dayOfWeek":"https://schema.org/Friday","opens":"11:00:00","closes":"22:30:00"},'
            '{"dayOfWeek":"Saturday","opens":"12:00","closes":"02:00"},'
            '{"dayOfWeek":"Sunday","opens":"10:00","closes":"14:00","validFrom":"2026-12-24","validThrough":"2026-12-24"}]}</script>')
    assert _p(html) == ("Mon–Thu 11am–9pm · Fri 11am–10:30pm · Sat 12pm–2am · Sun Closed", "jsonld")


def test_jsonld_string_and_graph():
    html = ('<script type="application/ld+json">{"@graph":[{"@type":"WebSite"},{"@type":"Restaurant",'
            '"openingHours":["Mo-Fr 11:00-14:00 17:00-21:00","Sa 12:00-22:00"]}]}</script>')
    assert _p(html) == ("Mon–Fri 11am–2pm, 5pm–9pm · Sat 12pm–10pm · Sun Closed", "jsonld")


def test_microdata():
    html = '<time itemprop="openingHours" datetime="Tu-Su 11:30-21:00">Tue–Sun 11:30–9</time>'
    assert _p(html) == ("Mon Closed · Tue–Sun 11:30am–9pm", "microdata")


def test_text_stops_at_happy_hour():
    html = ('<h3>Hours</h3><p>Mon - Thu: 11am - 9pm</p><p>Fri - Sat: 11am - 10:30pm</p><p>Sunday: Closed</p>'
            '<h3>Happy Hour</h3><p>Mon-Fri 3-5pm</p>')
    assert _p(html) == ("Mon–Thu 11am–9pm · Fri–Sat 11am–10:30pm · Sun Closed", "text")


def test_text_variants():
    assert _p("<p>Hours: Tuesday-Sunday 11:30 a.m. to 3 p.m. & 5 p.m. to 9 p.m. Monday Closed</p>")[0] == \
        "Mon Closed · Tue–Sun 11:30am–3pm, 5pm–9pm"
    assert _p("<p>Open daily 11am-10pm</p>")[0] == "Mon–Sun 11am–10pm"
    assert _p("<p>Hours: Mon-Thu 11-9 Fri-Sat 11-10 Sun 12-8</p>")[0] == "Mon–Thu 11am–9pm · Fri–Sat 11am–10pm · Sun 12pm–8pm"
    assert _p("<p>Hours: Lunch Mon-Fri 11-2:30, Dinner Mon-Sat 5-10</p>")[0] == \
        "Mon–Fri 11am–2:30pm, 5pm–10pm · Sat 5pm–10pm · Sun Closed"
    assert _p("<p>HOURS Sun-Thu 11:00-22:00 Fri-Sat 11:00-23:00</p>")[0] == \
        "Mon–Thu 11am–10pm · Fri–Sat 11am–11pm · Sun 11am–10pm"


def test_text_rejects_non_hours():
    assert _p("<p>Call us at 704-555-1212. Open 7 days. Hours vary.</p>") == (None, None)
    assert _p("<p>Hours: Mon 11-9</p>") == (None, None)            # one day isn't a week
    assert _p("<p>Hours: Mon-Fri 2024-2026</p>") == (None, None)


def test_note_hours_keeps_then_ages_out():
    rec = {}
    C.note_hours(rec, "a.com", {"reached": True, "hours": {"w": [[[660, 1260]]] * 7, "how": "text"}}, "2026-10-01")
    C.note_hours(rec, "a.com", {"reached": True}, "2026-11-01")
    assert "a.com" in rec
    C.note_hours(rec, "a.com", {"reached": True}, "2026-12-15")
    assert "a.com" not in rec
    C.note_hours(rec, "b.com", {"reached": True, "hours": {"w": [[[660, 1260]]] * 7, "how": "text"}}, "2026-10-01")
    C.note_hours(rec, "b.com", {"reached": True, "closed": True}, "2026-10-02")
    assert "b.com" not in rec
