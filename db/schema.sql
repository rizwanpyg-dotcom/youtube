-- =====================================================================
-- youtube_automation.db: schema v1
-- SQLite 3.35+ (STRICT tables need 3.37+; GitHub's ubuntu-24.04 ships 3.45)
-- Conventions:
--   * Timestamps are ISO-8601 UTC text ('2026-11-09T14:00:00Z').
--   * JSON columns are TEXT checked with json_valid().
--   * No column may ever hold a credential. Secrets live only in GitHub Secrets.
-- =====================================================================

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    description TEXT NOT NULL
) STRICT;

-- Runtime switches editable from the control workflow (pause, mode, caps).
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_by TEXT NOT NULL DEFAULT 'system'
) STRICT;

-- ---------------------------------------------------------------------
-- Agent 1: Market research
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS niches (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    demand        REAL,      -- 0..1
    competition   REAL,      -- 0..1 (1 = low competition)
    rpm_potential REAL,      -- 0..1
    automation    REAL,      -- 0..1
    risk          REAL,      -- 0..1 (1 = low risk)
    total_score   REAL,
    evidence_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(evidence_json)),
    status        TEXT NOT NULL DEFAULT 'candidate'
                  CHECK (status IN ('candidate','active','rejected','retired')),
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
) STRICT;

-- ---------------------------------------------------------------------
-- Agent 2: Topic research (dedupe via slug + fingerprint)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS topics (
    id                INTEGER PRIMARY KEY,
    niche_id          INTEGER REFERENCES niches(id),
    slug              TEXT NOT NULL UNIQUE,         -- normalized subject, e.g. 'tulip-mania-1637'
    fingerprint       TEXT NOT NULL,                -- sorted keyword set, for near-duplicate checks
    title             TEXT NOT NULL,
    category          TEXT NOT NULL,                -- e.g. 'bank-panic','tech-failure','currency-crisis'
    era               TEXT,                         -- e.g. '1900-1950'
    region            TEXT,
    angle             TEXT,                         -- the unique take
    demand_score      REAL,
    competition_score REAL,
    gap_score         REAL,
    final_score       REAL,
    evidence_json     TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(evidence_json)),
    status            TEXT NOT NULL DEFAULT 'candidate'
                      CHECK (status IN ('candidate','selected','in_production','used','rejected')),
    rejection_reason  TEXT,
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
) STRICT;
CREATE INDEX IF NOT EXISTS ix_topics_status_score ON topics(status, final_score DESC);

-- ---------------------------------------------------------------------
-- Videos: one row per long-form or Short; the pipeline state lives here
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS videos (
    id               INTEGER PRIMARY KEY,
    topic_id         INTEGER NOT NULL REFERENCES topics(id),
    format           TEXT NOT NULL CHECK (format IN ('long','short')),
    parent_video_id  INTEGER REFERENCES videos(id),  -- Short cut from a long-form video
    stage            TEXT NOT NULL DEFAULT 'planned' CHECK (stage IN (
                        'planned','researched','fact_checked','scripted',
                        'script_approval','voiced','visuals_ready','rendered',
                        'thumbnailed','seo_ready','qc_passed','final_approval',
                        'package_ready','scheduled','published')),
    status           TEXT NOT NULL DEFAULT 'active' CHECK (status IN (
                        'active','waiting','paused','failed','rejected','done')),
    working_title    TEXT,
    duration_s       REAL,
    publish_slot     TEXT,                  -- planned slot from calendar (UTC)
    youtube_video_id TEXT UNIQUE,
    privacy          TEXT CHECK (privacy IN ('private','unlisted','public')),
    publish_at       TEXT,                  -- YouTube scheduled publish time
    published_at     TEXT,
    package_url      TEXT,                  -- GitHub Actions artifact link (package mode)
    render_seconds   REAL,                  -- measured resource use
    last_error       TEXT,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    CHECK (format = 'short' OR parent_video_id IS NULL)
) STRICT;
CREATE INDEX IF NOT EXISTS ix_videos_stage ON videos(status, stage);
-- One long-form video per topic.
CREATE UNIQUE INDEX IF NOT EXISTS ux_videos_long_topic ON videos(topic_id) WHERE format = 'long';

CREATE TRIGGER IF NOT EXISTS trg_videos_touch AFTER UPDATE ON videos
FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE videos SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id = NEW.id;
END;

CREATE TABLE IF NOT EXISTS calendar (
    id        INTEGER PRIMARY KEY,
    slot_at   TEXT NOT NULL,                          -- UTC publish slot
    format    TEXT NOT NULL CHECK (format IN ('long','short')),
    video_id  INTEGER UNIQUE REFERENCES videos(id),
    status    TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','assigned','filled','skipped')),
    UNIQUE (slot_at, format)
) STRICT;

-- ---------------------------------------------------------------------
-- Agent 3: Research and fact-checking
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sources (
    id              INTEGER PRIMARY KEY,
    url             TEXT NOT NULL UNIQUE,
    title           TEXT,
    publisher       TEXT,
    author          TEXT,
    published_date  TEXT,
    accessed_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    tier            INTEGER NOT NULL CHECK (tier BETWEEN 1 AND 4),
                    -- 1 primary/official data, 2 academic/major press, 3 encyclopedic, 4 other
    content_sha256  TEXT,
    excerpt         TEXT               -- small cached excerpt for evidence checks
) STRICT;

CREATE TABLE IF NOT EXISTS claims (
    id          INTEGER PRIMARY KEY,
    video_id    INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    ref         TEXT NOT NULL,                        -- 'C12', cited in the script
    text        TEXT NOT NULL,
    claim_type  TEXT NOT NULL CHECK (claim_type IN ('number','date','quote','event','causal','other')),
    status      TEXT NOT NULL DEFAULT 'unverified'
                CHECK (status IN ('unverified','verified','conflicting','rejected')),
    confidence  REAL,
    notes       TEXT,
    UNIQUE (video_id, ref)
) STRICT;

CREATE TABLE IF NOT EXISTS claim_sources (
    claim_id       INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    source_id      INTEGER NOT NULL REFERENCES sources(id),
    stance         TEXT NOT NULL CHECK (stance IN ('supports','contradicts','context')),
    evidence_quote TEXT NOT NULL,       -- must appear verbatim in the fetched source text
    quote_verified INTEGER NOT NULL DEFAULT 0 CHECK (quote_verified IN (0,1)),
    PRIMARY KEY (claim_id, source_id)
) STRICT;

-- ---------------------------------------------------------------------
-- Agent 4: Scripts (versioned)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS scripts (
    id             INTEGER PRIMARY KEY,
    video_id       INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    version        INTEGER NOT NULL,
    outline_json   TEXT NOT NULL CHECK (json_valid(outline_json)),
    body           TEXT NOT NULL,          -- narration with [C#] claim markers and [SCENE] cues
    word_count     INTEGER NOT NULL,
    est_duration_s REAL NOT NULL,
    llm_provider   TEXT,
    status         TEXT NOT NULL DEFAULT 'draft'
                   CHECK (status IN ('draft','qc_failed','qc_passed','approved','rejected','superseded')),
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    UNIQUE (video_id, version)
) STRICT;

-- ---------------------------------------------------------------------
-- Agents 5-7: Assets and licence register (voice, images, music, charts, fonts)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS assets (
    id           INTEGER PRIMARY KEY,
    video_id     INTEGER REFERENCES videos(id) ON DELETE SET NULL,  -- NULL = shared brand asset
    kind         TEXT NOT NULL CHECK (kind IN ('image','map','chart','music','sfx','font','voice','footage','thumbnail')),
    origin       TEXT NOT NULL CHECK (origin IN ('generated','downloaded','brand')),
    origin_url   TEXT,
    file_name    TEXT NOT NULL,
    sha256       TEXT,
    license      TEXT NOT NULL,          -- 'PD','CC0','CC-BY-4.0','CC-BY-SA-4.0','Apache-2.0','YT-Audio-Library','Original'
    license_url  TEXT,
    author       TEXT,
    attribution  TEXT,                   -- exact credit text for the description
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    -- Non-commercial and no-derivatives licences are never allowed.
    -- GLOB is case-sensitive, so values like 'Brand' don't match by accident.
    CHECK (license NOT GLOB '*-NC*' AND license NOT GLOB '*-ND*')
) STRICT;
CREATE INDEX IF NOT EXISTS ix_assets_video ON assets(video_id);

CREATE TABLE IF NOT EXISTS thumbnails (
    id        INTEGER PRIMARY KEY,
    video_id  INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    variant   TEXT NOT NULL,              -- 'A','B','C'
    style     TEXT NOT NULL,              -- e.g. 'chart-crash','headline-dark','map-focus'
    text      TEXT,
    sha256    TEXT,
    selected  INTEGER NOT NULL DEFAULT 0 CHECK (selected IN (0,1)),
    UNIQUE (video_id, variant)
) STRICT;

-- ---------------------------------------------------------------------
-- Agent 8: SEO and publishing
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS seo_metadata (
    video_id        INTEGER PRIMARY KEY REFERENCES videos(id) ON DELETE CASCADE,
    title           TEXT NOT NULL CHECK (length(title) <= 100),
    description     TEXT NOT NULL CHECK (length(description) <= 5000),
    tags_json       TEXT NOT NULL CHECK (json_valid(tags_json)),
    chapters_json   TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(chapters_json)),
    category_id     TEXT NOT NULL DEFAULT '27',   -- Education
    synthetic_media INTEGER NOT NULL DEFAULT 0 CHECK (synthetic_media IN (0,1)),
    made_for_kids   INTEGER NOT NULL DEFAULT 0 CHECK (made_for_kids IN (0,1)),
    keywords_json   TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(keywords_json))
) STRICT;

-- ---------------------------------------------------------------------
-- Agent 10: Quality control and human approvals
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS qc_reports (
    id          INTEGER PRIMARY KEY,
    video_id    INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    gate        TEXT NOT NULL CHECK (gate IN ('research','script','audio','video','thumbnail','metadata','final')),
    passed      INTEGER NOT NULL CHECK (passed IN (0,1)),
    score       REAL,
    checks_json TEXT NOT NULL CHECK (json_valid(checks_json)),
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
) STRICT;

CREATE TABLE IF NOT EXISTS approvals (
    id           INTEGER PRIMARY KEY,
    video_id     INTEGER REFERENCES videos(id) ON DELETE CASCADE,  -- NULL for spend/policy/cadence decisions
    gate         TEXT NOT NULL CHECK (gate IN ('script','final','spend','policy','cadence','publish_mode')),
    subject      TEXT,                        -- what is being approved when there is no video
    issue_number INTEGER,                     -- GitHub Issue used as the approval ticket
    decision     TEXT NOT NULL DEFAULT 'pending' CHECK (decision IN ('pending','approved','rejected','changes_requested')),
    decided_by   TEXT,                        -- GitHub login; must be the repo owner
    comment      TEXT,
    requested_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    decided_at   TEXT,
    UNIQUE (video_id, gate),
    CHECK (video_id IS NOT NULL OR subject IS NOT NULL)
) STRICT;

-- ---------------------------------------------------------------------
-- Task queue: idempotent, retryable units of work (prevents duplicates)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tasks (
    id               INTEGER PRIMARY KEY,
    idempotency_key  TEXT NOT NULL UNIQUE,     -- e.g. 'video:12:render:v1'
    kind             TEXT NOT NULL,            -- agent/stage name
    video_id         INTEGER REFERENCES videos(id) ON DELETE CASCADE,
    status           TEXT NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending','running','done','failed','dead','paused','skipped')),
    attempts         INTEGER NOT NULL DEFAULT 0,
    max_attempts     INTEGER NOT NULL DEFAULT 3,
    next_run_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    lease_until      TEXT,                     -- stale 'running' tasks are reclaimed after this
    run_id           TEXT,                     -- GitHub Actions run id
    last_error       TEXT,                     -- secrets are redacted before storage
    result_json      TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    started_at       TEXT,
    finished_at      TEXT
) STRICT;
CREATE INDEX IF NOT EXISTS ix_tasks_due ON tasks(status, next_run_at);

CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY,
    workflow     TEXT NOT NULL,
    run_id       TEXT NOT NULL,
    attempt      INTEGER NOT NULL DEFAULT 1,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL CHECK (status IN ('running','success','failure','cancelled')),
    cpu_seconds  REAL,
    peak_rss_mb  REAL,
    disk_used_mb REAL,
    summary      TEXT,
    UNIQUE (run_id, attempt)
) STRICT;

CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY,
    ts        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    level     TEXT NOT NULL CHECK (level IN ('DEBUG','INFO','WARNING','ERROR','CRITICAL')),
    component TEXT NOT NULL,
    video_id  INTEGER,
    run_id    TEXT,
    message   TEXT NOT NULL
) STRICT;
CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts);

-- ---------------------------------------------------------------------
-- Quotas, costs, health
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS api_usage (
    provider   TEXT NOT NULL,        -- 'youtube_data','youtube_analytics','gemini','github_models'
    day        TEXT NOT NULL,        -- quota day in the provider's reset timezone
    units      INTEGER NOT NULL DEFAULT 0,
    requests   INTEGER NOT NULL DEFAULT 0,
    errors     INTEGER NOT NULL DEFAULT 0,
    daily_cap  INTEGER NOT NULL,     -- our self-imposed hard stop (below the provider limit)
    PRIMARY KEY (provider, day)
) STRICT;

CREATE TABLE IF NOT EXISTS expenses (
    id          INTEGER PRIMARY KEY,
    month       TEXT NOT NULL,            -- 'YYYY-MM'
    item        TEXT NOT NULL,
    amount_usd  REAL NOT NULL DEFAULT 0,
    amount_pkr  REAL,
    approved_by TEXT,                     -- must be set for any amount > 0
    note        TEXT,
    UNIQUE (month, item),
    CHECK (amount_usd = 0 OR approved_by IS NOT NULL)
) STRICT;

CREATE TABLE IF NOT EXISTS health_checks (
    id        INTEGER PRIMARY KEY,
    ts        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    check_name TEXT NOT NULL,
    ok        INTEGER NOT NULL CHECK (ok IN (0,1)),
    detail    TEXT
) STRICT;

-- ---------------------------------------------------------------------
-- Agent 9: Analytics, and the learning loop
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS analytics_daily (
    video_id          INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    day               TEXT NOT NULL,
    views             INTEGER,
    watch_minutes     REAL,
    avg_view_duration REAL,
    avg_view_pct      REAL,
    likes             INTEGER,
    comments          INTEGER,
    subs_gained       INTEGER,
    subs_lost         INTEGER,
    impressions       INTEGER,         -- NULL when the API doesn't provide it
    ctr               REAL,            -- NULL when the API doesn't provide it
    PRIMARY KEY (video_id, day)
) STRICT;

CREATE TABLE IF NOT EXISTS channel_daily (
    day                  TEXT PRIMARY KEY,
    subscribers          INTEGER,
    total_views          INTEGER,
    video_count          INTEGER,
    watch_hours_365d     REAL,         -- public long-form watch hours, for YPP tracking
    shorts_views_90d     INTEGER
) STRICT;

-- Thompson-sampling arms for what to make next (topic category, length, thumbnail style, title pattern).
CREATE TABLE IF NOT EXISTS strategy_arms (
    dimension  TEXT NOT NULL,
    value      TEXT NOT NULL,
    alpha      REAL NOT NULL DEFAULT 1.0,
    beta       REAL NOT NULL DEFAULT 1.0,
    n_videos   INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    PRIMARY KEY (dimension, value)
) STRICT;

CREATE TABLE IF NOT EXISTS insights (
    id          INTEGER PRIMARY KEY,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    kind        TEXT NOT NULL CHECK (kind IN ('topic','format','thumbnail','title','retention','schedule')),
    finding     TEXT NOT NULL,
    evidence    TEXT NOT NULL CHECK (json_valid(evidence)),
    applied     INTEGER NOT NULL DEFAULT 0 CHECK (applied IN (0,1))
) STRICT;

-- ---------------------------------------------------------------------
-- Dashboard views
-- ---------------------------------------------------------------------
CREATE VIEW IF NOT EXISTS v_pipeline AS
SELECT v.id, v.format, t.title AS topic, v.stage, v.status, v.publish_slot,
       v.youtube_video_id, v.updated_at, v.last_error
FROM videos v JOIN topics t ON t.id = v.topic_id;

CREATE VIEW IF NOT EXISTS v_failed_tasks AS
SELECT id, kind, video_id, attempts, last_error, finished_at
FROM tasks WHERE status IN ('failed','dead') ORDER BY finished_at DESC;

CREATE VIEW IF NOT EXISTS v_monetization AS
SELECT day, subscribers, watch_hours_365d, shorts_views_90d,
       MIN(1.0, subscribers / 1000.0)        AS subs_progress,
       MIN(1.0, watch_hours_365d / 4000.0)   AS watch_progress,
       MIN(1.0, shorts_views_90d / 10000000.0) AS shorts_progress
FROM channel_daily ORDER BY day DESC LIMIT 1;

CREATE VIEW IF NOT EXISTS v_monthly_cost AS
SELECT month, SUM(amount_usd) AS total_usd FROM expenses GROUP BY month;

-- ---------------------------------------------------------------------
-- Seed data
-- ---------------------------------------------------------------------
INSERT OR IGNORE INTO schema_version(version, description) VALUES (1, 'initial schema');

INSERT OR IGNORE INTO settings(key, value) VALUES
    ('pipeline_paused',          'false'),
    ('publish_mode',             'package'),  -- package | review_window | auto
    ('require_script_approval',  'true'),
    ('require_final_approval',   'true'),
    ('approved_videos_count',    '0'),
    ('long_per_week',            '2'),
    ('shorts_per_week',          '2'),
    ('monthly_budget_usd',       '0');

INSERT OR IGNORE INTO niches(name, status, evidence_json) VALUES
    ('Economic History & Tech Collapses', 'active',
     '{"basis":"Phase 1 qualitative scoring 19-20/25","pending":"Agent 1 data validation"}');
