"""One-off checks of free data sources, run on GitHub (python -m menucheck.probe). Prints timings and counts only."""
import json
import time
import urllib.request

import requests


def t(label, fn):
    t0 = time.time()
    try:
        out = fn()
        print(f"{label}: OK in {time.time() - t0:.0f}s -> {out}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"{label}: FAILED in {time.time() - t0:.0f}s -> {type(e).__name__}: {str(e)[:300]}", flush=True)


def web(q):
    r = requests.get("https://crt.sh/", params={"q": q, "output": "json", "exclude": "expired"}, timeout=(15, 120),
                     headers={"User-Agent": "KnewdleNOW-MenuCheck/1.0"})
    if r.status_code != 200:
        return f"HTTP {r.status_code}"
    rows = r.json()
    names = {n for row in rows for n in str(row.get("name_value", "")).split("\n")}
    return f"{len(rows)} certs, e.g. {sorted(names)[:8]}"


def db(sql, arg):
    import psycopg
    with psycopg.connect(host="crt.sh", port=5432, dbname="certwatch", user="guest", connect_timeout=30,
                         autocommit=True) as c, c.cursor() as cur:
        cur.execute("SET statement_timeout = '150s'")
        cur.execute(sql, (arg,))
        rows = cur.fetchall()
    return f"{len(rows)} rows, e.g. {rows[:8]}"


FTS = """SELECT DISTINCT lower(cai.NAME_VALUE) FROM certificate_and_identities cai
         WHERE plainto_tsquery('certwatch', %s) @@ identities(cai.CERTIFICATE)
           AND x509_notAfter(cai.CERTIFICATE) > now() LIMIT 5000"""
PREFIX = """SELECT DISTINCT lower(cai.NAME_VALUE) FROM certificate_and_identities cai
            WHERE lower(cai.NAME_VALUE) LIKE %s LIMIT 5000"""


def commoncrawl():
    import duckdb
    info = json.load(urllib.request.urlopen("https://index.commoncrawl.org/collinfo.json", timeout=60))
    crawl = info[0]["id"]
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; SET s3_region='us-east-1';")
    path = f"s3://commoncrawl/cc-index/table/cc-main/warc/crawl={crawl}/subset=warc/part-00000-*.parquet"
    t0 = time.time()
    n = con.execute(f"SELECT count(*) FROM read_parquet('{path}')").fetchone()[0]
    rows = con.execute(f"""SELECT DISTINCT url_host_registered_domain FROM read_parquet('{path}')
                           WHERE url_host_registered_domain LIKE '%ramen%' LIMIT 50""").fetchall()
    return f"{crawl}: one file has {n} urls; {len(rows)} ramen domains e.g. {[r[0] for r in rows[:10]]} ({time.time()-t0:.0f}s)"


TSQ = """SELECT lower(cai.NAME_VALUE), min(x509_notBefore(cai.CERTIFICATE))::date FROM certificate_and_identities cai
         WHERE to_tsquery('certwatch', %s) @@ identities(cai.CERTIFICATE)
           AND x509_notAfter(cai.CERTIFICATE) > now() GROUP BY 1 LIMIT 20000"""


def db_names(q):
    import psycopg
    with psycopg.connect(host="crt.sh", port=5432, dbname="certwatch", user="guest", connect_timeout=30,
                         autocommit=True) as c, c.cursor() as cur:
        cur.execute("SET statement_timeout = '170s'")
        cur.execute(TSQ, (q,))
        rows = cur.fetchall()
    ramen = sorted({r[0] for r in rows if "ramen" in r[0]})
    return f"{len(rows)} names, {len(ramen)} with ramen, e.g. {ramen[:25]}"


if __name__ == "__main__":
    t("tsquery nemar:* (names ending in ramen)", lambda: db_names("nemar:*"))
    t("tsquery ramen:* (names starting with ramen)", lambda: db_names("ramen:*"))
    t("tsquery ramen", lambda: db_names("ramen"))
