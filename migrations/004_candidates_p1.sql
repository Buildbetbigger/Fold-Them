-- 004_candidates_p1.sql — P1-revised candidates (the dedup-aware candidate table).
-- Ticket T3 (build-plan §3). Schema: P1 §4a (authoritative pre-implementation rebuild).
-- DEDUP GUARANTEE: inline UNIQUE(opportunity_key). Status CHECK enumerates the 7-state
-- lifecycle and MUST stay in lockstep with constants.Status (reconciled by a T3 test).
-- FK parents (audit_runs, raw_api_pulls, events) exist by 001/002.

CREATE TABLE candidates (
    candidate_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity_key          TEXT    NOT NULL UNIQUE,          -- DEDUP GUARANTEE
    audit_run_id             INTEGER NOT NULL REFERENCES audit_runs(audit_run_id),
    first_pull_id            INTEGER NOT NULL REFERENCES raw_api_pulls(pull_id),
    event_id                 TEXT    NOT NULL REFERENCES events(event_id),
    sport_key                TEXT    NOT NULL,
    commence_time            TEXT    NOT NULL,
    market_key               TEXT    NOT NULL,
    selection_raw            TEXT    NOT NULL,
    selection_canonical_id   TEXT    NOT NULL,
    sharp_book               TEXT    NOT NULL,
    soft_book                TEXT    NOT NULL,
    soft_decimal             REAL    NOT NULL,                 -- d_taken; fixed per key
    threshold_used           REAL    NOT NULL,
    -- first-detection snapshot (stable; full series lives in candidate_observations)
    first_seen_ts            TEXT    NOT NULL,
    last_seen_ts             TEXT    NOT NULL,
    observation_count        INTEGER NOT NULL DEFAULT 1,
    detect_sharp_decimal     REAL    NOT NULL,
    detect_sharp_opp_decimal REAL    NOT NULL,
    detect_sharp_novig_prob  REAL    NOT NULL,
    detect_edge_pct          REAL    NOT NULL,                 -- edge at first detection
    -- confirm lifecycle
    status                   TEXT    NOT NULL DEFAULT 'DETECTED'
        CHECK (status IN ('DETECTED','PENDING_CONFIRM','CONFIRMED',
                          'TRANSIENT','PENDING_GRADE','GRADED','UNGRADED')),
    confirm_due_ts           TEXT,
    confirms_required        INTEGER NOT NULL DEFAULT 1,
    confirms_attempted       INTEGER NOT NULL DEFAULT 0,
    confirms_passed          INTEGER NOT NULL DEFAULT 0,
    confirm_first_ts         TEXT,
    confirm_last_ts          TEXT,
    confirm_post_edge_pct    REAL,                             -- edge at last confirm
    -- bookkeeping
    notional_stake           REAL    NOT NULL DEFAULT 1.0,     -- flat; never sizing
    claude_note              TEXT                              -- commentary only
);
