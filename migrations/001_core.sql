-- 001_core.sql — audit_runs, system_errors, raw_api_pulls.
-- Ticket T3 (build-plan §3). Schema: base spec §3 (#10, #11, #1).
-- FK parents first: audit_runs has no FK; system_errors + raw_api_pulls -> audit_runs.
-- Non-unique indexes live in 008_indexes.sql (build-plan §3).

CREATE TABLE audit_runs (
    audit_run_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    run_label       TEXT,
    run_start_ts    TEXT NOT NULL,
    run_end_ts      TEXT,
    config_hash     TEXT NOT NULL,
    config_snapshot TEXT NOT NULL,          -- resolved config JSON (secret-free)
    code_version    TEXT,                   -- git sha
    status          TEXT NOT NULL           -- RUNNING|COMPLETED|ABORTED
);

CREATE TABLE system_errors (
    error_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    error_ts     TEXT NOT NULL,
    audit_run_id INTEGER REFERENCES audit_runs(audit_run_id),
    component    TEXT NOT NULL,
    severity     TEXT NOT NULL,             -- WARN|ERROR|FATAL
    context      TEXT,                      -- JSON (pull_id/event_id/etc)
    message      TEXT NOT NULL,
    stack_trace  TEXT
);

CREATE TABLE raw_api_pulls (
    pull_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    audit_run_id   INTEGER NOT NULL REFERENCES audit_runs(audit_run_id),
    pull_timestamp TEXT    NOT NULL,        -- UTC, your clock
    endpoint       TEXT    NOT NULL,
    sport_key      TEXT    NOT NULL,
    region         TEXT,
    market_key     TEXT    NOT NULL,
    http_status    INTEGER NOT NULL,
    payload_hash   TEXT    NOT NULL,        -- sha256 of raw_payload
    raw_payload    TEXT    NOT NULL         -- full JSON, never mutated
);
