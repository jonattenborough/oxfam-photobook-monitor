CREATE TABLE research_sweeps (
    id INTEGER PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id),
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL,
    result_json TEXT,
    error TEXT
);
CREATE INDEX research_sweeps_day_idx ON research_sweeps(started_at,status);
