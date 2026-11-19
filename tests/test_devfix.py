from menucheck import devfix


def test_address_fix_moves_the_listing_once():
    data = {"places": [{"n": "Pho Anh", "la": 37.393497, "lo": -122.079094, "a": "220 Castro St, Mountain View, CA"}],
            "more": [{"n": "Other", "la": 37.393497, "lo": -122.079094, "a": "x"}]}
    fix = {"n": "Pho Anh", "la": 37.393497, "lo": -122.079094, "a": "175 E El Camino Real Ste-C, Mountain View, CA 94040",
           "nla": 37.3791, "nlo": -122.0802}
    assert devfix.apply(data, [fix]) == 1
    p = data["places"][0]
    assert (p["a"], p["la"], p["lo"]) == (fix["a"], 37.3791, -122.0802)
    assert data["more"][0]["a"] == "x"                      # same spot, different name: untouched
    assert devfix.apply(data, [fix]) == 0                   # already fixed


def test_hand_fixes_file(tmp_path, monkeypatch):
    f = tmp_path / "address_fixes.txt"
    f.write_text("# note\nPho Anh | 37.393497, -122.079094 | 175 E El Camino Real Ste-C, Mountain View, CA 94040 | 37.38, -122.08\nbad line\n")
    monkeypatch.setattr(devfix, "FIX_LIST", f)
    assert devfix.hand_fixes() == [{"n": "Pho Anh", "la": 37.393497, "lo": -122.079094,
                                    "a": "175 E El Camino Real Ste-C, Mountain View, CA 94040", "nla": 37.38, "nlo": -122.08}]


def test_details_fix_sets_name_and_website_once():
    data = {"more": [{"n": "The Bowl", "la": 32.746185, "lo": -117.246445, "a": "4953 Newport Ave, San Diego, CA", "w": None},
                     {"n": "The Bowl", "la": 40.0, "lo": -75.0, "a": "elsewhere", "w": None}]}
    fix = {"n": "The Bowl", "la": 32.746185, "lo": -117.246445, "name": "The Bowl Ocean Beach", "w": "https://thebowlob.com"}
    assert devfix.apply_details(data, [fix]) == 2
    assert (data["more"][0]["n"], data["more"][0]["w"]) == ("The Bowl Ocean Beach", "https://thebowlob.com")
    assert data["more"][1]["n"] == "The Bowl"                  # same name, different place: untouched
    assert devfix.apply_details(data, [fix]) == 0              # already fixed (matches by the new name too)


def test_details_file(tmp_path, monkeypatch):
    f = tmp_path / "shop_details.txt"
    f.write_text("# note\nThe Bowl | 32.746185, -117.246445 | The Bowl Ocean Beach | thebowlob.com\nbad\n")
    monkeypatch.setattr(devfix, "DETAILS_LIST", f)
    assert devfix.details() == [{"n": "The Bowl", "la": 32.746185, "lo": -117.246445, "name": "The Bowl Ocean Beach",
                                 "w": "https://thebowlob.com"}]


def test_parse_hand_hours():
    w = devfix.parse_hand_hours("Fri-Sat 5pm-9pm")
    assert w == [[], [], [], [], [[1020, 1260]], [[1020, 1260]], []]
    w = devfix.parse_hand_hours("Mon, Wed & Fri 11:30am-2pm, 5pm-1am; Sun noon-8pm")
    assert w[0] == [[690, 840], [1020, 1500]] and w[2] == w[0] and w[4] == w[0] and w[1] == [] and w[6] == [[720, 1200]]
    assert devfix.parse_hand_hours("Fri-Mon 6pm-10pm")[0] == [[1080, 1320]]          # wraps past Sunday
    assert devfix.parse_hand_hours("sometimes") is None
    assert devfix.parse_hand_hours("Fri 5pm") is None


def test_hand_hours_go_to_pages_or_shops(tmp_path, monkeypatch):
    f = tmp_path / "hours_by_hand.txt"
    f.write_text("# note\nhttps://www.instagram.com/last_run_ramen/ | Fri-Sat 5pm-9pm\nmyshop.com | Mon-Sun 11am-9pm\nbad | ?\n")
    monkeypatch.setattr(devfix, "HOURS_LIST", f)
    shops, pages = {}, {}
    assert devfix.merge_hand_hours(shops, pages) == 2
    assert pages == {"instagram.com/last_run_ramen": [[], [], [], [], [[1020, 1260]], [[1020, 1260]], []]}
    assert list(shops) == ["myshop.com"]
    assert devfix.merge_hand_hours(shops, pages) == 0
