-- 002_events_entities.sql — normalized_entities, events.
-- Ticket T3 (build-plan §3). Schema: base spec §3 (#5, #2).
-- normalized_entities first; events -> normalized_entities (home/away, nullable).

CREATE TABLE normalized_entities (
    map_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    sport_key      TEXT NOT NULL,
    raw_name       TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    canonical_id   TEXT NOT NULL,           -- stable slug
    UNIQUE(sport_key, raw_name)
);

CREATE TABLE events (
    event_id       TEXT PRIMARY KEY,        -- provider event id
    sport_key      TEXT NOT NULL,
    commence_time  TEXT NOT NULL,           -- UTC
    home_team_raw  TEXT NOT NULL,
    away_team_raw  TEXT NOT NULL,
    home_entity_id INTEGER REFERENCES normalized_entities(map_id),
    away_entity_id INTEGER REFERENCES normalized_entities(map_id),
    first_seen_ts  TEXT NOT NULL,
    last_seen_ts   TEXT NOT NULL
);
