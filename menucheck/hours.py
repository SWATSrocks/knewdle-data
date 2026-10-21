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


# ---------- 3b. plain text, any layout ----------
# Reads the page as a stream of day words, time ranges and "closed", then makes sense of the common layouts:
#   "Mon-Thu: 11am-9pm | Fri & Sat 11am-10pm"   (days then times)
#   "Mon: Tue: Wed: … 10am-9pm 10am-9pm …"      (a row of days, then a row of times)
#   "11:00 am - 2:00 pm (Mon - Fri)"            (times then days)
#   "Open 7 days · Lunch 11-3 · Dinner 4-9"      (every day)
#   "Open 6 days a week 11 am to 9:30 pm, closed Tuesdays"

_DAY1 = r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tues|tue|wed|thurs|thrus|thur|thu|fri|sat|sun)s?"
_TIME1 = r"(?:\d{1,2}(?::\d{2})?\s*(?:am|pm)?|noon|midnight)"
_TOKEN = re.compile(
    rf"(?P<all>open (?:7|seven) days(?: a week)?|(?:7|seven) days a week|open daily|open every ?day|daily(?= \d)|every ?day(?= \d)|"
    rf"open (?:6|six) days(?: a week)?)"
    rf"|(?P<range>\b{_DAY1}\.?\s*(?:-|to|thru|through)\s*{_DAY1}\b\.?)"
    rf"|(?P<day>\b{_DAY1}\b\.?)"
    rf"|(?P<span>(?<![\d:]){_TIME1}\s*(?:-|to|until|till)\s*{_TIME1}(?![\d:]))"
    rf"|(?P<closed>\bclosed\b)", re.I)
_NOT_HOURS = re.compile(r"happy hour|happy-hour|kitchen|\bbar hours|delivery|holiday|thanksgiving|christmas|"
                        r"special|promo|\$|%|reserv", re.I)
_BAD_BEFORE = re.compile(r"happy hour|happy-hour|kitchen|\bbar hours|delivery|holiday|thanksgiving|christmas", re.I)
_ANCHOR = re.compile(r"hours|\bopen\b|business|contact|location|visit|store info|find us|we are|welcome", re.I)


def _norm(text: str) -> str:
    t = text.lower()
    t = re.sub(r"[\u200b\u00a0|]", " ", t)
    t = re.sub(r"[–—~]|--+", "-", t)
    t = re.sub(r"a\.m\.?", "am", t); t = re.sub(r"p\.m\.?", "pm", t)
    t = re.sub(r"(\d)\s*noon", r"\1pm", t)            # "12:00noon"
    t = re.sub(r"(\d:\d\d)\d+\s*(am|pm)", r"\1\2", t)   # "9:300am" typo
    t = re.sub(r"\b(\d{1,2})\.(\d{2})(?=\s*(?:am|pm|-))", r"\1:\2", t)   # "11.30 am"
    return re.sub(r"\s+", " ", t)


def _days_of(tok: str) -> list[int]:
    words = re.findall(_DAY1, tok)
    def idx(w):
        w = w.rstrip("s") if w.endswith("s") and w not in ("tues", "thurs", "thrus") else w
        w = {"thrus": "thurs"}.get(w, w)
        return DAYS.index(_WORD_TO_DAY.get(w, _WORD_TO_DAY.get(w[:3], "mo")))
    if len(words) == 2:
        i, j = idx(words[0]), idx(words[1])
        return [(i + k) % 7 for k in range(((j - i) % 7) + 1)]
    return [idx(words[0])]


def _clusters(t: str):
    toks = [(m.lastgroup, m.group(0), m.start(), m.end()) for m in _TOKEN.finditer(t)]
    cl, cur = [], []
    for tok in toks:
        if cur:
            gap = t[cur[-1][3]:tok[2]]
            if len(gap) > 45 or _NOT_HOURS.search(gap) or len(re.findall(r"[a-z]{3,}", gap)) > 5:
                cl.append(cur); cur = []
        cur.append(tok)
    if cur: cl.append(cur)
    return cl


def _read_cluster(t: str, cl) -> list | None:
    kinds = [k for k, *_ in cl]
    week = empty_week(); touched = set()
    every = any(k == "all" for k in kinds)
    # a row of days, then a row of times/closed of the same length
    i = 0
    while i < len(kinds) and kinds[i] in ("day", "range"): i += 1
    j = i
    while j < len(kinds) and kinds[j] in ("span", "closed"): j += 1
    if i >= 3 and j > i:
        if j - i != i:
            return None   # days and times don't line up one to one (lunch/dinner columns): don't guess
        for (k, tok, *_), (k2, tok2, *_) in zip(cl[:i], cl[i:i + i]):
            ds = _days_of(tok)
            touched |= set(ds)
            if k2 == "span":
                sp = _span(*re.split(r"\s*(?:-|to|until|till)\s*", tok2, maxsplit=1))
                if not sp:
                    return None   # a time we can't read: better no hours than wrong ones
                for d in ds: week[d].append(sp)
        return _tidy(week)
    pending: list[int] = []; prev = None; orphan = []; closed_next = False
    for n, (k, tok, a, b) in enumerate(cl):
        if k in ("day", "range"):
            ds = _days_of(tok)
            if len(touched) == 7 and set(ds) <= touched and prev == "span" and not t[cl[n - 1][3]:a].strip().startswith("("):
                break   # the week is done; what follows is a repeat ("Tue (today) …") or something else
            if closed_next:
                for d in ds: week[d] = []; touched.add(d)
                closed_next = False; prev = "closeddays"; continue
            if prev in ("day", "range") and re.fullmatch(r"\s*(?:,|&|and|/)?\s*", t[cl[n - 1][3]:a]):
                pending += ds
            elif prev == "span" and t[cl[n - 1][3]:a].strip().startswith("(") and orphan:
                for d in ds:
                    week[d] += orphan; touched.add(d)
                orphan = []; every = False; pending = []
                prev = "paren"; continue
            else:
                pending = list(ds)
            prev = k
        elif k == "span":
            sp = _span(*re.split(r"\s*(?:-|to|until|till)\s*", tok, maxsplit=1))
            if not sp:
                if pending and prev in ("day", "range"):
                    return None   # this day's time can't be read: better no hours than wrong ones
                prev = k; continue
            nxt = cl[n + 1] if n + 1 < len(cl) else None
            if nxt and nxt[0] in ("day", "range") and t[b:nxt[2]].strip().startswith("("):
                orphan = [sp]; prev = k; continue      # "11:00 am - 2:00 pm (Mon - Fri)": the days come next
            if pending and prev in ("day", "range", "span"):
                for d in pending:
                    if d in touched and prev in ("day", "range") and week[d] and not re.search(r"lunch|dinner|brunch", t[max(0, a - 40):a]):
                        week[d] = []
                    week[d].append(sp); touched.add(d)
            elif every or not pending:
                orphan.append(sp)
            prev = k
        elif k == "closed":
            if prev in ("day", "range"):
                for d in pending: week[d] = []; touched.add(d)
                pending = []
            elif prev in ("span", "all"):
                closed_next = True    # "11 am to 9:30 pm, closed Tuesdays"
            prev = k
        elif k == "all":
            every = True; prev = k
    if every and orphan:
        for d in range(7):
            if d not in touched: week[d] += orphan
        touched = set(range(7))
    if len(touched) < 3:
        return None
    return _tidy(week)


def from_text_any(text: str):
    t = _norm(text)
    best, best_score = None, 0
    for cl in _clusters(t):
        if not any(k == "span" for k, *_ in cl):
            continue
        before = t[max(0, cl[0][2] - 160):cl[0][2]]
        if _BAD_BEFORE.search(t[max(0, cl[0][2] - 30):cl[0][2]]):
            continue
        week = _read_cluster(t, cl)
        if not week or not valid(week):
            continue
        open_days = sum(1 for d in week if d)
        if not _ANCHOR.search(before) and open_days < 6:
            continue
        score = open_days * 10 + sum(map(len, week)) + (5 if _ANCHOR.search(before) else 0)
        if score > best_score:
            best, best_score = week, score
    return best


# ---------- 4. Toast restaurant websites ----------

def from_toast(raw: str):
    """Toast's websites keep hours in page data: "schedule":[{"days":["monday",…],"intervals":[{"startTime":"11:00:00.000",…}]}]."""
    found = []
    dec = json.JSONDecoder()
    for m in re.finditer(r'"schedule":\s*\[', raw):
        try:
            arr, _ = dec.raw_decode(raw, m.end() - 1)
        except ValueError:
            continue
        week = empty_week(); ok = False
        for sch in arr if isinstance(arr, list) else []:
            if not isinstance(sch, dict): continue
            for iv in sch.get("intervals") or []:
                o, c = str(iv.get("startTime", ""))[:5], str(iv.get("endTime", ""))[:5]
                sp = _span(o, c, clock24=True) if o and c else None
                if not sp: continue
                for d in sch.get("days") or []:
                    if str(d).lower() in _WORD_TO_DAY:
                        week[DAYS.index(_WORD_TO_DAY[str(d).lower()])].append(sp); ok = True
        if ok:
            found.append(_tidy(week))
    uniq = {json.dumps(w) for w in found}
    if len(uniq) == 1 and valid(found[0]):   # more than one schedule = a multi-location page: don't guess
        return found[0]
    return None


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
    raw = html.decode("utf-8", "replace") if isinstance(html, bytes) else html
    soup = BeautifulSoup(raw, "html.parser")
    for fn, how in ((from_jsonld, "jsonld"), (from_microdata, "microdata")):
        w = fn(soup)
        if w and valid(w): return w, how
    if '"schedule"' in raw:
        w = from_toast(raw)
        if w: return w, "toast"
    for tag in soup(["script", "style", "noscript"]): tag.decompose()
    text = soup.get_text(" ", strip=True)
    w = from_text(text)
    if w and valid(w): return w, "text"
    w = from_text_any(text)
    if w and valid(w): return w, "text2"
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
