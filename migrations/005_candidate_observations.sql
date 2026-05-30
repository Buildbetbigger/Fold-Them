-- 005_candidate_observations.sql — every threshold-crossing sighting, linked to its
-- de-duplicated parent candidate (repeated sightings cannot mint new candidates).
-- Ticket T3 (build-plan §3). Schema: P1 §2.
-- UNIQUE(pull_id, opportunity_key): one sighting per pull per opportunity (idempotent
-- re-processing). phase CHECK MUST stay in lockstep with constants.Phase (T3 test).
-- FK parents (candidates, raw_api_pulls) exist by 004/001.

CREATE TABLE candidate_observations (
    observation_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id       INTEGER NOT NULL REFERENCES candidates(candidate_id),
    opportunity_key    TEXT    NOT NULL,                       -- denormalized link
    pull_id            INTEGER NOT NULL REFERENCES raw_api_pulls(pull_id),
    observed_ts        TEXT    NOT NULL,
    phase              TEXT    NOT NULL                        -- which loop saw it
        CHECK (phase IN ('DETECTION','CONFIRM')),
    sharp_decimal      REAL    NOT NULL,                       -- selection side
    sharp_opp_decimal  REAL    NOT NULL,
    sharp_novig_prob   REAL    NOT NULL,
    sharp_age_s        REAL    NOT NULL,
    soft_decimal       REAL    NOT NULL,                       -- == candidate's (audit)
    soft_implied_prob  REAL    NOT NULL,
    soft_age_s         REAL    NOT NULL,
    edge_pct           REAL    NOT NULL,                       -- at this sighting
    UNIQUE(pull_id, opportunity_key)                           -- one sighting per pull per opp
);
