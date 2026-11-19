"""Applies the developer's Knewdle Pulse lists to the published docs/closed.json right away, without the rest of the
daily closed-shops run: "still open" drops a shop's warnings, "pull down" hides it (and undoing a pull brings it back), address fixes
rewrite the listing in menu_ramen.json.
Run every 30 minutes by .github/workflows/confirmed-open.yml. (Phones apply both lists live from the Worker's
/overrides; this keeps closed.json, the city pages and older app versions in step.)

Undoing a "still open" can't bring a warning back here (it isn't in the file any more); the next daily run does.
"""
from __future__ import annotations

import json
import sys

from .closed import (EXTRA, OPEN_LIST, OUT, Confirmed, Listed, apply_confirmed, apply_pulled, developer_lists,
                     listed_places, load_json, manual)


def main() -> int:
    lists = developer_lists()
    if lists is None:
        return 0
    dev_open, pulled, fixes = lists
    confirmed = dev_open + manual(OPEN_LIST)
    out = load_json(OUT, None)
    if not out:
        print("no closed.json yet")
        return 0
    listed = Listed(listed_places(load_json(EXTRA, {})))
    n = apply_confirmed(out, Confirmed(confirmed, listed))
    m = apply_pulled(out, pulled, listed)
    print(f"{len(confirmed)} confirmed open, {len(pulled)} pulled down; {n} warning(s) dropped; pulled list changed: {bool(m)}")
    if n or m:
        OUT.write_text(json.dumps(out, separators=(",", ":"), ensure_ascii=False))
    from . import devfix
    fixes = fixes + devfix.hand_fixes()
    f = devfix.apply_file(fixes)
    print(f"{len(fixes)} address fix(es); {f} listing(s) updated now")
    d = devfix.apply_details_file()
    print(f"name/website fixes: {d} change(s) now")
    f += d
    hh = devfix.apply_hand_hours_file()
    print(f"hand-kept hours: {hh} change(s) now")
    if n or m or f:
        from . import citypages
        citypages.build()
    return 0


if __name__ == "__main__":
    sys.exit(main())
