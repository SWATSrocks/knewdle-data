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


def test_osm_slim():
    from menucheck import osm
    d = {"elements": [
        {"type": "node", "id": 5, "lat": 35.1, "lon": -80.2, "tags": {"name": "Ramen A", "amenity": "restaurant", "opening_hours": "Mo-Su 11:00-21:00", "brand:wikidata": "Q1"}},
        {"type": "way", "id": 2, "center": {"lat": 35.2, "lon": -80.3}, "tags": {"name": "Ramen B", "website": "https://b.com"}},
        {"type": "node", "id": 9, "lat": 1, "lon": 2, "tags": {"amenity": "restaurant"}},
    ]}
    out = osm.slim(d)
    assert [(e["type"], e["id"]) for e in out] == [("node", 5), ("way", 2)]
    assert out[0]["tags"] == {"name": "Ramen A", "opening_hours": "Mo-Su 11:00-21:00"}
    assert out[1]["lat"] == 35.2


def test_osm_geojson_element():
    from menucheck import osm
    pt = {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-80.8, 35.2]},
          "properties": {"@type": "node", "@id": 42, "amenity": "restaurant", "name": "Sacramento Grill"}}
    assert osm.element(pt) is None
    pt["properties"]["name"] = "Menya Hosaki"
    assert osm.element(pt) == {"type": "node", "id": 42, "lat": 35.2, "lon": -80.8, "tags": {"name": "Menya Hosaki"}}
    poly = {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]]},
            "properties": {"@type": "area", "@id": 200, "amenity": "restaurant", "name": "Bowl", "cuisine": "japanese;ramen"}}
    e = osm.element(poly)
    assert (e["type"], e["id"], e["lat"], e["lon"]) == ("way", 100, 1.0, 1.0)


def test_text_any_layouts():
    f = lambda t: (H.fmt(H.from_text_any(t)).replace("\n", " · ") if H.from_text_any(t) else None)
    assert f("OPEN HOURS MON TUE WED THU FRI SAT SUN 11:30AM-9PM CLOSED 11:30AM-9PM 11:30AM-9PM 11:30AM-10PM 11:30AM-10PM 11:30AM-9PM") == \
        "Mon 11:30am–9pm · Tue Closed · Wed–Thu 11:30am–9pm · Fri–Sat 11:30am–10pm · Sun 11:30am–9pm"
    assert f("Sun - Thu : 11:00AM - 10:00PM Tuesday : Closed Fri - Sat : 11:00AM - 10:30PM") == \
        "Mon 11am–10pm · Tue Closed · Wed–Thu 11am–10pm · Fri–Sat 11am–10:30pm · Sun 11am–10pm"
    assert f("Hours 11:00 am - 2:00 pm (Mon - Fri) 11:00 am - 2:30 pm (Sat - Sun) 5:00 pm - 8:00 pm (Mon - Thu) 5:00 pm - 8:30 pm (Fri - Sun)") == \
        "Mon–Thu 11am–2pm, 5pm–8pm · Fri 11am–2pm, 5pm–8:30pm · Sat–Sun 11am–2:30pm, 5pm–8:30pm"
    assert f("HOURS Open 6 days a week 11 am to 9:30 pm Closed Tuesdays") == "Mon 11am–9:30pm · Tue Closed · Wed–Sun 11am–9:30pm"
    assert f("Opening hours: Mon – Fri : 11.00 AM – 2.30 PM 5.00 PM – 9.00 PM Sat – Sun : 11.30 AM – 3.00 PM 4.30 PM – 9.00 PM") == \
        "Mon–Fri 11am–2:30pm, 5pm–9pm · Sat–Sun 11:30am–3pm, 4:30pm–9pm"
    assert f("Store Info Closed MON 11:30 AM - 07:50 PM TUE 11:30 AM - 07:50 PM WED 11:30 AM - 07:50 PM THU 11:30 AM - 07:50 PM "
             "FRI 11:30 AM - 07:50 PM SAT 11:30 AM - 08:20 PM SUN 11:30 AM - 08:20 PM TUE ( Today ) 11:30 AM - 07:50 PM") == \
        "Mon–Fri 11:30am–7:50pm · Sat–Sun 11:30am–8:20pm"
    assert f("Business Hours Mon: Closed Tue: 11:00 am~8:45 pm Wed: 11:00 am~8:45 pm Thurs: 11:00 am~8:45 pm Fri: 11:00 am~9:15 pm "
             "Sat: 11:00 am~9:15 pm Sun: 11:00 am~8:45 pm") == "Mon Closed · Tue–Thu 11am–8:45pm · Fri–Sat 11am–9:15pm · Sun 11am–8:45pm"
    assert f("Happy Hour Mon-Fri 3-6pm") is None
    assert f("Monday - Thursday Friday Saturday Sunday Lunch 11.00 am - 3.00 pm Dinner 5.00 pm - 9.30 pm Lunch 11.00 am - 3.00 pm") is None
    assert f("Hours Mon-Thu 11am-10pm Fri 11am-11pm Saturday 12:00AM - 11:00PM Sunday 12pm-10pm") is None


def test_toast_schedule():
    raw = ('{"schedule":[{"__typename":"Schedule","days":["monday","tuesday","wednesday","thursday"],"intervals":[{"endTime":"22:00:00.000",'
           '"startTime":"11:00:00.000","overnight":false}]},{"days":["friday","saturday"],"intervals":[{"endTime":"01:00:00.000","startTime":"11:30:00.000"}]},'
           '{"days":["sunday"],"intervals":[{"endTime":"22:00:00.000","startTime":"11:30:00.000"}]}]}')
    assert H.fmt(H.from_toast(raw)).replace("\n", " · ") == "Mon–Thu 11am–10pm · Fri–Sat 11:30am–1am · Sun 11:30am–10pm"
    two = raw + raw.replace("11:00:00", "10:00:00")
    assert H.from_toast(two) is None


def test_page_key_and_owner_hours():
    assert C.page_key("https://www.JinyaRamenBar.com/locations/charlotte/") == "jinyaramenbar.com/locations/charlotte"
    assert C.page_key("https://noodles.com/") is None
    assert C.page_key("kurasushi.com/locations/houston?utm=x") == "kurasushi.com/locations/houston"
    assert H.fmt(C.owner_hours("Tue-Sun 11:30am-9pm")).replace("\n", " · ") == "Mon Closed · Tue–Sun 11:30am–9pm"
    assert C.owner_hours("Call us!") is None
