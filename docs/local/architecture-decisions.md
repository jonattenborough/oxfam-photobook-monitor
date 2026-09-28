# Architecture decisions

## Host and boundaries

The host is Jon's macOS 14.4.1 arm64 Mac Studio. The application data directory is `~/Library/Application Support/Photobook Radar`, outside the repository and outside the normal Documents sync path. The web server binds only to `127.0.0.1:8765`. Three per-user LaunchAgents supervise the web process, local triage worker and hourly backup. The web process does not schedule scans. The local instance remains in **shadow** mode; eBay and Telegram production traffic are disabled by configuration.

## Persistence

SQLite in WAL mode with `foreign_keys=ON`, a 10-second busy timeout and `synchronous=FULL` is used. The standard-library `sqlite3` layer replaced the handover's suggested SQLAlchemy/Alembic stack to keep the local service small and remove an ORM dependency from the always-on path. Versioned SQL files under `photobook_radar/migrations` run in immediate transactions. Page capture and cursor advancement share a transaction; jobs use leases and fencing tokens; alert eligibility and outbox insertion share a transaction. Tests cover stale observation ordering, duplicate identity, page rollback and stale worker rejection. A consistent SQLite backup and isolated restore have been run on the actual host. The smaller persistence layer still needs broader failure injection and load testing before cutover.

## Historical data and current truth

Importers preserve the raw input, origin hash, original timestamp and reconciliation status. GitHub Issue comments become `HISTORICAL_CLAIM` reviews only when tied to a listing; they never set current availability or a current verdict. Search-query context is excluded from recognition so a query for a photographer cannot turn an unrelated book into that photographer's work. Imported stock cannot create a new phone alert. Unverified historical listings have an explicit warning in the UI.

## Provider policy

Telegram is the selected phone channel because Jon chose a free, quick channel. The Pushover adapter remains available but disabled. Credentials live in a mode-0600 private TOML file; neither credentials nor tokens are in the repository. The research provider is `none`; no recurring Codex or paid API activity is enabled. The Oxfam Photography adapter has a production-gated scheduler, with network work isolated from the main triage and outbox loop; it has only offline fixture evidence and remains off in shadow mode. The eBay request boundary and page capture have offline tests but are not attached to live scanner routes. Automatic exact-copy verification and the AI research runner are not commissioned. The existing GitHub workflows and ChatGPT tasks were not changed.
