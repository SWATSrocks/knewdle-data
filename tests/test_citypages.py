from menucheck.citypages import split_address, tz_for, slug, play_link, late_night


def test_split_address():
    assert split_address("1115 N Brevard St, Charlotte, NC") == ("1115 N Brevard St", "Charlotte", "NC")
    assert split_address("8625C Lindholm Dr, HUNTERSVILLE, North Carolina 28078") == ("8625C Lindholm Dr", "Huntersville", "NC")
    assert split_address("12 Main St, Suite 100, NC") is None or split_address("12 Main St, Suite 100, NC")[1] != "Suite 100"
    assert split_address("Tokyo, Japan") is None


def test_time_zones():
    assert tz_for("NC", 35.2, -80.8) == "America/New_York"
    assert tz_for("TX", 31.8, -106.4) == "America/Denver"      # El Paso
    assert tz_for("FL", 30.4, -87.2) == "America/Chicago"      # Pensacola
    assert tz_for("AZ", 33.4, -112.0) == "America/Phoenix"


def test_bits():
    assert slug("Winston-Salem") == "winston-salem" and slug("Kailua-Kona") == "kailua-kona"
    assert "utm_medium%3Dnc-charlotte" in play_link("nc-charlotte")
    assert late_night([[[660, 1380]]] + [[]] * 6) and not late_night([[[660, 1260]]] * 7)
