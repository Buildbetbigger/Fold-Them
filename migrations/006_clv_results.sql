-- 006_clv_results.sql — one CLV row per graded unique candidate.
-- Ticket T3 (build-plan §3). Schema: base spec §3 (#9).
-- UNIQUE(candidate_id): exactly one CLV per candidate (its own index; no ix_clv_cand).
-- grade_status is GRADED|UNGRADED_CLOSE_MISSING (errata E5); free-text by base §3 (no
-- CHECK by design), enum-backed via constants.GradeStatus which lands with grading (T13/T14).
-- FK parents (candidates, closing_lines) exist by 004/003.

CREATE TABLE clv_results (
    clv_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id       INTEGER NOT NULL UNIQUE REFERENCES candidates(candidate_id),
    close_id           INTEGER REFERENCES closing_lines(close_id),
    d_taken            REAL,                 -- soft_decimal at detection
    p_close_novig      REAL,                 -- selection side at close
    fair_close_decimal REAL,
    clv_pct            REAL,                 -- d_taken * p_close_novig - 1, x100
    beat_close         INTEGER,              -- bool: clv_pct > 0
    graded_ts          TEXT,
    grade_status       TEXT NOT NULL         -- GRADED|UNGRADED_CLOSE_MISSING
);
