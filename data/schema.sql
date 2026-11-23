-- Chief of Staff local database (SQLite). The .db file is gitignored; only this schema is public.
-- Note: WHOOP data is intentionally NOT stored (WHOOP API terms §4.2.2: no permanent copies).
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', '1');

-- What the owner ate. kcal/macros NULL until Claude estimates them.
CREATE TABLE IF NOT EXISTS food_entries (
  id          TEXT PRIMARY KEY,
  at          TEXT NOT NULL,              -- ISO-8601 with offset
  day         TEXT NOT NULL,              -- local YYYY-MM-DD
  text        TEXT NOT NULL,
  kcal        REAL, protein_g REAL, carbs_g REAL, fat_g REAL,
  supplement  INTEGER NOT NULL DEFAULT 0,
  source      TEXT NOT NULL DEFAULT 'claude'   -- claude | dashboard
);
CREATE INDEX IF NOT EXISTS food_day ON food_entries(day);

-- Daily nutrition targets (latest row wins).
CREATE TABLE IF NOT EXISTS nutrition_targets (
  set_at TEXT PRIMARY KEY, kcal REAL, protein_g REAL, carbs_g REAL, fat_g REAL
);

-- One row per night: the plan Claude made, and what actually happened.
CREATE TABLE IF NOT EXISTS sleep_plans (
  night       TEXT PRIMARY KEY,           -- local date the night starts
  bedtime     TEXT, wake TEXT, reason TEXT, away INTEGER DEFAULT 0,
  alarm_fired_at TEXT, alarm_dismissed_at TEXT, snoozes INTEGER DEFAULT 0
);

-- Daily weather summary for the home/current location (Open-Meteo, CC BY 4.0).
CREATE TABLE IF NOT EXISTS weather_daily (
  day TEXT PRIMARY KEY, place TEXT, hi REAL, lo REAL, kind TEXT, rain_pct REAL, alerts TEXT
);

-- Every BedJet command sent (scheduled or manual).
CREATE TABLE IF NOT EXISTS bedjet_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, tag TEXT, command TEXT, ok INTEGER, result TEXT
);

-- Daily snapshot of the dashboard headline/priorities/risks for looking back.
CREATE TABLE IF NOT EXISTS daily_briefs (
  day TEXT PRIMARY KEY, headline TEXT, priorities TEXT, risks TEXT, events INTEGER, first_event TEXT
);

-- Room air from a SwitchBot Meter Pro (CO2), read passively over Bluetooth every ~5 min.
CREATE TABLE IF NOT EXISTS air_readings (
  at TEXT PRIMARY KEY, co2 INTEGER, temp_c REAL, humidity INTEGER, battery INTEGER
);
