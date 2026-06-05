import sqlite3, os

DB_PATH = os.environ.get('DB_PATH', '/var/jizo/jizo.db')

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    return conn

def init_db():
    conn = get_conn()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS projects (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL DEFAULT '',
            created_at  TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'uploaded'
        );

        CREATE TABLE IF NOT EXISTS participants (
            project_id  TEXT NOT NULL REFERENCES projects(id),
            idx         INTEGER NOT NULL,
            name        TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (project_id, idx)
        );

        CREATE TABLE IF NOT EXISTS recordings (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id  TEXT NOT NULL REFERENCES projects(id),
            seq         INTEGER NOT NULL,
            audio_path  TEXT NOT NULL,
            mime        TEXT NOT NULL DEFAULT 'audio/webm',
            duration    TEXT NOT NULL DEFAULT '',
            created_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ref_segments (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id  TEXT NOT NULL REFERENCES projects(id),
            seq         INTEGER NOT NULL,
            text        TEXT NOT NULL DEFAULT '',
            speaker_idx INTEGER,
            ts          TEXT NOT NULL DEFAULT '',
            highlight   INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS ai_transcripts (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id       TEXT NOT NULL REFERENCES projects(id),
            recording_id     INTEGER REFERENCES recordings(id),
            run_number       INTEGER NOT NULL DEFAULT 1,
            aai_id           TEXT,
            status           TEXT NOT NULL DEFAULT 'queued',
            full_text        TEXT,
            utterances_json  TEXT,
            speaker_map_json TEXT,
            error            TEXT
        );

        CREATE TABLE IF NOT EXISTS merged_transcripts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id  TEXT NOT NULL REFERENCES projects(id),
            model       TEXT NOT NULL DEFAULT 'claude-sonnet-4-6',
            result_json TEXT,
            notes_json  TEXT,
            summary     TEXT,
            tasks_json  TEXT,
            created_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS qa_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id  TEXT NOT NULL REFERENCES projects(id),
            question    TEXT NOT NULL,
            answer      TEXT NOT NULL,
            model       TEXT,
            created_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS speaker_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id  TEXT NOT NULL REFERENCES projects(id),
            seq         INTEGER NOT NULL,
            ts          TEXT NOT NULL DEFAULT '',
            ms          INTEGER NOT NULL DEFAULT 0,
            speaker_idx INTEGER,
            speaker_name TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_spkev_project ON speaker_events(project_id);

        CREATE TABLE IF NOT EXISTS api_usage (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            service     TEXT NOT NULL,
            units       REAL NOT NULL,
            unit_type   TEXT NOT NULL,
            project_id  TEXT,
            endpoint    TEXT,
            cost_usd    REAL DEFAULT 0,
            created_at  TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_usage_service_date ON api_usage(service, created_at);

        CREATE INDEX IF NOT EXISTS idx_recordings_project  ON recordings(project_id);
        CREATE INDEX IF NOT EXISTS idx_refseg_project      ON ref_segments(project_id);
        CREATE INDEX IF NOT EXISTS idx_aitx_project        ON ai_transcripts(project_id);
        CREATE INDEX IF NOT EXISTS idx_merged_project      ON merged_transcripts(project_id);
        CREATE INDEX IF NOT EXISTS idx_qa_project          ON qa_history(project_id);

        -- Zoom 連携（v1）
        CREATE TABLE IF NOT EXISTS zoom_pending (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            zoom_meeting_id      TEXT NOT NULL,
            zoom_uuid            TEXT UNIQUE NOT NULL,
            topic                TEXT,
            host_email           TEXT,
            start_time           TEXT,
            duration             INTEGER,
            participants_json    TEXT,
            download_token       TEXT,
            downloaded_at        TEXT,
            status               TEXT NOT NULL,
            error_message        TEXT,
            created_at           TEXT NOT NULL,
            imported_project_id  TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_zoom_pending_status ON zoom_pending(status);

        -- LLM統合（マージ）設定
        CREATE TABLE IF NOT EXISTS merge_settings (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TEXT
        );
    ''')

    # merge_settings の初期値挿入（既存キーは保護）
    from datetime import datetime, timezone
    _now = datetime.now(timezone.utc).isoformat()
    _defaults = {
        'chunk_size':           '25',
        'cluster_window_ms':    '2000',
        'orphan_sim_threshold': '0.4',
        'backbone_algo':        'fixed',
        'backbone_fixed':       'run2',
        'default_model':        'gemini-2.5-flash',
        'parallel':             '3',
        'max_tokens_per_chunk': '8192',
        'retry_per_chunk':      '1',
        'speaker_priority':     'deepgram',
        'default_w_ws':         '0',
        'default_w_run1':       '7',
        'default_w_run2':       '3',
    }
    for k, v in _defaults.items():
        try:
            conn.execute(
                'INSERT OR IGNORE INTO merge_settings(key,value,updated_at) VALUES(?,?,?)',
                (k, v, _now)
            )
        except Exception:
            pass

    # 既存DBへのマイグレーション（カラム追加・エラー無視）
    for sql in [
        'ALTER TABLE ai_transcripts ADD COLUMN run_number INTEGER NOT NULL DEFAULT 1',
        'ALTER TABLE merged_transcripts ADD COLUMN summary TEXT',
        'ALTER TABLE merged_transcripts ADD COLUMN tasks_json TEXT',
        'ALTER TABLE ai_transcripts ADD COLUMN engine TEXT DEFAULT "whisper"',
        'ALTER TABLE ai_transcripts ADD COLUMN settings_json TEXT',
        # Zoom 連携（v1）
        'ALTER TABLE recordings ADD COLUMN zoom_participant_name TEXT',
        "ALTER TABLE projects ADD COLUMN source TEXT NOT NULL DEFAULT 'mobile'",
    ]:
        try:
            conn.execute(sql)
        except Exception:
            pass  # 既にカラムが存在する場合は無視

    conn.commit()
    conn.close()
