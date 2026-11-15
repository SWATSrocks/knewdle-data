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
