"""Smarter name matching on Overture's free open data.

The app already shows Overture places named or filed as "ramen". This finds the ones it misses:
  * name: other ramen words (tsukemen, mazesoba, chuka soba, 中華そば, 麺屋…) and ramen chains whose
    names don't say "ramen" (Afuri, Mensho, Tsujita, Daikokuya…)
  * brand: Overture's brand tag is a known ramen brand (from OpenStreetMap's open brand list,
    github.com/osmlab/name-suggestion-index)
  * site: the place's own website address says ramen (kaiyoramen.com), even if the name doesn't
  * noodle: a Japanese restaurant named "... Noodle Bar/House" (not udon/soba)

Everything comes from the same Overture download the menu check uses: no new source, no cost.
"""
from __future__ import annotations

import re

from .places import RAMEN_IN_DOMAIN, host_of

# The app's own rule (Models.kt RamenMatch): places matching it are already on the map.
APP_NAME = re.compile(
    r"(?<![a-z])(?:ramen|ramyun|ラーメン|らーめん|拉麺|拉麵|tonkotsu|ichiran|ippudo|menya|men-ya|jinya|kizuki|"
    r"santouka|hinodeya|totto)",
    re.IGNORECASE,
)
EXTRA_WORDS = re.compile(
    r"(?<![a-z])(?:tsukemen|tsuke-men|mazesoba|maze-soba|mazemen|abura[ -]?soba|chuka[ -]?soba|chukasoba|"
    r"tantanmen|ramyeon|ramenya|らあめん|ら～めん|中華そば|麺屋|拉面|라멘|라면)",
    re.IGNORECASE,
)
# US ramen chains whose names usually don't include "ramen".
CHAIN_WORDS = re.compile(
    r"(?<![a-z])(?:afuri|mensho|marufuku|tsujita|daikokuya|shin[- ]?sen[- ]?gumi|hironori|rakkan|momosan|"
    r"kinton|yoshiharu|ichicoro|ikkousha|misoya|tatsu-ya|kyuramen|hachiban|ajisen|tenkaippin|danbo)(?![a-z])",
    re.IGNORECASE,
)
# Ramen brands in OpenStreetMap's name-suggestion-index (cuisine=ramen), by Wikidata id.
RAMEN_BRANDS = {
    "Q109237427": "AFURI", "Q111204601": "Bone Daddies", "Q11326388": "Hachiban Ramen", "Q6065417": "Ippudo",
    "Q16997755": "JINYA Ramen Bar", "Q137644118": "Kizuki Ramen", "Q125870474": "Oishi Ramen",
    "Q130221273": "Ramen Danbo", "Q17210103": "Santouka", "Q110423710": "Shoryu", "Q109859392": "Tonkotsu",
    "Q11265679": "Kurumaya Ramen", "Q115113303": "Yamaokaya", "Q135989251": "Kagetsu Arashi",
    "Q11266830": "Ichiran", "Q99735903": "Marugen Ramen", "Q4699761": "Ajisen Ramen",
    "Q11442172": "Tenkaippin", "Q11523703": "Rairaitei",
}
NOODLE = re.compile(r"(?<![a-z])noodles?(?![a-z])", re.IGNORECASE)
NOT_RAMEN_NOODLE = re.compile(
    r"udon|soba|pho|phở|thai|viet|chinese|china|lo mein|lanzhou|hand[- ]?pulled|dumpling|pad|wok|"
    r"szechuan|sichuan|hunan|cantonese|taiwan|korean|naeng|knife|rice noodle|mein|mian",
    re.IGNORECASE,
)


def why(name: str, cat: str, websites: list[str], brand_wd: str | None, brand_name: str | None) -> str | None:
    """How a place shows it's ramen, or None. 'app' means the app already finds it on its own."""
    if APP_NAME.search(name) or "ramen" in cat:
        return "app"
    if EXTRA_WORDS.search(name) or CHAIN_WORDS.search(name):
        return "name"
    if (brand_wd and brand_wd in RAMEN_BRANDS) or (brand_name and (APP_NAME.search(brand_name) or CHAIN_WORDS.search(brand_name))):
        return "brand"
    for w in websites:
        h = host_of(w)
        # The restaurant's own domain (not a page on a big platform).
        if h and RAMEN_IN_DOMAIN.search(h.split(".")[-2] if h.count(".") >= 1 else h):
            return "site"
    if NOODLE.search(name) and "japanese" in cat and not NOT_RAMEN_NOODLE.search(name):
        return "noodle"
    return None


def fetch_ramen_places(release: str | None = None, limit: int | None = None) -> tuple[str, list[dict]]:
    """All US places in Overture that show ramen in any of the ways above (including ones the app finds)."""
    from .candidates import PLACES, _category_expr, _columns, _connect, latest_release  # needs duckdb
    release = release or latest_release()
    path = PLACES.format(release=release)
    con = _connect()
    cols = _columns(con, path)
    cat = _category_expr(cols)
    has_brand = "brand" in cols
    brand_wd = "brand.wikidata" if has_brand else "NULL"
    brand_name = 'brand.names."primary"' if has_brand and "names" in cols.get("brand", "") else "NULL"
    closed = "AND coalesce(operating_status, 'open') NOT LIKE '%closed%'" if "operating_status" in cols else ""
    lim = f"LIMIT {int(limit)}" if limit else ""
    words = ("ramen|ramyun|ラーメン|らーめん|拉麺|拉麵|tonkotsu|ichiran|ippudo|menya|men-ya|jinya|kizuki|santouka|"
             "hinodeya|totto|tsuke|maze|abura|chuka|tantanmen|ramyeon|らあめん|ら～めん|中華そば|麺屋|拉面|라멘|라면|"
             "afuri|mensho|marufuku|tsujita|daikokuya|sen-gumi|sengumi|sen gumi|hironori|rakkan|momosan|kinton|"
             "yoshiharu|ichicoro|ikkousha|misoya|tatsu-ya|hachiban|ajisen|tenkaippin|danbo|noodle")
    brand_ids = ",".join(f"'{q}'" for q in RAMEN_BRANDS)
    brand_sql = f"OR {brand_wd} IN ({brand_ids})" if has_brand else ""
    sql = f"""
        WITH p AS (
            SELECT
                id,
                names."primary" AS name,
                {cat} AS cat,
                websites,
                {brand_wd} AS brand_wd,
                {brand_name} AS brand_name,
                (bbox.xmin + bbox.xmax) / 2 AS lon,
                (bbox.ymin + bbox.ymax) / 2 AS lat,
                addresses[1].freeform AS street,
                addresses[1].locality AS city,
                addresses[1].region AS region,
                addresses[1].country AS country
            FROM read_parquet('{path}', hive_partitioning=1)
            WHERE bbox.xmin BETWEEN -180 AND -60
              AND bbox.ymin BETWEEN 15 AND 72
              AND coalesce(confidence, 1) >= 0.35
              {closed}
        )
        SELECT * FROM p
        WHERE country = 'US'
          AND (regexp_matches(lower(coalesce(name, '')), '{words}')
               OR regexp_matches(cat, 'ramen')
               OR regexp_matches(lower(coalesce(array_to_string(websites, ' '), '')), 'ramen')
               {brand_sql})
        {lim}
    """
    rows = con.execute(sql).fetchall()
    names = [d[0] for d in con.description]
    out = []
    for row in rows:
        r = dict(zip(names, row))
        if not r["name"]:
            continue
        sites = [w for w in (r["websites"] or []) if w]
        reason = why(r["name"], r["cat"] or "", sites, r["brand_wd"], r["brand_name"])
        if not reason:
            continue
        out.append({
            "id": r["id"],
            "name": r["name"],
            "lat": round(float(r["lat"]), 6),
            "lon": round(float(r["lon"]), 6),
            "address": ", ".join(x for x in (r["street"], r["city"], r["region"]) if x) or None,
            "website": sites[0] if sites else None,
            "f": reason,
        })
    return release, out
