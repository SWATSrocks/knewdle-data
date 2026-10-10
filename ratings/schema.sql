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
