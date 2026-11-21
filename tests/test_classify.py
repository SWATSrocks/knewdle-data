

def test_styles_come_from_ramen_dishes_only():
    from menucheck.classify import classify
    menu = ("Rolls\nCalifornia Roll $8\nMiso Soup $3\nSpicy Tuna $9\nTsukemen $16\nthick noodles, dipping broth\n"
            "Teriyaki Chicken $13\nEdamame $5\nGyoza $7\n")
    v = classify(menu, "Sakura")
    assert v.kind == "serves" and v.styles == ["tsukemen"]          # "Miso Soup" isn't a ramen style here
