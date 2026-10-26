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
