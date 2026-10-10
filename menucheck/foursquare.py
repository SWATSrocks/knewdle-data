"""Ramen restaurants from Foursquare's open places data (FSQ OS Places, Apache 2.0).

Read straight from Foursquare's dataset on Hugging Face with DuckDB (only the columns and rows needed).
Needs the HF_TOKEN secret (a free Hugging Face account approved for the dataset); without it this step
is skipped. Kept: US places filed as "Ramen Restaurant" or named like ramen, not closed, recently confirmed.

Credit: "Contains data from Foursquare Open Source Places, © Foursquare Labs, Inc., Apache License 2.0."
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import urllib.request

from .names import CHAIN_WORDS, EXTRA_WORDS
from .places import RAMEN_WORD

REPO = "foursquare/fsq-os-places"
API = f"https://huggingface.co/api/datasets/{REPO}/tree/main/release"
RULES_VERSION = 4
MAX_AGE_YEARS = 3   # places nobody has confirmed in this long are often gone
# Filed as ramen but named for another cuisine: usually a mis-filed listing.
OTHER_CUISINE = re.compile(r"^pho|(?<![a-z])(?:pho|phở|taco|taqueria|pizza|pizzeria|burger|bbq|barbecue|wings?|mexican|"
                           r"cantina|tex-mex|bagel|donut|doughnut|pancake|steakhouse|seafood boil|crawfish|hoagies?|sandwich(?:es)?|subs?|cheesesteaks?|deli|bagels?|pretzels?|"
                           r"ice cream|gelato|boba|bubble tea|smoothies?|juice)(?![a-z])",
                           re.IGNORECASE)
CREDIT = "Contains data from Foursquare Open Source Places, © Foursquare Labs, Inc. (Apache License 2.0)"


def token() -> str | None:
    t = os.environ.get("HF_TOKEN", "").strip()
    return t or None


def _get_json(url: str):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token()}",
                                               "User-Agent": "KnewdleNOW-MenuCheck/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def latest_release() -> str:
    """Newest 'dt=YYYY-MM-DD' folder that has places data."""
    folders = sorted((e["path"].rsplit("dt=", 1)[-1] for e in _get_json(API)
                      if e.get("type") == "directory" and "dt=" in e.get("path", "")), reverse=True)
    if not folders:
        raise RuntimeError("No Foursquare releases listed (is the account approved for the dataset?)")
    return folders[0]


def notice_text() -> str | None:
    """Foursquare's NOTICE file, which the license asks us to pass along with the data."""
    for name in ("NOTICE.txt", "NOTICE"):
        try:
            req = urllib.request.Request(f"https://huggingface.co/datasets/{REPO}/resolve/main/{name}",
                                         headers={"Authorization": f"Bearer {token()}"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            continue
    return None


def fetch(release: str) -> list[dict]:
    import duckdb
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("CREATE SECRET hf (TYPE huggingface, TOKEN ?)", [token()])
    path = f"hf://datasets/{REPO}/release/dt={release}/places/parquet/*.parquet"
    cols = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{path}') LIMIT 0").fetchall()}

    def col(name, default="NULL"):
        return name if name in cols else default

    labels = "lower(array_to_string(fsq_category_labels, ' | '))" if "fsq_category_labels" in cols else "''"
    closed = "AND date_closed IS NULL" if "date_closed" in cols else ""
    flags = ("AND (unresolved_flags IS NULL OR len(unresolved_flags) = 0)" if "unresolved_flags" in cols else "")
    cutoff = (dt.date.today() - dt.timedelta(days=365 * MAX_AGE_YEARS)).isoformat()
    fresh = f"AND (date_refreshed IS NULL OR CAST(date_refreshed AS VARCHAR) >= '{cutoff}')" if "date_refreshed" in cols else ""
    words = ("ramen|ramyun|ラーメン|らーめん|tonkotsu|ichiran|ippudo|menya|men-ya|jinya|kizuki|santouka|hinodeya|"
             "totto|tsukemen|mazesoba|mazemen|abura|chuka|tantanmen|中華そば|麺屋|afuri|mensho|marufuku|tsujita|"
             "daikokuya|hironori|rakkan|momosan|yoshiharu|ichicoro|ikkousha|misoya|tatsu-ya|kyuramen|danbo")
    sql = f"""
        SELECT fsq_place_id AS id, name, latitude AS lat, longitude AS lon,
               {col('address')} AS street, {col('locality')} AS city, {col('region')} AS region,
               {col('website')} AS website, {labels} AS labels
        FROM read_parquet('{path}')
        WHERE country = 'US' AND latitude IS NOT NULL AND longitude IS NOT NULL
          {closed} {flags} {fresh}
          AND (regexp_matches({labels}, 'ramen')
               OR (regexp_matches({labels}, 'dining') AND regexp_matches(lower(coalesce(name, '')), '{words}')))
    """
    out = []
    for r in con.execute(sql).fetchall():
        pid, name, lat, lon, street, city, region, website, labels_txt = r
        if not name:
            continue
        filed = "ramen" in (labels_txt or "")
        named = bool(RAMEN_WORD.search(name) or EXTRA_WORDS.search(name) or CHAIN_WORDS.search(name))
        if not (filed or named):
            continue
        if not street:
            continue  # no street address: the pin is often just the middle of the city
        if not named and OTHER_CUISINE.search(name):
            continue
        site = website if website and website.startswith("http") else (f"http://{website}" if website else None)
        out.append({
            "id": f"fsq_{pid}",
            "name": name,
            "lat": round(float(lat), 6),
            "lon": round(float(lon), 6),
            "address": ", ".join(x for x in (street, city, region) if x) or None,
            "website": site,
            "f": "fsq",
        })
    return out


__all__ = ["CREDIT", "RULES_VERSION", "fetch", "latest_release", "notice_text", "token", "re"]
