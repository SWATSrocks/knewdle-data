-- Knewdle NOW star ratings: one row per (shop, phone). Stars only, no text, no names.
CREATE TABLE IF NOT EXISTS ratings (
  shop    TEXT NOT NULL,      -- the app's shop key, e.g. "kairusushi.com@35.42,-80.86"
  device  TEXT NOT NULL,      -- random ID made by the app on install (not tied to a person)
  stars   INTEGER NOT NULL CHECK (stars BETWEEN 1 AND 5),
  updated TEXT NOT NULL,      -- ISO date of the last change
  PRIMARY KEY (shop, device)
);
CREATE INDEX IF NOT EXISTS ratings_by_device ON ratings (device, updated);

-- Simple abuse limits: how many ratings one network address sent today (address stored only as a hash).
CREATE TABLE IF NOT EXISTS limits (
  who   TEXT NOT NULL,
  day   TEXT NOT NULL,
  count INTEGER NOT NULL,
  PRIMARY KEY (who, day)
);

-- "This shop is closed" reports: one per (shop, phone). A shop is hidden for everyone once 2+ phones report it.
CREATE TABLE IF NOT EXISTS reports (
  shop    TEXT NOT NULL,
  device  TEXT NOT NULL,
  kind    TEXT NOT NULL DEFAULT 'closed',
  name    TEXT,                -- the shop's name as shown in the app (for the reviewer)
  updated TEXT NOT NULL,
  PRIMARY KEY (shop, device, kind)
);

-- Leaderboards (opt-in). Only a nickname and totals; never which shops or when.
CREATE TABLE IF NOT EXISTS lb_players (
  device      TEXT PRIMARY KEY,   -- random install ID (same one as ratings)
  nick        TEXT NOT NULL,
  nick_norm   TEXT NOT NULL UNIQUE,
  joined      TEXT NOT NULL,
  hidden      INTEGER NOT NULL DEFAULT 0,   -- 1 = removed from the boards by the app owner
  month       TEXT,                         -- "2026-10": the month month_shops counts
  month_shops INTEGER NOT NULL DEFAULT 0,
  all_shops   INTEGER NOT NULL DEFAULT 0,
  best_streak INTEGER NOT NULL DEFAULT 0,
  updated     TEXT
);
CREATE INDEX IF NOT EXISTS lb_by_month ON lb_players (month, month_shops);
CREATE INDEX IF NOT EXISTS lb_by_all ON lb_players (all_shops);
CREATE INDEX IF NOT EXISTS lb_by_streak ON lb_players (best_streak);

-- Install alerts for the developer: one row per phone (its random install ID), the first time the app opens.
-- The Play install source tag (e.g. "instagram" / "qr"), when, and (from Nov 2026) the city Cloudflare places the
-- phone's network in, rounded to ~1 km (city/region/lat/lon columns are added by migrate.sql). Nothing about the person.
CREATE TABLE IF NOT EXISTS installs (
  device TEXT PRIMARY KEY,
  src    TEXT NOT NULL,
  medium TEXT NOT NULL,
  day    TEXT NOT NULL,
  ts     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS installs_day ON installs (day);

-- Shop insights for the shops' monthly reports (Knewdle NOW 1.17+). Totals only, per shop per day:
-- seen (in someone's results), open (page opened), go ("Let's go!"), site, menu, share, call, stamp (verified visit),
-- bowl (a logged style). No device IDs, no locations of people, no history of who went where.
CREATE TABLE IF NOT EXISTS shop_stats (
  shop TEXT NOT NULL,
  day  TEXT NOT NULL,
  kind TEXT NOT NULL,
  n    INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (shop, day, kind)
);
CREATE INDEX IF NOT EXISTS shop_stats_day ON shop_stats (day);
CREATE TABLE IF NOT EXISTS shop_info (
  shop    TEXT PRIMARY KEY,
  name    TEXT NOT NULL,
  lat     REAL,
  lon     REAL,
  updated TEXT
);
-- New vs repeat visitors: v is a code that changes every month (a hash of the install ID, the month and a secret),
-- so a visitor can't be followed from one month to the next.
CREATE TABLE IF NOT EXISTS shop_visitors (
  shop  TEXT NOT NULL,
  month TEXT NOT NULL,
  v     TEXT NOT NULL,
  n     INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (shop, month, v)
);
-- Busiest times (stamps by weekday 0=Mon and hour) and what people ordered (logged bowl styles), per month.
CREATE TABLE IF NOT EXISTS shop_hours (
  shop  TEXT NOT NULL,
  month TEXT NOT NULL,
  dow   INTEGER NOT NULL,
  hour  INTEGER NOT NULL,
  n     INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (shop, month, dow, hour)
);
CREATE TABLE IF NOT EXISTS shop_bowls (
  shop  TEXT NOT NULL,
  month TEXT NOT NULL,
  style TEXT NOT NULL,
  n     INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (shop, month, style)
);
-- Shops the developer confirmed are still open (from Knewdle Pulse); they beat automatic "may be closed" warnings.
CREATE TABLE IF NOT EXISTS dev_open (
  id    TEXT PRIMARY KEY,
  entry TEXT NOT NULL,
  label TEXT,
  at    TEXT NOT NULL
);
