"""Decides from a restaurant's own menu text whether it serves ramen.

Only facts are kept (how many ramen dishes, which kind of place), never the menu itself.

    shop    ramen is a main part of the menu (>= 5 ramen dishes making up a real share of it)
    serves  at least 2 real ramen dishes, OR
            one ramen bowl offered with 2+ broth choices ("choice of tonkotsu, shoyu or miso"), OR
            one genuine ramen bowl: a named ramen broth plus 2+ classic toppings
            ("tonkotsu broth, pork belly, narutomaki, soft egg...")
    none    otherwise (a one-off "ramen bowl" special with no broth, "ramen salad", etc. don't count)
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


# Bump when the rules change, so places judged "none" under older rules get re-checked.
RULES_VERSION = 5

# Named ramen broths. CLASSIC ones are needed to count a bowl; the others only count as extra choices.
CLASSIC_BROTHS = {
    "tonkotsu": r"tonkotsu", "shoyu": r"shoyu", "shio": r"shio", "miso": r"miso",
    "paitan": r"(?:tori )?paitan", "tantan": r"tan ?tan(?: ?men)?",
}
OTHER_BROTHS = {
    "chicken": r"chicken", "veggie": r"vegan|vegetarian|veggie|vegetable", "spicy": r"spicy(?! (?:miso|shoyu|tonkotsu|shio|tan))",
    "curry": r"curry", "black garlic": r"black garlic", "beef": r"beef",
}
_BROTH_WORD = "|".join(list(CLASSIC_BROTHS.values()) + list(OTHER_BROTHS.values()))
# Something that clearly offers a choice: "choice of broth", "choose", "broth options", "tonkotsu or shoyu", "shio / miso".
CHOICE_CUE = re.compile(
    r"\b(?:choice|choices|choose|pick|select)\b|\bbroth (?:options|choices)\b|\bbroths\b"
    rf"|(?:{_BROTH_WORD})\w*(?: broth)?\s*(?:,\s*)?(?:\bor\b|/)\s*(?:spicy )?(?:{_BROTH_WORD})",
    re.IGNORECASE,
)
# Lines that describe options rather than being a dish of their own.
NOT_A_DISH = re.compile(r"^\W*(?:choice|choose|pick|select|add|add-ons?|extra|broth|toppings?)\b", re.IGNORECASE)
TOPPINGS = re.compile(
    r"chashu|char siu|pork belly|narutomaki|naruto|fish ?cake|ajitama|ajitsuke|soft[- ]?boiled egg|"
    r"hard ?boiled egg|boiled egg|soy egg|marinated egg|ramen egg|ajitsuke tamago|tamago|\begg\b|"
    r"nori|seaweed|menma|bamboo shoots?|bean ?sprouts|kikurage|wood ?ear|\bcorn\b|scallions?|green onions?|"
    r"negi|black garlic oil|mayu|chili oil|bok choy|spinach|kimchi|mushrooms?",
    re.IGNORECASE,
)
WINDOW = 4  # a dish's description usually fits on the next few lines


@dataclass
class Verdict:
    kind: str            # "shop" | "serves" | "none"
    dishes: int          # distinct ramen dish lines
    menu_items: int      # rough count of priced lines (menu size)
    examples: list[str]  # up to 3 dish names, for the log only (never published)
    broths: int = 0      # broth choices offered for a single ramen bowl (0 when not applicable)
    reason: str = ""     # which rule decided it (for the log)


def _broths_in(text: str) -> tuple[set[str], set[str]]:
    t = text.lower()
    classic = {k for k, pat in CLASSIC_BROTHS.items() if re.search(rf"(?<![a-z]){pat}", t)}
    other = {k for k, pat in OTHER_BROTHS.items() if re.search(rf"(?<![a-z])(?:{pat})", t)}
    return classic, other


def _single_bowl(lines: list[str]) -> tuple[str, int, str] | None:
    """Looks around each ramen mention for (a) 2+ broth choices or (b) a genuine bowl description."""
    best: tuple[str, int, str] | None = None
    for i, line in enumerate(lines):
        if not RAMEN_TERMS.search(line) and not re.fullmatch(r"\W*ramen\W*(?:\$?\s?\d.*)?", line, re.I):
            continue
        window_lines = lines[i:i + WINDOW]
        window = " ".join(window_lines)
        if NOT_A_BOWL.search(window_lines[0]):
            continue
        classic, other = _broths_in(window)
        if classic and CHOICE_CUE.search(window) and len(classic | other) >= 2:
            # "Choice of broth: tonkotsu, shoyu or spicy miso" -> broth choices
            n = len(classic | other)
            if best is None or best[0] != "choices" or n > best[1]:
                best = ("choices", n, line)
            continue
        if classic and len(set(m.lower() for m in TOPPINGS.findall(window))) >= 2 and re.search(
                r"(?<![a-z])ramen|ラーメン|noodles?", window, re.IGNORECASE):
            if best is None:
                best = ("genuine", 0, line)
    return best


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


NOODLE_NAME = re.compile(r"(?<![a-z])(?:ramen|noodle|broth|menya|men-ya|soba|udon|tsukemen|tonkotsu|ラーメン)", re.I)


def classify(text: str, name: str = "") -> Verdict:
    lines = _lines(text)
    priced = sum(1 for l in lines if PRICE.search(l))
    dishes: dict[str, str] = {}
    for line in lines:
        if not RAMEN_TERMS.search(line) or NOT_A_BOWL.search(line) or NOT_A_DISH.search(line):
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
    # "Ramen-focused" needs a menu we could actually size (8+ priced items) where ramen is a real share.
    focused_menu = priced >= 8 and n / size >= 0.25
    noodle_place = bool(NOODLE_NAME.search(name or ""))  # e.g. "Bao and Broth", "Menya Hana"
    if n >= 5 and (focused_menu or noodle_place):
        return Verdict("shop", n, priced, list(dishes.values())[:3], reason="5+ ramen dishes")
    if n >= 2:
        return Verdict("serves", n, priced, list(dishes.values())[:3], reason="2+ ramen dishes")
    single = _single_bowl(lines)
    if single:
        rule, broths, line = single
        return Verdict("serves", 1, priced, [line], broths=broths,
                       reason="1 bowl, %d broth choices" % broths if rule == "choices" else "1 genuine ramen bowl")
    return Verdict("none", n, priced, list(dishes.values())[:3])
