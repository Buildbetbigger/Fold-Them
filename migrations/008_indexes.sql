-- 008_indexes.sql — all non-unique indexes (build-plan §3).
-- Unique indexes already exist via inline UNIQUE/PRIMARY KEY in 001-007
-- (candidates.opportunity_key, candidate_observations(pull_id,opportunity_key),
--  clv_results.candidate_id, closing_lines(event_id,sharp_book), ...), so they are
-- not recreated here. Index set = base spec §3 + P1 §2/§4.

-- 001 core
CREATE INDEX ix_audit_start  ON audit_runs(run_start_ts);
CREATE INDEX ix_err_ts       ON system_errors(error_ts, component);
CREATE INDEX ix_raw_sport_ts ON raw_api_pulls(sport_key, pull_timestamp);

-- 002 events / entities
CREATE INDEX ix_norm_canon           ON normalized_entities(sport_key, canonical_id);
CREATE INDEX ix_events_sport_commence ON events(sport_key, commence_time);

-- 003 snapshots / outcomes / closing
CREATE INDEX ix_snap_lookup ON bookmaker_snapshots(event_id, bookmaker_key, market_key, pull_timestamp);
CREATE INDEX ix_out_snap    ON market_outcomes(snapshot_id);
CREATE INDEX ix_out_event   ON market_outcomes(event_id, bookmaker_key);
CREATE INDEX ix_close_event ON closing_lines(event_id);

-- 004 candidates (P1)
CREATE INDEX ix_cand_event   ON candidates(event_id);
CREATE INDEX ix_cand_status  ON candidates(status, sport_key, first_seen_ts);
CREATE INDEX ix_cand_confirm ON candidates(status, confirm_due_ts);   -- confirm-worker queue

-- 005 candidate_observations (P1)
CREATE INDEX ix_obs_candidate ON candidate_observations(candidate_id);
CREATE INDEX ix_obs_oppkey    ON candidate_observations(opportunity_key);
CREATE INDEX ix_obs_pull      ON candidate_observations(pull_id);
CREATE INDEX ix_obs_ts        ON candidate_observations(observed_ts);

-- 007 rejections (base + P1 opportunity_key)
CREATE INDEX ix_rej_code   ON rejections(rejection_code, rejected_ts);
CREATE INDEX ix_rej_event  ON rejections(event_id);
CREATE INDEX ix_rej_oppkey ON rejections(opportunity_key);
