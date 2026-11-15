"""Address fixes from Knewdle Pulse ({n, la, lo} as published, then {a, nla, nlo}): applied to docs/menu_ramen.json so
older app versions and the city pages show the right address and spot. (Phones on 1.17.2+ also apply them live.)"""
from __future__ import annotations

import json
from pathlib import Path

MENU = Path(__file__).resolve().parent.parent / "docs" / "menu_ramen.json"
NEAR = 2e-4   # ~20 m: the published spot the fix was made against


def _hit(p: dict, f: dict) -> bool:
    if (p.get("n") or "").strip().lower() != (f.get("n") or "").strip().lower():
        return False
    try:
        la, lo = float(p["la"]), float(p["lo"])
    except (KeyError, TypeError, ValueError):
        return False
    return (abs(la - f["la"]) < NEAR and abs(lo - f["lo"]) < NEAR) or (abs(la - f["nla"]) < NEAR and abs(lo - f["nlo"]) < NEAR)


def apply(data: dict, fixes: list[dict]) -> int:
    """Rewrites matching listings in a menu_ramen.json dict; returns how many changed."""
    changed = 0
    fixes = [f for f in fixes if all(k in f for k in ("n", "la", "lo", "a", "nla", "nlo"))]
    for part in ("places", "more"):
        for p in data.get(part, []):
            for f in fixes:
                if _hit(p, f) and (p.get("a") != f["a"] or p.get("la") != f["nla"] or p.get("lo") != f["nlo"]):
                    p["a"], p["la"], p["lo"] = f["a"], f["nla"], f["nlo"]
                    changed += 1
    return changed


def apply_file(fixes: list[dict]) -> int:
    if not fixes or not MENU.exists():
        return 0
    raw = MENU.read_text()
    data = json.loads(raw)
    n = apply(data, fixes)
    if n:
        compact = "\n" not in raw.strip()
        MENU.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")) if compact
                        else json.dumps(data, ensure_ascii=False, indent=1))
    return n
