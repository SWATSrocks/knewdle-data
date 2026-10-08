"""Decides from a restaurant's own menu text whether it serves ramen.

Only facts are kept (how many ramen dishes, which kind of place), never the menu itself.

    shop    ramen is a main part of the menu (>= 5 ramen dishes making up a real share of it)
    serves  at least 2 real ramen dishes
    none    0 or 1 mentions (a one-off "ramen bowl" special or "ramen salad" doesn't count)
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# A dish line must contain one of these. "ramen" can't start mid-word (Sacramento), but may be
# followed by letters (Ramenya). Specific styles count even without the word "ramen".
RAMEN_TERMS = re.compile(
    r"(?<![a-z])ramen|ラーメン|らーめん|拉麺|拉麵"
    r"|tonkotsu|tsukemen|tantan ?men|tan tan men|tantanmen|mazemen|maze ?soba|abura ?soba"
    r"|(?:shoyu|shio|miso|paitan|chintan|tori ?paitan|tonkotsu|spicy|vegan|veggie|black garlic)\s+ramen",
    re.IGNORECASE,
)

# Lines that mention ramen but aren't a bowl of ramen.
NOT_A_BOWL = re.compile(
    r"ramen\s+(?:salad|slaw|burger|bun|crunch|crusted|crumble|noodle salad|chips|bites|fries|pizza|"
    r"taco|egg roll|eggroll|wings?|crisps?|bar\b|bars\b|cookie|brittle|seasoning)"
    r"|instant ramen|cup (?:of )?ramen|top ramen|maruchan|ramen (?:week|night|tuesday|monday|wednesday|thursday|friday)"
    r"|(?:sold out|coming soon|ask about)",
    re.IGNORECASE,
)

# Something that looks like a price: $12, 12.95, 14.
PRICE = re.compile(r"(?:\$\s?\d{1,3}(?:\.\d{2})?|(?<![\d.])\d{1,3}\.\d{2}(?!\d))")

MAX_LINE = 160


@dataclass
class Verdict:
    kind: str            # "shop" | "serves" | "none"
    dishes: int          # distinct ramen dish lines
    menu_items: int      # rough count of priced lines (menu size)
    examples: list[str]  # up to 3 dish names, for the log only (never published)


def _lines(text: str) -> list[str]:
    out = []
    for raw in re.split(r"[\r\n]+|(?<=\S)\s{3,}(?=\S)|\s*[•·|]\s*", text):
        line = re.sub(r"\s+", " ", raw).strip()
        if 2 < len(line) <= MAX_LINE:
            out.append(line)
    return out


def _dish_key(line: str) -> str:
    # "Tonkotsu Ramen  $16" and "TONKOTSU RAMEN ...... 16.00" are the same dish.
    key = PRICE.sub("", line.lower())
    key = re.sub(r"[^a-z぀-ヿ一-鿿 ]+", " ", key)
    return re.sub(r"\s+", " ", key).strip()[:60]


def classify(text: str) -> Verdict:
    lines = _lines(text)
    priced = sum(1 for l in lines if PRICE.search(l))
    dishes: dict[str, str] = {}
    for line in lines:
        if not RAMEN_TERMS.search(line) or NOT_A_BOWL.search(line):
            continue
        # A section heading like "RAMEN" alone isn't a dish, but tells us there is a ramen section.
        key = _dish_key(line)
        if not key or key in ("ramen", "ramen bowls", "ramen menu", "our ramen", "ramen noodle soup"):
            continue
        # Skip long marketing sentences ("We make the best ramen in town since 2009 and ...").
        if len(line) > 110 and not PRICE.search(line):
            continue
        dishes.setdefault(key, line)

    n = len(dishes)
    size = max(priced, n)
    if n >= 5 and (size == 0 or n / size >= 0.25):
        kind = "shop"
    elif n >= 2:
        kind = "serves"
    else:
        kind = "none"
    return Verdict(kind, n, priced, list(dishes.values())[:3])
