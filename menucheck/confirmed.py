"""Applies the developer's "still open" confirmations (Knewdle Pulse) to the published docs/closed.json right away,
without the rest of the daily closed-shops run. Run every 30 minutes by .github/workflows/confirmed-open.yml.

Undoing a confirmation can't bring a warning back here (it isn't in the file any more); the next daily run does.
"""
from __future__ import annotations

import json
import sys

from .closed import EXTRA, OUT, Confirmed, Listed, apply_confirmed, confirmed_open, listed_places, load_json


def main() -> int:
    confirmed = confirmed_open()
    if confirmed is None:
        return 0
    out = load_json(OUT, None)
    if not out:
        print("no closed.json yet")
        return 0
    n = apply_confirmed(out, Confirmed(confirmed, Listed(listed_places(load_json(EXTRA, {})))))
    print(f"{len(confirmed)} confirmed open; {n} warning(s) dropped now")
    if n:
        OUT.write_text(json.dumps(out, separators=(",", ":"), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
