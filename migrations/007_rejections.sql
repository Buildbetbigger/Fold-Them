-- 007_rejections.sql — opportunity-level rejections, logged with equal care.
-- Ticket T3 (build-plan §3). Schema: base spec §3 (#7) + P1 §4c (opportunity_key
-- included directly; fresh build, no ALTER).
-- rejection_code is free-text TEXT (no CHECK): the allowed set lives in
-- constants.RejectionCode (errata E4/E5 keep API_FAIL and CLOSE_MISSING out of it),
-- so there is no DB enum to drift. FK parent (raw_api_pulls) exists by 001.

CREATE TABLE rejections (
    rejection_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    rejected_ts     TEXT NOT NULL,
    stage           TEXT NOT NULL,          -- gate name
    pull_id         INTEGER REFERENCES raw_api_pulls(pull_id),
    event_id        TEXT,                   -- may be NULL at event-level failures
    sport_key       TEXT NOT NULL,
    commence_time   TEXT,
    market_key      TEXT,
    selection_raw   TEXT,
    sharp_book      TEXT,
    soft_book       TEXT,
    rejection_code  TEXT NOT NULL,          -- see constants.RejectionCode
    trigger_values  TEXT NOT NULL,          -- JSON
    opportunity_key TEXT                    -- nullable; correlates dedup-aware rejections
);
