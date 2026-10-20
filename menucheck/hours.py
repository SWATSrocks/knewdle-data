"""Opening hours from a restaurant's own website.

Three ways, most reliable first:
  1. schema.org structured data (JSON-LD "openingHoursSpecification" / "openingHours"), which most restaurant
     website builders (BentoBox, Squarespace, Wix, Popmenu, Toast…) add for search engines;
  2. microdata (itemprop="openingHours");
  3. plain text near the word "Hours", like "Mon–Thu 11am–9pm · Fri–Sat 11am–10pm · Sun closed".

A week is 7 lists (Monday first) of [open, close] in minutes after midnight; close can go past 1440 for
after-midnight closing ("11am–2am" -> [660, 1560]). Anything that doesn't look like real restaurant hours is
dropped: it's better to show no hours than wrong ones.
"""
from __future__ import annotations

import json
import re

from bs4 import BeautifulSoup

DAYS = ["mo", "tu", "we", "th", "fr", "sa", "su"]
_DAY_WORDS = {
    "mo": ("monday", "mon", "mo"), "tu": ("tuesday", "tues", "tue", "tu"), "we": ("wednesday", "wed", "we"),
    "th": ("thursday", "thurs", "thur", "thu", "th"), "fr": ("friday", "fri", "fr"),
    "sa": ("saturday", "sat", "sa"), "su": ("sunday", "sun", "su"),
}
_WORD_TO_DAY = {w: d for d, ws in _DAY_WORDS.items() for w in ws}
DAY_RX = r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tues|tue|wed|thurs|thur|thu|fri|sat|sun)"
# 11am · 11:30 am · 11:30AM · 11 a.m. · 23:00 · noon · midnight
TIME_RX = r"(?:noon|midnight|\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)?)"
DASH_RX = r"\s*(?:-|–|—|to|until|till)\s*"


def empty_week() -> list[list[list[int]]]:
    return [[] for _ in range(7)]


def _minutes(tok: str, ampm_hint: str | None = None) -> int | None:
    t = tok.strip().lower().replace(".", "").replace(" ", "")
    if t == "noon": return 720
    if t == "midnight": return 1440
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?(am|pm)?", t)
    if not m: return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3) or ampm_hint
    if mi > 59 or h > 24: return None
    if ap == "am": h = 0 if h == 12 else h
    elif ap == "pm": h = h if h == 12 else h + 12
    elif h > 24: return None
    return h * 60 + mi


def _hour(tok: str) -> int:
    m = re.match(r"\s*(\d{1,2})", tok)
    return int(m.group(1)) if m else 0


def _span(open_tok: str, close_tok: str, clock24: bool = False) -> list[int] | None:
    """[open, close] from two times; fills in am/pm the way people write hours ("11-9" = 11am-9pm, "5-10" = 5pm-10pm)."""
    o_ap = re.search(r"(a|p)\.?m", open_tok.lower()); c_ap = re.search(r"(a|p)\.?m", close_tok.lower())
    o_ap = (o_ap.group(1) + "m") if o_ap else None; c_ap = (c_ap.group(1) + "m") if c_ap else None
    has_24h = clock24 or (not o_ap and not c_ap and (
        _hour(open_tok) > 12 or _hour(close_tok) > 12 or re.match(r"\s*0\d", open_tok) or re.match(r"\s*0\d", close_tok)))
    if has_24h:
        o, c = _minutes(open_tok), _minutes(close_tok)
    else:
        o = _minutes(open_tok, o_ap or ("pm" if c_ap == "pm" and _hour(open_tok) < _hour(close_tok) else "am"))
        if o_ap is None and (_hour(open_tok) == 12 or (c_ap is None and 1 <= _hour(open_tok) <= 6)):
            o = _minutes(open_tok, "pm")  # "12-8" opens at noon, "5-10" is dinner
        c = _minutes(close_tok, c_ap or "pm")
        if c_ap is None and _hour(close_tok) == 12 and ":" not in close_tok: c = 1440  # "11-12" closes at midnight
    if o is None or c is None: return None
    if c <= o: c += 1440  # closes after midnight
    if not (60 <= c - o <= 20 * 60) or c > 1440 + 6 * 60: return None
    return [o, c]


def _day_range(a: str, b: str | None) -> list[int]:
    i = DAYS.index(_WORD_TO_DAY[a.lower()]); j = DAYS.index(_WORD_TO_DAY[b.lower()]) if b else i
    return [(i + k) % 7 for k in range(((j - i) % 7) + 1)]


def valid(week) -> bool:
    open_days = sum(1 for d in week if d)
    if open_days < 3: return False
    for d in week:
        for o, c in d:
            if not (0 <= o < 1440 and o < c <= 1440 + 6 * 60): return False
    return True


# ---------- 1. JSON-LD ----------

def _walk(o):
    if isinstance(o, dict):
        yield o
        for v in o.values(): yield from _walk(v)
    elif isinstance(o, list):
        for v in o: yield from _walk(v)


def _spec_days(d) -> list[int]:
    out = []
    for x in (d if isinstance(d, list) else [d]):
        w = str(x).rsplit("/", 1)[-1].strip().lower()
        if w in _WORD_TO_DAY: out.append(DAYS.index(_WORD_TO_DAY[w]))
    return out


def _oh_string(s: str, week):
    """schema.org openingHours text: "Mo-Fr 11:00-21:00", "Mo,Tu,We 11:00-14:00 17:00-21:00", "Sa 11-22"."""
    for part in re.split(r"[;\n]|,(?=\s*(?:mo|tu|we|th|fr|sa|su)\b[^,]*\d)", s, flags=re.I):
        m = re.match(r"\s*((?:(?:mo|tu|we|th|fr|sa|su)\s*(?:-\s*(?:mo|tu|we|th|fr|sa|su))?\s*,?\s*)+)\s+(.*)", part, re.I)
        if not m: continue
        days = []
        for grp in re.findall(r"(mo|tu|we|th|fr|sa|su)\s*(?:-\s*(mo|tu|we|th|fr|sa|su))?", m.group(1), re.I):
            days += _day_range(grp[0], grp[1] or None)
        for a, b in re.findall(r"(\d{1,2}:?\d{0,2})\s*-\s*(\d{1,2}:?\d{0,2})", m.group(2)):
            a = a if ":" in a else a + ":00"; b = b if ":" in b else b + ":00"
            sp = _span(a, b, clock24=True)
            if sp:
                for d in days: week[d].append(sp)


def from_jsonld(soup: BeautifulSoup):
    week = empty_week(); found = False
    for tag in soup.find_all("script", type=lambda v: v and "ld+json" in v):
        try:
            data = json.loads(tag.string or tag.get_text() or "")
        except Exception:  # noqa: BLE001 - broken JSON-LD is common
            continue
        for o in _walk(data):
            spec = o.get("openingHoursSpecification")
            for sp in (spec if isinstance(spec, list) else [spec] if isinstance(spec, dict) else []):
                if not isinstance(sp, dict) or not sp.get("opens") or not sp.get("closes"): continue
                if sp.get("validThrough") or sp.get("validFrom"): continue  # holiday exceptions
                span = _span(str(sp["opens"])[:5], str(sp["closes"])[:5], clock24=True)
                if span:
                    for d in _spec_days(sp.get("dayOfWeek")): week[d].append(span); found = True
            oh = o.get("openingHours")
            for s in (oh if isinstance(oh, list) else [oh] if isinstance(oh, str) else []):
                _oh_string(s, week); found = True
    return _tidy(week) if found else None


# ---------- 2. microdata ----------

def from_microdata(soup: BeautifulSoup):
    week = empty_week(); found = False
    for el in soup.select('[itemprop="openingHours"]'):
        s = el.get("content") or el.get("datetime") or el.get_text(" ", strip=True)
        if s: _oh_string(s, week); found = True
    return _tidy(week) if found else None


# ---------- 3. plain text ----------

LINE_RX = re.compile(
    rf"(?P<d1>{DAY_RX})\.?(?:\s*(?:-|–|—|to|thru|through)\s*(?P<d2>{DAY_RX})\.?)?\s*[:,]?\s*"
    rf"(?P<rest>closed|(?:{TIME_RX}{DASH_RX}{TIME_RX}(?:\s*(?:&|and|,|/)\s*{TIME_RX}{DASH_RX}{TIME_RX})?))",
    re.I)


def from_text(text: str):
    """Hours written out on the page. Only looks right after the word "hours" (or "open"), and needs 2+ day groups."""
    t = re.sub(r"\s+", " ", text)
    best = None
    for m in re.finditer(r"\b(hours|open(?:ing)? hours|hours of operation|we(?:'| a)re open|open daily)\b", t, re.I):
        chunk = t[m.start(): m.start() + 600]
        week = empty_week(); groups = 0; seen = set()
        if re.search(r"open daily", chunk[:40], re.I):
            mm = re.search(rf"({TIME_RX}){DASH_RX}({TIME_RX})", chunk[:80], re.I)
            if mm:
                sp = _span(mm.group(1), mm.group(2))
                if sp: return _tidy([[sp] for _ in range(7)])
        last_end = 0
        for g in LINE_RX.finditer(chunk):
            days = _day_range(g.group("d1"), g.group("d2"))
            between = chunk[last_end:g.start()]
            if last_end and len(between) > 120: break  # hours lines sit together
            if re.search(r"happy|kitchen|\bbar\b|delivery|holiday|pick ?up|drive", between, re.I): break  # another list
            if seen & set(days) and not re.search(r"lunch|dinner|brunch", between + chunk[:40], re.I): break
            last_end = g.end()
            seen |= set(days); groups += 1
            rest = g.group("rest")
            if rest.lower() == "closed": continue
            for a, b in re.findall(rf"({TIME_RX}){DASH_RX}({TIME_RX})", rest, re.I):
                sp = _span(a, b)
                if sp:
                    for d in days: week[d].append(sp)
        if groups >= 2 and valid(_tidy(week)):
            cand = _tidy(week)
            if best is None or sum(map(len, cand)) > sum(map(len, best)): best = cand
    return best


def _tidy(week):
    out = []
    for d in week:
        spans = sorted({(o, c) for o, c in d})
        merged = []
        for o, c in spans:
            if merged and o <= merged[-1][1]: merged[-1][1] = max(merged[-1][1], c)
            else: merged.append([o, c])
        out.append(merged)
    return out


def parse(html: bytes | str):
    """(week, how) from a page, or (None, None)."""
    soup = BeautifulSoup(html, "html.parser")
    for fn, how in ((from_jsonld, "jsonld"), (from_microdata, "microdata")):
        w = fn(soup)
        if w and valid(w): return w, how
    for tag in soup(["script", "style", "noscript"]): tag.decompose()
    w = from_text(soup.get_text(" ", strip=True))
    if w and valid(w): return w, "text"
    return None, None


HOURS_LINK = re.compile(r"hours|contact|location|visit|find[- ]?us|about", re.I)


def fmt(week) -> str:
    """'Mon–Thu 11am–9pm\\nFri–Sat 11am–10pm\\nSun Closed' (for logs and tests)."""
    def t(m):
        m %= 1440; h, mi = divmod(m, 60); ap = "am" if h < 12 else "pm"; h = h % 12 or 12
        return f"{h}{':%02d' % mi if mi else ''}{ap}"
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    rows, i = [], 0
    while i < 7:
        j = i
        while j + 1 < 7 and week[j + 1] == week[i]: j += 1
        label = names[i] if i == j else f"{names[i]}–{names[j]}"
        rows.append(f"{label} " + (", ".join(f"{t(o)}–{t(c)}" for o, c in week[i]) or "Closed"))
        i = j + 1
    return "\n".join(rows)
