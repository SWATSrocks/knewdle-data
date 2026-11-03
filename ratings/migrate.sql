-- One statement per line: the deploy runs each one and ignores "duplicate column" on later deploys.
ALTER TABLE installs ADD COLUMN city TEXT;
ALTER TABLE installs ADD COLUMN region TEXT;
ALTER TABLE installs ADD COLUMN lat REAL;
ALTER TABLE installs ADD COLUMN lon REAL;
