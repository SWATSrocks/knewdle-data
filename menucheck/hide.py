"""Apply not_ramen.txt right away: drop those places from docs/menu_ramen.json and rebuild the city pages.

    python -m menucheck.hide

Runs on every change to not_ramen.txt (see .github/workflows/not-ramen.yml), so a wrong place disappears within minutes
instead of waiting for the next daily run. The daily runs apply the same list too, so nothing comes back.
"""
from __future__ import annotations

import json

from . import citypages
from .discover import ROOT, is_not_ramen, load_not_ramen


def main() -> int:
    hidden = load_not_ramen()
    p = ROOT / "docs" / "menu_ramen.json"
    text = p.read_text()
    data = json.loads(text)
    before = len(data.get("more", []))
    # Vouched shops ("web" with v=1) are never hidden: someone checked them in person.
    data["more"] = [x for x in data.get("more", []) if x.get("v") == 1 or not is_not_ramen(x.get("n", ""), x.get("a"), hidden)]
    removed = before - len(data["more"])
    if removed:
        compact = "\n" not in text.strip()[:200]
        p.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")) if compact
                     else json.dumps(data, ensure_ascii=False, indent=2))
    print(f"not_ramen.txt: {len(hidden)} entries; removed {removed} place(s) from menu_ramen.json")
    citypages.build()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
