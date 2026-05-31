-- 003_snapshots_outcomes.sql — bookmaker_snapshots, market_outcomes, closing_lines.
-- Ticket T3 (build-plan §3). Schema: base spec §3 (#3, #4, #8).
-- Parents (raw_api_pulls, events, normalized_entities) exist by 001/002.
-- market_outcomes.event_id / bookmaker_key are denormalized (no FK, per base §3).

CREATE TABLE bookmaker_snapshots (
    snapshot_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    pull_id         INTEGER NOT NULL REFERENCES raw_api_pulls(pull_id),
    event_id        TEXT    NOT NULL REFERENCES events(event_id),
    bookmaker_key   TEXT    NOT NULL,
    bookmaker_title TEXT,
    market_key      TEXT    NOT NULL,
    api_last_update TEXT    NOT NULL,       -- provider per-book timestamp
    pull_timestamp  TEXT    NOT NULL        -- denormalized for fast freshness math
);

CREATE TABLE market_outcomes (
    outcome_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id       INTEGER NOT NULL REFERENCES bookmaker_snapshots(snapshot_id),
    event_id          TEXT    NOT NULL,     -- denormalized
    bookmaker_key     TEXT    NOT NULL,     -- denormalized
    market_key        TEXT    NOT NULL,
    outcome_name_raw  TEXT    NOT NULL,
    outcome_entity_id INTEGER REFERENCES normalized_entities(map_id),
    price_american    INTEGER,             -- nullable if API returns decimal only
    price_decimal     REAL    NOT NULL,
    point             REAL                 -- always NULL in v0.1; schema-forward
);

CREATE TABLE closing_lines (
    close_id                INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id                TEXT NOT NULL REFERENCES events(event_id),
    sport_key               TEXT NOT NULL,
    commence_time           TEXT NOT NULL,
    sharp_book              TEXT NOT NULL,
    close_pull_id           INTEGER REFERENCES raw_api_pulls(pull_id),
    close_capture_ts        TEXT,
    minutes_before_commence REAL,
    outcome_a_id            TEXT,           -- canonical_id
    outcome_a_decimal       REAL,
    outcome_a_novig         REAL,
    outcome_b_id            TEXT,
    outcome_b_decimal       REAL,
    outcome_b_novig         REAL,
    -- free-text by base §3 (no CHECK by design); values are enum-backed at the
    -- application layer via constants.CloseSourceFlag, which lands with closing.py (T13).
    close_source_flag       TEXT NOT NULL,  -- NORMAL|FROM_SUSPENSION|MISSING
    UNIQUE(event_id, sharp_book)
);
