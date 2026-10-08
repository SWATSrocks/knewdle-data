"""Finds places worth a menu check, straight from Overture Maps' free open data (no account needed).

Candidates are US restaurants Overture files under Japanese / sushi / Asian / noodle / izakaya-style
categories that have their own website, and whose name and category DON'T already say "ramen"
(Knewdle NOW finds those on its own).

Overture is renaming its category fields during 2026 ("categories" -> "basic_category" + "taxonomy"),
so the query looks at the actual columns first and uses whichever exist.
"""
from __future__ import annotations

import json
import re
import urllib.request

import duckdb

STAC_CATALOG = "https://stac.overturemaps.org/catalog.json"
PLACES = "s3://overturemaps-us-west-2/release/{release}/theme=places/type=place/*"

CATEGORY_HINT = r"japanese|sushi|asian|noodle|izakaya|teppanyaki|udon|soba|poke|korean|taiwanese|fusion|hawaiian"
RAMEN_NAME = re.compile(
    r"(?<![a-z])(?:ramen|ramyun|ラーメン|らーめん|拉麺|拉麵|tonkotsu|ichiran|ippudo|menya|men-ya|jinya|kizuki|"
    r"santouka|hinodeya|totto)",
    re.IGNORECASE,
)


def latest_release() -> str:
    with urllib.request.urlopen(STAC_CATALOG, timeout=30) as r:
        cat = json.load(r)
    rel = cat.get("latest")
    if not rel:
        raise RuntimeError("Overture catalog has no 'latest' release")
    return rel


def _connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2';")
    return con


def _columns(con, path: str) -> dict[str, str]:
    rows = con.execute(f"DESCRIBE SELECT * FROM read_parquet('{path}', hive_partitioning=1) LIMIT 0").fetchall()
    return {r[0]: str(r[1]) for r in rows}


def _category_expr(cols: dict[str, str]) -> str:
    parts = []
    tax = cols.get("taxonomy", "")
    if tax:
        # Only the place's own category and alternates, not the parent levels: Chinese, Thai and
        # Vietnamese restaurants all sit under "asian_restaurant" in the hierarchy, and rarely serve ramen.
        parts.append("coalesce(taxonomy.\"primary\", '')")
        for alt in ("alternates", "alternate"):
            if re.search(rf"\b{alt}\b", tax):
                parts.append(f"coalesce(array_to_string(taxonomy.{alt}, ' '), '')")
    if "basic_category" in cols:
        parts.append("coalesce(basic_category, '')")
    cats = cols.get("categories", "")
    if cats:
        parts.append("coalesce(categories.\"primary\", '')")
        if re.search(r"\balternate\b", cats):
            parts.append("coalesce(array_to_string(categories.alternate, ' '), '')")
    if not parts:
        raise RuntimeError(f"No category columns found in Overture places: {sorted(cols)}")
    return "lower(concat_ws(' ', " + ", ".join(parts) + "))"


def fetch_candidates(release: str | None = None, limit: int | None = None) -> tuple[str, list[dict]]:
    release = release or latest_release()
    path = PLACES.format(release=release)
    con = _connect()
    cols = _columns(con, path)
    cat = _category_expr(cols)
    closed = "AND coalesce(operating_status, 'open') NOT LIKE '%closed%'" if "operating_status" in cols else ""
    lim = f"LIMIT {int(limit)}" if limit else ""
    sql = f"""
        WITH p AS (
            SELECT
                id,
                names."primary" AS name,
                {cat} AS cat,
                websites[1] AS website,
                (bbox.xmin + bbox.xmax) / 2 AS lon,
                (bbox.ymin + bbox.ymax) / 2 AS lat,
                addresses[1].freeform AS street,
                addresses[1].locality AS city,
                addresses[1].region AS region,
                addresses[1].postcode AS postcode,
                addresses[1].country AS country,
                confidence
            FROM read_parquet('{path}', hive_partitioning=1)
            WHERE bbox.xmin BETWEEN -180 AND -60
              AND bbox.ymin BETWEEN 15 AND 72
              AND websites IS NOT NULL AND len(websites) > 0
              AND coalesce(confidence, 1) >= 0.35
              {closed}
        )
        SELECT * FROM p
        WHERE country = 'US'
          AND regexp_matches(cat, '{CATEGORY_HINT}')
          AND NOT regexp_matches(cat, 'ramen')
        {lim}
    """
    rows = con.execute(sql).fetchall()
    names = [d[0] for d in con.description]
    out = []
    for row in rows:
        r = dict(zip(names, row))
        if not r["name"] or not r["website"] or RAMEN_NAME.search(r["name"]):
            continue
        address = ", ".join(x for x in (r["street"], r["city"], r["region"]) if x)
        out.append({
            "id": r["id"],
            "name": r["name"],
            "lat": round(float(r["lat"]), 6),
            "lon": round(float(r["lon"]), 6),
            "address": address or None,
            "website": r["website"],
        })
    return release, out
