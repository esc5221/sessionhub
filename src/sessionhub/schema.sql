-- Session Hub schema (versioned; migration runner picks latest)

CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  -- session origin: 'interactive' (human-driven) | 'subagent' (spawned agent,
  -- e.g. codex thread_source=subagent) | 'exec' (one-shot headless, e.g. codex exec).
  -- recent/list hide non-interactive by default; --all surfaces them.
  origin TEXT NOT NULL DEFAULT 'interactive',
  machine TEXT NOT NULL,
  project_path TEXT,
  started_at TEXT,
  ended_at TEXT,
  duration_minutes INTEGER,
  message_count INTEGER,
  token_input INTEGER,
  token_output INTEGER,
  project TEXT,
  subsystem TEXT,
  fallback_title TEXT,
  first_user_message TEXT,
  llm_title TEXT,
  llm_summary TEXT,
  work_type TEXT,
  outcome TEXT,
  why_started TEXT,
  files_changed TEXT,
  related_commits TEXT,
  raw_path TEXT,
  raw_mtime REAL,
  -- ORIGIN: where the untrimmed log lives. origin_host is an ssh alias, or NULL
  -- when the log is on this machine. origin_path is its path there. The hub no
  -- longer mirrors the log; these let `raw --full` find it in place.
  origin_host TEXT,
  origin_path TEXT,
  enrichment_status TEXT DEFAULT 'pending',
  ingested_at TEXT DEFAULT (datetime('now')),
  enriched_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project);
CREATE INDEX IF NOT EXISTS idx_sessions_machine ON sessions(machine);
CREATE INDEX IF NOT EXISTS idx_sessions_raw_path ON sessions(raw_path);

-- DIGEST layer — the trimmed conversation, gzip-compressed, one row per
-- session. Kept in the index DB so the whole archive stays a single portable
-- file. `mode` records how it was rendered (conversation | full).
CREATE TABLE IF NOT EXISTS digests (
  session_id TEXT PRIMARY KEY REFERENCES sessions(id),
  mode TEXT NOT NULL,
  bytes BLOB NOT NULL,
  chars INTEGER,
  built_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS session_tags (
  session_id TEXT REFERENCES sessions(id),
  tag TEXT NOT NULL,
  source TEXT DEFAULT 'manual',
  PRIMARY KEY (session_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_tags_tag ON session_tags(tag);

CREATE TABLE IF NOT EXISTS project_rules (
  id INTEGER PRIMARY KEY,
  path_pattern TEXT NOT NULL,
  project TEXT NOT NULL,
  subsystem TEXT,
  priority INTEGER DEFAULT 0,
  source TEXT DEFAULT 'config'  -- 'config' | 'auto' | 'user'
);

CREATE TABLE IF NOT EXISTS session_links (
  from_id TEXT NOT NULL REFERENCES sessions(id),
  to_id TEXT NOT NULL REFERENCES sessions(id),
  link_type TEXT NOT NULL,
  score REAL DEFAULT 1.0,
  reason TEXT,
  source TEXT NOT NULL,
  PRIMARY KEY (from_id, to_id, link_type)
);

CREATE TABLE IF NOT EXISTS workstreams (
  id TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  project TEXT,
  status TEXT DEFAULT 'active',
  summary TEXT
);

CREATE TABLE IF NOT EXISTS session_workstreams (
  session_id TEXT REFERENCES sessions(id),
  workstream_id TEXT REFERENCES workstreams(id),
  role TEXT,
  PRIMARY KEY (session_id, workstream_id)
);

CREATE TABLE IF NOT EXISTS entities (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  type TEXT NOT NULL,
  UNIQUE(name, type)
);

CREATE TABLE IF NOT EXISTS session_entities (
  session_id TEXT REFERENCES sessions(id),
  entity_id INTEGER REFERENCES entities(id),
  role TEXT,
  source TEXT DEFAULT 'rule',
  PRIMARY KEY (session_id, entity_id)
);

-- Observability
CREATE TABLE IF NOT EXISTS ingest_runs (
  id INTEGER PRIMARY KEY,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT,              -- 'ok' | 'error' | 'partial'
  new_sessions INTEGER DEFAULT 0,
  updated_sessions INTEGER DEFAULT 0,
  errors INTEGER DEFAULT 0,
  notes TEXT
);

CREATE TABLE IF NOT EXISTS sync_runs (
  id INTEGER PRIMARY KEY,
  host TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT,
  bytes_received INTEGER,
  notes TEXT
);

CREATE TABLE IF NOT EXISTS ingest_errors (
  id INTEGER PRIMARY KEY,
  path TEXT NOT NULL,
  error TEXT,
  attempts INTEGER DEFAULT 1,
  first_seen TEXT DEFAULT (datetime('now')),
  last_seen TEXT DEFAULT (datetime('now')),
  UNIQUE(path)
);

-- FTS5
CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts USING fts5(
  id,
  display_title,
  llm_summary,
  first_user_message,
  project,
  subsystem,
  tags_text,
  files_text,
  why_started
);

-- Schema version
CREATE TABLE IF NOT EXISTS schema_version (
  version INTEGER PRIMARY KEY,
  applied_at TEXT DEFAULT (datetime('now'))
);
INSERT OR IGNORE INTO schema_version (version) VALUES (1);
