# Zoom連携 実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Zoom Cloud Recording API 経由でオンラインMTGの音声を取り込み、話者別音声トラックで完璧な話者分離を実現する。

**Architecture:** Server-to-Server OAuth で Zoom Webhook (`recording.completed`) を受け、参加者別 .m4a を VPS に DL → 管理画面で承認 → 既存の Whisper/Deepgram + Gemini 統合フローに合流。

**Tech Stack:** FastAPI / SQLite / Python httpx / Zoom REST API / HMAC-SHA256 / VanillaJS 管理画面。

設計書: `docs/superpowers/specs/2026-06-04-zoom-integration-design.md`

**ファイル構成（編集対象）:**
- Create: `tests/test_zoom_webhook.py`
- Create: `tests/test_zoom_client.py`
- Modify: `_main_remote.py` （Zoom関連エンドポイント追加・Gemini統合に source 分岐）
- Modify: VPSの `db.py` （スキーマ追加）→ ローカルコピーがないので新規作成 or 取得
- Modify: `_admin_remote.html` （取り込み待ちセクション・手動アップロード）
- Modify: `.env.example`（新設、Zoom credentials記載）

**前提:** Pythonローカル実行環境 (`python3 -m pytest` 可) と `httpx` がインストール済み。VPSは `ssh root@162.43.14.31` で到達可。

---

### Task 1: db.py の取得とスキーマ拡張準備

**Files:**
- Create: `_db_remote.py` （VPSからDLしたdb.pyのローカル正典）

- [ ] **Step 1: VPSから現行db.pyを取得**

Run:
```bash
scp root@162.43.14.31:/opt/jizo-api/db.py "c:/Users/taka/Downloads/files (1)/_db_remote.py"
```
Expected: ファイル取得成功

- [ ] **Step 2: 現行スキーマを確認**

`_db_remote.py` を読み、`projects`/`recordings`/`participants`/`ai_transcripts`/`merged_transcripts`/`ref_segments` のCREATE文を把握する。

- [ ] **Step 3: コミット**

```bash
git add _db_remote.py
git commit -m "chore: snapshot db.py from VPS for editing"
```

---

### Task 2: SQLiteスキーマ拡張（zoom_pending テーブル + 既存テーブル ALTER）

**Files:**
- Modify: `_db_remote.py`

- [ ] **Step 1: zoom_pending テーブル追加**

`init_db()` 関数内、既存の `CREATE TABLE` 群の末尾に以下を追加：

```python
c.execute("""
CREATE TABLE IF NOT EXISTS zoom_pending (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  zoom_meeting_id TEXT NOT NULL,
  zoom_uuid TEXT UNIQUE NOT NULL,
  topic TEXT,
  host_email TEXT,
  start_time TEXT,
  duration INTEGER,
  participants_json TEXT,
  download_token TEXT,
  downloaded_at TEXT,
  status TEXT NOT NULL,
  error_message TEXT,
  created_at TEXT NOT NULL,
  imported_project_id TEXT
)
""")
c.execute("CREATE INDEX IF NOT EXISTS idx_zoom_pending_status ON zoom_pending(status)")
```

- [ ] **Step 2: 既存テーブルに ALTER（冪等）**

`init_db()` 内、CREATE 群の後ろに以下を追加：

```python
def _add_col_if_missing(c, table, col, decl):
    cols = [r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()]
    if col not in cols:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

_add_col_if_missing(c, 'recordings', 'zoom_participant_name', 'TEXT')
_add_col_if_missing(c, 'projects',   'source',                "TEXT NOT NULL DEFAULT 'mobile'")
```

注：SQLite `ALTER TABLE ADD COLUMN` で `NOT NULL DEFAULT` を後付けする際、既存行にはDEFAULT値が入る。

- [ ] **Step 3: ローカルでスキーマ初期化テスト**

```bash
cd "c:/Users/taka/Downloads/files (1)"
cp _db_remote.py /tmp/db.py
cd /tmp && python3 -c "import db; db.init_db()" 
sqlite3 /var/jizo/jizo.db ".schema zoom_pending" 2>/dev/null || echo "ローカル検証スキップ可（VPS本番で適用）"
```

Expected: エラーなし

- [ ] **Step 4: コミット**

```bash
git add _db_remote.py
git commit -m "feat(db): add zoom_pending table and source/zoom_participant_name columns"
```

---

### Task 3: Zoom OAuth トークン取得クライアント

**Files:**
- Create: `tests/test_zoom_client.py`
- Modify: `_main_remote.py` （末尾近く・httpxインポートは既存）

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_zoom_client.py`:

```python
import os, pytest
from unittest.mock import AsyncMock, patch

@pytest.mark.asyncio
async def test_get_zoom_access_token_caches_token():
    from zoom_client import ZoomClient
    client = ZoomClient(client_id='cid', client_secret='csec', account_id='aid')

    mock_resp = AsyncMock()
    mock_resp.json = lambda: {'access_token': 'tok123', 'expires_in': 3600}
    mock_resp.raise_for_status = lambda: None

    with patch('httpx.AsyncClient.post', return_value=mock_resp) as mp:
        t1 = await client.get_access_token()
        t2 = await client.get_access_token()
        assert t1 == 'tok123'
        assert t2 == 'tok123'
        assert mp.call_count == 1   # キャッシュされる
```

- [ ] **Step 2: テスト失敗を確認**

```bash
cd "c:/Users/taka/Downloads/files (1)"
python3 -m pytest tests/test_zoom_client.py -v
```
Expected: `ModuleNotFoundError: No module named 'zoom_client'`

- [ ] **Step 3: zoom_client.py 実装**

`_main_remote.py` と同じディレクトリに新規ファイル `_zoom_client_remote.py` を作成（VPS配置先: `/opt/jizo-api/zoom_client.py`）。

```python
import time, httpx, base64

class ZoomClient:
    TOKEN_URL = 'https://zoom.us/oauth/token'
    API_BASE  = 'https://api.zoom.us/v2'

    def __init__(self, client_id: str, client_secret: str, account_id: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.account_id = account_id
        self._token = None
        self._expires_at = 0

    async def get_access_token(self) -> str:
        now = time.time()
        if self._token and now < self._expires_at - 60:
            return self._token
        basic = base64.b64encode(
            f'{self.client_id}:{self.client_secret}'.encode()).decode()
        async with httpx.AsyncClient(timeout=10) as cli:
            r = await cli.post(
                self.TOKEN_URL,
                params={'grant_type': 'account_credentials',
                        'account_id': self.account_id},
                headers={'Authorization': f'Basic {basic}'})
            r.raise_for_status()
            data = r.json()
        self._token = data['access_token']
        self._expires_at = now + int(data.get('expires_in', 3600))
        return self._token

    async def download_file(self, url: str, dest_path: str, token: str | None = None):
        """download_token (recording_files の token) または OAuth token で DL"""
        params = {'access_token': token} if token else None
        headers = None if token else {
            'Authorization': f'Bearer {await self.get_access_token()}'}
        async with httpx.AsyncClient(timeout=300, follow_redirects=True) as cli:
            async with cli.stream('GET', url, params=params, headers=headers) as r:
                r.raise_for_status()
                with open(dest_path, 'wb') as f:
                    async for chunk in r.aiter_bytes(64 * 1024):
                        f.write(chunk)
```

- [ ] **Step 4: テスト通過確認**

`tests/conftest.py` に `sys.path` 設定が必要：

```python
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
# テスト時は _zoom_client_remote.py を zoom_client として import
import importlib.util
spec = importlib.util.spec_from_file_location(
    'zoom_client',
    os.path.join(os.path.dirname(os.path.dirname(__file__)), '_zoom_client_remote.py'))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
sys.modules['zoom_client'] = mod
```

Run:
```bash
python3 -m pytest tests/test_zoom_client.py -v
```
Expected: PASS

- [ ] **Step 5: コミット**

```bash
git add _zoom_client_remote.py tests/test_zoom_client.py tests/conftest.py
git commit -m "feat(zoom): add ZoomClient with OAuth token caching"
```

---

### Task 4: Webhook署名検証ユーティリティ

**Files:**
- Create: `tests/test_zoom_webhook.py`
- Create: `_zoom_webhook_remote.py` （VPS配置先: `/opt/jizo-api/zoom_webhook.py`）

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_zoom_webhook.py`:

```python
import hmac, hashlib, json

def test_verify_signature_valid():
    from zoom_webhook import verify_signature
    secret = 'mysecret'
    body = b'{"event":"recording.completed"}'
    ts = '1700000000'
    msg = f'v0:{ts}:{body.decode()}'
    sig = 'v0=' + hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
    assert verify_signature(body, ts, sig, secret) is True

def test_verify_signature_invalid():
    from zoom_webhook import verify_signature
    assert verify_signature(b'{}', '1700000000', 'v0=bad', 'mysecret') is False

def test_url_validation_response():
    from zoom_webhook import build_url_validation_response
    secret = 'mysecret'
    plain = 'abc123'
    resp = build_url_validation_response(plain, secret)
    assert resp['plainToken'] == plain
    expected = hmac.new(secret.encode(), plain.encode(), hashlib.sha256).hexdigest()
    assert resp['encryptedToken'] == expected
```

- [ ] **Step 2: テスト失敗を確認**

```bash
python3 -m pytest tests/test_zoom_webhook.py -v
```
Expected: ModuleNotFoundError

- [ ] **Step 3: zoom_webhook.py 実装**

`_zoom_webhook_remote.py`:

```python
import hmac, hashlib

def verify_signature(body: bytes, timestamp: str, signature: str, secret: str) -> bool:
    msg = f'v0:{timestamp}:{body.decode("utf-8")}'
    expected = 'v0=' + hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or '')

def build_url_validation_response(plain_token: str, secret: str) -> dict:
    encrypted = hmac.new(secret.encode(), plain_token.encode(), hashlib.sha256).hexdigest()
    return {'plainToken': plain_token, 'encryptedToken': encrypted}
```

`tests/conftest.py` に同様の動的import追加（`_zoom_webhook_remote.py` → `zoom_webhook`）。

- [ ] **Step 4: テスト通過確認**

```bash
python3 -m pytest tests/test_zoom_webhook.py -v
```
Expected: 3 passed

- [ ] **Step 5: コミット**

```bash
git add _zoom_webhook_remote.py tests/test_zoom_webhook.py tests/conftest.py
git commit -m "feat(zoom): add webhook signature verification + URL validation"
```

---

### Task 5: Webhook受信エンドポイント

**Files:**
- Modify: `_main_remote.py`

- [ ] **Step 1: 環境変数読み込み追加**

`_main_remote.py` の API_KEY 周辺（行34付近）に追記：

```python
ZOOM_CLIENT_ID      = os.environ.get('ZOOM_CLIENT_ID', '')
ZOOM_CLIENT_SECRET  = os.environ.get('ZOOM_CLIENT_SECRET', '')
ZOOM_ACCOUNT_ID     = os.environ.get('ZOOM_ACCOUNT_ID', '')
ZOOM_WEBHOOK_SECRET = os.environ.get('ZOOM_WEBHOOK_SECRET', '')
ZOOM_AUDIO_DIR      = '/var/jizo/audio/zoom'
os.makedirs(ZOOM_AUDIO_DIR, exist_ok=True)

from zoom_client import ZoomClient
from zoom_webhook import verify_signature, build_url_validation_response
import asyncio

zoom_client = ZoomClient(ZOOM_CLIENT_ID, ZOOM_CLIENT_SECRET, ZOOM_ACCOUNT_ID) \
              if ZOOM_CLIENT_ID else None
```

- [ ] **Step 2: Webhookエンドポイント実装**

`_main_remote.py` の末尾（他のエンドポイント群の最後）に追加：

```python
@app.post('/api/zoom/webhook')
async def zoom_webhook(request: Request):
    body = await request.body()
    ts   = request.headers.get('x-zm-request-timestamp', '')
    sig  = request.headers.get('x-zm-signature', '')

    try:
        data = json.loads(body)
    except Exception:
        raise HTTPException(400, 'invalid json')

    # URL Validation Challenge
    if data.get('event') == 'endpoint.url_validation':
        plain = data.get('payload', {}).get('plainToken', '')
        return build_url_validation_response(plain, ZOOM_WEBHOOK_SECRET)

    # 通常イベントの署名検証
    if not verify_signature(body, ts, sig, ZOOM_WEBHOOK_SECRET):
        logger.warning(f'zoom webhook: invalid signature ts={ts}')
        raise HTTPException(401, 'invalid signature')

    if data.get('event') != 'recording.completed':
        logger.info(f"zoom webhook: ignored event={data.get('event')}")
        return {'status': 'ignored'}

    payload = data.get('payload', {}).get('object', {})
    dl_token = data.get('download_token') or data.get('payload', {}).get('download_token', '')

    # DBへpending登録（重複はUNIQUE違反で握りつぶす）
    uuid_ = payload.get('uuid', '')
    with get_conn() as c:
        try:
            c.execute("""
                INSERT INTO zoom_pending
                (zoom_meeting_id, zoom_uuid, topic, host_email, start_time,
                 duration, participants_json, download_token, status, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (
                str(payload.get('id', '')), uuid_,
                payload.get('topic', ''), payload.get('host_email', ''),
                payload.get('start_time', ''), int(payload.get('duration', 0)),
                json.dumps(payload.get('recording_files', []), ensure_ascii=False),
                dl_token, 'pending_download',
                datetime.now(timezone.utc).isoformat()))
            pending_id = c.lastrowid
        except Exception as e:
            logger.info(f'zoom webhook: duplicate uuid {uuid_} ({e})')
            return {'status': 'duplicate'}

    # バックグラウンドでDL開始
    asyncio.create_task(_zoom_download_worker(pending_id, payload, dl_token))
    return {'status': 'accepted', 'pending_id': pending_id}
```

- [ ] **Step 3: コミット**

```bash
git add _main_remote.py
git commit -m "feat(zoom): add /api/zoom/webhook endpoint with signature verification"
```

---

### Task 6: 録画ファイルDLワーカー

**Files:**
- Modify: `_main_remote.py`

- [ ] **Step 1: ワーカー関数追加**

Task 5 のエンドポイント直前に追加：

```python
async def _zoom_download_worker(pending_id: int, payload: dict, dl_token: str):
    """recording_files から participant_audio_files を抽出して個別DL。
    無い場合は audio_only にフォールバック（話者分離不可・警告ログ）。"""
    uuid_ = payload.get('uuid', '')
    safe_uuid = re.sub(r'[^A-Za-z0-9_-]', '_', uuid_)
    out_dir = os.path.join(ZOOM_AUDIO_DIR, safe_uuid)
    os.makedirs(out_dir, exist_ok=True)

    files = payload.get('recording_files', [])
    participants = []  # [{name, audio_path, duration}]

    # 1) 話者別音声を優先
    p_audio = [f for f in files if f.get('recording_type') == 'participant_audio'
               or f.get('participant_email')]
    targets = p_audio if p_audio else [f for f in files
                                       if f.get('recording_type') == 'audio_only']

    if not p_audio:
        logger.warning(f'zoom dl {uuid_}: no participant_audio (falling back to mixed)')

    try:
        for f in targets:
            url = f.get('download_url')
            if not url:
                continue
            name = (f.get('participant_email') or f.get('file_name')
                    or f.get('id', 'unknown'))
            safe_name = re.sub(r'[^\w\-.@ぁ-んァ-ヴー一-龥]', '_', name)
            dest = os.path.join(out_dir, f'{safe_name}.m4a')
            await zoom_client.download_file(url, dest, token=dl_token)
            participants.append({
                'name': safe_name,
                'audio_path': dest,
                'duration': int(f.get('file_size', 0))  # サイズ仮置き
            })

        with get_conn() as c:
            c.execute("""UPDATE zoom_pending
                         SET participants_json=?, downloaded_at=?, status='ready'
                         WHERE id=?""",
                      (json.dumps(participants, ensure_ascii=False),
                       datetime.now(timezone.utc).isoformat(), pending_id))
        logger.info(f'zoom dl {uuid_}: {len(participants)} files done')

    except Exception as e:
        logger.exception(f'zoom dl {uuid_} failed: {e}')
        with get_conn() as c:
            c.execute("UPDATE zoom_pending SET status='error', error_message=? WHERE id=?",
                      (str(e), pending_id))
```

- [ ] **Step 2: コミット**

```bash
git add _main_remote.py
git commit -m "feat(zoom): add download worker for participant audio files"
```

---

### Task 7: 取り込み待ち一覧API

**Files:**
- Modify: `_main_remote.py`

- [ ] **Step 1: GETエンドポイント追加**

Webhookエンドポイントの後に追加：

```python
@app.get('/api/zoom/pending')
async def zoom_pending_list(_: HTTPBasicCredentials = Depends(verify_basic)):
    with get_conn() as c:
        rows = c.execute("""
            SELECT id, zoom_meeting_id, zoom_uuid, topic, host_email,
                   start_time, duration, participants_json, status,
                   error_message, created_at, imported_project_id
            FROM zoom_pending
            WHERE status IN ('pending_download','ready','error')
            ORDER BY created_at DESC
        """).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d['participants'] = json.loads(d.pop('participants_json') or '[]')
        except Exception:
            d['participants'] = []
        out.append(d)
    return {'pending': out}
```

注: `verify_basic` は既存の Basic認証 Depends 関数名に合わせること（main.py内を grep で確認）。

- [ ] **Step 2: 動作確認用コマンド記載**

```bash
# VPSデプロイ後:
curl -s -u test:test https://jizo-dev.com/api/zoom/pending | python3 -m json.tool
```
Expected: `{"pending": []}`

- [ ] **Step 3: コミット**

```bash
git add _main_remote.py
git commit -m "feat(zoom): add /api/zoom/pending list endpoint"
```

---

### Task 8: 取り込み実行API

**Files:**
- Modify: `_main_remote.py`

- [ ] **Step 1: POST /api/zoom/pending/{id}/import 実装**

```python
@app.post('/api/zoom/pending/{pending_id}/import')
async def zoom_import(pending_id: int,
                       request: Request,
                       _: HTTPBasicCredentials = Depends(verify_basic)):
    """body: {run: 1|2, model: 'whisper-1'|'nova-3'|...}"""
    body = await request.json()
    run = int(body.get('run', 1))
    model = body.get('model', 'whisper-1')

    with get_conn() as c:
        row = c.execute(
            "SELECT * FROM zoom_pending WHERE id=? AND status='ready'",
            (pending_id,)).fetchone()
        if not row:
            raise HTTPException(404, 'pending not found or not ready')

        topic = row['topic'] or 'Zoom MTG'
        start = row['start_time'] or datetime.now(timezone.utc).isoformat()
        project_id = uuid.uuid4().hex
        project_name = f'{topic} ({start[:16]})'

        c.execute("""INSERT INTO projects(id,name,created_at,status,source)
                     VALUES(?,?,?,?, 'zoom')""",
                  (project_id, project_name, datetime.now(timezone.utc).isoformat(),
                   'analyzing_run1' if run == 1 else 'analyzing_run2'))

        participants = json.loads(row['participants_json'] or '[]')
        proj_dir = f'/var/jizo/audio/{project_id}'
        os.makedirs(proj_dir, exist_ok=True)

        rec_ids = []
        for idx, p in enumerate(participants, start=1):
            src = p['audio_path']
            dst = os.path.join(proj_dir, f'{idx:02d}_{os.path.basename(src)}')
            try:
                os.rename(src, dst)
            except OSError:
                import shutil; shutil.copy2(src, dst)
            cur = c.execute("""INSERT INTO recordings
                (project_id, seq, audio_path, mime, duration, created_at,
                 zoom_participant_name)
                VALUES(?,?,?,?,?,?,?)""",
                (project_id, idx, dst, 'audio/m4a',
                 int(p.get('duration', 0)),
                 datetime.now(timezone.utc).isoformat(),
                 p.get('name', '')))
            rec_ids.append(cur.lastrowid)
            c.execute("INSERT INTO participants(project_id, idx, name) VALUES(?,?,?)",
                      (project_id, idx, p.get('name', '')))

        c.execute("""UPDATE zoom_pending
                     SET status='imported', imported_project_id=? WHERE id=?""",
                  (project_id, pending_id))

    # 各 recording を個別に STT に投げる（既存 analyze ロジック流用）
    for rid in rec_ids:
        asyncio.create_task(_run_stt_for_recording(project_id, rid, run, model))

    return {'project_id': project_id, 'recordings': len(rec_ids)}
```

- [ ] **Step 2: `_run_stt_for_recording` ヘルパー追加**

既存の `/api/projects/{id}/analyze` 内のSTT呼び出しロジックを切り出して関数化。実装は既存 `analyze_project` 内のWhisper/Deepgram分岐をコピー＆`recording_id` 引数を取るよう変更：

```python
async def _run_stt_for_recording(project_id: str, recording_id: int,
                                  run: int, model: str):
    """既存 analyze_project のSTT処理を1 recordingに限定して実行。
    engine は model から派生（whisper-1→whisper / nova-*→deepgram）。"""
    engine = 'deepgram' if model.startswith('nova') else 'whisper'
    # ↓ ここに既存 analyze_project の該当recording処理ブロックをそのまま
    # （Whisperなら同期、Deepgramなら同期、結果を ai_transcripts に INSERT）
    # ※ 既存コードからのコピペになるため、main.py を読んで該当部分を流用すること
    pass  # 実装時に既存ロジックをコピー
```

注: 既存 `analyze_project` 関数を読み、recording単位の処理を抽出する。新規ロジックは書かない（DRY）。

- [ ] **Step 3: コミット**

```bash
git add _main_remote.py
git commit -m "feat(zoom): add import endpoint creating project + per-speaker recordings"
```

---

### Task 9: 取り込み待ち破棄API

**Files:**
- Modify: `_main_remote.py`

- [ ] **Step 1: DELETE実装**

```python
@app.delete('/api/zoom/pending/{pending_id}')
async def zoom_pending_delete(pending_id: int,
                               _: HTTPBasicCredentials = Depends(verify_basic)):
    with get_conn() as c:
        row = c.execute("SELECT zoom_uuid, participants_json FROM zoom_pending WHERE id=?",
                        (pending_id,)).fetchone()
        if not row:
            raise HTTPException(404, 'not found')
        try:
            for p in json.loads(row['participants_json'] or '[]'):
                try: os.remove(p['audio_path'])
                except OSError: pass
            safe_uuid = re.sub(r'[^A-Za-z0-9_-]', '_', row['zoom_uuid'])
            d = os.path.join(ZOOM_AUDIO_DIR, safe_uuid)
            if os.path.isdir(d) and not os.listdir(d):
                os.rmdir(d)
        finally:
            c.execute("DELETE FROM zoom_pending WHERE id=?", (pending_id,))
    return {'deleted': pending_id}
```

- [ ] **Step 2: コミット**

```bash
git add _main_remote.py
git commit -m "feat(zoom): add pending delete endpoint"
```

---

### Task 10: Gemini統合の source='zoom' 分岐

**Files:**
- Modify: `_main_remote.py`（`/api/projects/{id}/merge` ハンドラ内）

- [ ] **Step 1: 既存merge処理にsource分岐追加**

`merge_transcripts` 関数（あるいは `merge` エンドポイント内ロジック）冒頭で `projects.source` を取得し、Zoomの場合は話者決定をスキップ：

```python
with get_conn() as c:
    proj = c.execute("SELECT source FROM projects WHERE id=?",
                     (project_id,)).fetchone()
source = proj['source'] if proj else 'mobile'

if source == 'zoom':
    # Zoomは recordings.zoom_participant_name が話者ラベル（確定）
    # ref_segments（Web Speech）は無視、Run1/Run2のutterancesを
    # recording_idごとに「話者=zoom_participant_name」で束ねてマージ
    speaker_by_rec = dict(
        c.execute("SELECT id, zoom_participant_name FROM recordings WHERE project_id=?",
                  (project_id,)).fetchall())
    # タイムスタンプ順に全recordingのutterancesを並べ、speakerを上書き
    # （既存マージロジックの話者決定チェーンをスキップしてここを使う）
    ...
else:
    # 既存ロジック（WS＋Run1＋Run2の重み付け話者決定）
    ...
```

詳細実装は既存 merge ロジックを読んでから差し替える（既存実装のフォーマットに合わせる）。

- [ ] **Step 2: コミット**

```bash
git add _main_remote.py
git commit -m "feat(zoom): bypass speaker resolution in Gemini merge for zoom source"
```

---

### Task 11: 手動アップロードAPI（他組織MTG用）

**Files:**
- Modify: `_main_remote.py`

- [ ] **Step 1: POST /api/zoom/upload 実装**

```python
@app.post('/api/zoom/upload')
async def zoom_upload(request: Request,
                       _: HTTPBasicCredentials = Depends(verify_basic)):
    """multipart: files[] (.m4a/.mp4) + meta JSON (topic, participants[{filename,name}])"""
    form = await request.form()
    meta = json.loads(form.get('meta', '{}'))
    files = form.getlist('files')

    project_id = uuid.uuid4().hex
    proj_dir = f'/var/jizo/audio/{project_id}'
    os.makedirs(proj_dir, exist_ok=True)

    name_map = {p['filename']: p['name'] for p in meta.get('participants', [])}

    with get_conn() as c:
        c.execute("""INSERT INTO projects(id,name,created_at,status,source)
                     VALUES(?,?,?,?, 'upload')""",
                  (project_id, meta.get('topic', 'Zoom upload'),
                   datetime.now(timezone.utc).isoformat(), 'uploaded'))
        for idx, f in enumerate(files, start=1):
            dst = os.path.join(proj_dir, f'{idx:02d}_{f.filename}')
            with open(dst, 'wb') as out:
                out.write(await f.read())
            speaker = name_map.get(f.filename, f.filename.rsplit('.',1)[0])
            c.execute("""INSERT INTO recordings
                (project_id, seq, audio_path, mime, duration, created_at,
                 zoom_participant_name)
                VALUES(?,?,?,?,0,?,?)""",
                (project_id, idx, dst, f.content_type or 'audio/m4a',
                 datetime.now(timezone.utc).isoformat(), speaker))
            c.execute("INSERT INTO participants(project_id, idx, name) VALUES(?,?,?)",
                      (project_id, idx, speaker))
    return {'project_id': project_id}
```

- [ ] **Step 2: コミット**

```bash
git add _main_remote.py
git commit -m "feat(zoom): add manual upload endpoint for external-host meetings"
```

---

### Task 12: 管理画面UI - 取り込み待ちセクション

**Files:**
- Modify: `_admin_remote.html`

- [ ] **Step 1: HTML追加（既存プロジェクト一覧の上部）**

`<body>` 内、プロジェクト一覧 `<table>` の前に追加：

```html
<section id="zoom-pending-section" style="margin:24px 0;padding:16px;
         background:#f8f8f8;border-radius:8px;">
  <h2 style="font-size:20px;margin:0 0 12px;">
    Zoom取り込み待ち <span id="zoom-badge" style="background:#d33;color:#fff;
    padding:2px 8px;border-radius:12px;font-size:16px;display:none;">0</span>
  </h2>
  <div id="zoom-pending-list">読み込み中…</div>
  <button onclick="zoomUploadModal()" style="margin-top:12px;padding:8px 16px;
          background:#444;color:#fff;border:none;border-radius:4px;">
    他組織MTGをファイルアップロード
  </button>
</section>
```

- [ ] **Step 2: JS関数追加**

`<script>` 末尾に追加：

```javascript
async function loadZoomPending() {
  const r = await fetch('/api/zoom/pending', {credentials:'include'});
  const data = await r.json();
  const list = document.getElementById('zoom-pending-list');
  const badge = document.getElementById('zoom-badge');
  badge.style.display = data.pending.length ? 'inline-block' : 'none';
  badge.textContent = data.pending.length;
  if (!data.pending.length) {
    list.innerHTML = '<p style="color:#666;">取り込み待ちなし</p>';
    return;
  }
  list.innerHTML = data.pending.map(p => `
    <div style="padding:12px;background:#fff;margin-bottom:8px;border-radius:4px;
         display:flex;justify-content:space-between;align-items:center;">
      <div>
        <div style="font-size:16px;font-weight:bold;">${escapeHtml(p.topic||'(無題)')}</div>
        <div style="font-size:12px;color:#666;">${p.start_time||''} ・ 参加者${p.participants.length}人 ・ ${p.status}</div>
        ${p.error_message?`<div style="color:#d33;font-size:12px;">${escapeHtml(p.error_message)}</div>`:''}
      </div>
      <div>
        ${p.status==='ready'?`<button onclick="zoomImport(${p.id})" style="padding:6px 12px;background:#080;color:#fff;border:none;border-radius:4px;">取り込む</button>`:''}
        <button onclick="zoomDelete(${p.id})" style="padding:6px 12px;background:#888;color:#fff;border:none;border-radius:4px;">破棄</button>
      </div>
    </div>`).join('');
}

async function zoomImport(id) {
  const model = prompt('STTモデルを指定 (whisper-1 / nova-3 / nova-2)', 'whisper-1');
  if (!model) return;
  const run = parseInt(prompt('Run番号 (1 or 2)', '1') || '1');
  const r = await fetch(`/api/zoom/pending/${id}/import`, {
    method:'POST', credentials:'include',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({run, model})});
  if (r.ok) { alert('取り込み開始'); loadZoomPending(); loadProjects(); }
  else alert('失敗: ' + await r.text());
}

async function zoomDelete(id) {
  if (!confirm('この録画を破棄します。よろしいですか？')) return;
  await fetch(`/api/zoom/pending/${id}`, {method:'DELETE', credentials:'include'});
  loadZoomPending();
}

function escapeHtml(s) {
  return (s||'').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

// 初期読み込み・30秒ごとに更新
loadZoomPending();
setInterval(loadZoomPending, 30000);
```

注: 既存 `loadProjects()` 関数名は admin.html 内で実際の関数名を確認して合わせる。

- [ ] **Step 3: コミット**

```bash
git add _admin_remote.html
git commit -m "feat(admin): add Zoom pending section with import/delete actions"
```

---

### Task 13: 管理画面UI - 手動アップロードモーダル

**Files:**
- Modify: `_admin_remote.html`

- [ ] **Step 1: モーダルHTML追加**

`</body>` 直前に追加：

```html
<div id="zoom-upload-modal" style="display:none;position:fixed;inset:0;
     background:rgba(0,0,0,0.5);z-index:1000;align-items:center;justify-content:center;">
  <div style="background:#fff;padding:24px;border-radius:8px;max-width:500px;width:90%;">
    <h3 style="margin-top:0;">Zoom録画ファイルから取り込み</h3>
    <label>MTG名 <input id="zu-topic" style="width:100%;padding:8px;margin:4px 0;"></label>
    <label>音声ファイル（参加者ごと・複数選択可）
      <input id="zu-files" type="file" multiple accept="audio/*,video/*" style="margin:4px 0;">
    </label>
    <div id="zu-name-list" style="margin:8px 0;font-size:14px;"></div>
    <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:16px;">
      <button onclick="document.getElementById('zoom-upload-modal').style.display='none'"
              style="padding:8px 16px;">キャンセル</button>
      <button onclick="zoomUploadSubmit()" style="padding:8px 16px;background:#080;color:#fff;border:none;border-radius:4px;">アップロード</button>
    </div>
  </div>
</div>
```

- [ ] **Step 2: JS関数追加**

```javascript
function zoomUploadModal() {
  document.getElementById('zoom-upload-modal').style.display = 'flex';
  document.getElementById('zu-files').onchange = (e) => {
    const files = Array.from(e.target.files);
    document.getElementById('zu-name-list').innerHTML = files.map((f,i) => `
      <div>${escapeHtml(f.name)} →
        <input id="zu-name-${i}" value="${escapeHtml(f.name.replace(/\.[^.]+$/,''))}"
               style="padding:4px;width:200px;"></div>`).join('');
  };
}

async function zoomUploadSubmit() {
  const topic = document.getElementById('zu-topic').value || 'Zoom upload';
  const fileInput = document.getElementById('zu-files');
  const files = Array.from(fileInput.files);
  if (!files.length) return alert('ファイルを選択してください');

  const fd = new FormData();
  const participants = files.map((f,i) => ({
    filename: f.name,
    name: document.getElementById(`zu-name-${i}`).value
  }));
  fd.append('meta', JSON.stringify({topic, participants}));
  files.forEach(f => fd.append('files', f));

  const r = await fetch('/api/zoom/upload', {method:'POST', credentials:'include', body:fd});
  if (r.ok) {
    alert('アップロード完了');
    document.getElementById('zoom-upload-modal').style.display='none';
    loadProjects();
  } else alert('失敗: ' + await r.text());
}
```

- [ ] **Step 3: コミット**

```bash
git add _admin_remote.html
git commit -m "feat(admin): add manual Zoom upload modal"
```

---

### Task 14: Zoom Marketplace 設定 & デプロイ手順書

**Files:**
- Create: `docs/zoom-setup.md`

- [ ] **Step 1: 手順書作成**

```markdown
# Zoom Marketplace セットアップ手順

## 1. App作成
1. https://marketplace.zoom.us/develop/create にアクセス
2. 「Server-to-Server OAuth」を選択
3. App名: `jizo-voice-memo`

## 2. Credentials
- Client ID / Client Secret / Account ID を取得 → `.env` に設定

## 3. Scopes
- `recording:read:admin`
- `meeting:read:admin`
- `user:read:admin`

## 4. Event Subscriptions
- Event notification endpoint URL: `https://jizo-dev.com/api/zoom/webhook`
- Event types: `Recording > All Recordings have completed` (recording.completed)
- Secret Token: 生成 → `ZOOM_WEBHOOK_SECRET` に設定
- 「Validate」ボタンで疎通確認（URL Validation Challenge）

## 5. アカウント設定（Zoom Web）
- Settings > Recording > Cloud recording: ON
- Settings > Recording > Record a separate audio file for each participant: ON
- （推奨）Settings > Recording > Automatic recording: ON to cloud

## 6. Activate App
- App設定画面右上の「Activate」

## 7. テスト
- 自分1人でテストMTG開始 → クラウド録画開始 → 終了
- 数分後 `journalctl -u jizo-api | grep zoom` で着弾確認
- 管理画面の「Zoom取り込み待ち」セクションに表示されればOK
```

- [ ] **Step 2: コミット**

```bash
git add docs/zoom-setup.md
git commit -m "docs: add Zoom Marketplace setup guide"
```

---

### Task 15: VPSデプロイ

**Files:** なし（運用作業）

- [ ] **Step 1: スキーマ適用**

```bash
scp "c:/Users/taka/Downloads/files (1)/_db_remote.py" root@162.43.14.31:/opt/jizo-api/db.py
ssh root@162.43.14.31 "cd /opt/jizo-api && python3 -c 'import db; db.init_db()'"
```

- [ ] **Step 2: コード配置**

```bash
scp "c:/Users/taka/Downloads/files (1)/_zoom_client_remote.py" root@162.43.14.31:/opt/jizo-api/zoom_client.py
scp "c:/Users/taka/Downloads/files (1)/_zoom_webhook_remote.py" root@162.43.14.31:/opt/jizo-api/zoom_webhook.py
scp "c:/Users/taka/Downloads/files (1)/_main_remote.py"         root@162.43.14.31:/opt/jizo-api/main.py
scp "c:/Users/taka/Downloads/files (1)/_admin_remote.html"      root@162.43.14.31:/var/www/jizo-dev.com/ai-voice-memo/admin/index.html
```

- [ ] **Step 3: .env更新**

ローカルでZoom credentialsを準備（Task 14の手順で取得）後：

```bash
ssh root@162.43.14.31 "cat >> /opt/jizo-api/.env <<EOF
ZOOM_CLIENT_ID=xxx
ZOOM_CLIENT_SECRET=xxx
ZOOM_ACCOUNT_ID=xxx
ZOOM_WEBHOOK_SECRET=xxx
EOF"
```

- [ ] **Step 4: 再起動 & ヘルスチェック**

```bash
ssh root@162.43.14.31 "systemctl restart jizo-api && sleep 2 && systemctl status jizo-api --no-pager"
curl -s https://jizo-dev.com/api/health
```

Expected: status active / health=ok

- [ ] **Step 5: Webhook疎通確認**

Zoom Marketplace の Event Subscriptions ページで「Validate」ボタン押下。
Expected: 緑のチェック表示

- [ ] **Step 6: コミット**

```bash
git commit --allow-empty -m "chore: deploy Zoom integration to VPS"
```

---

### Task 16: 受け入れテスト

**Files:** なし

- [ ] **Step 1: 自分1人MTGテスト**

1. Zoomで1人MTG開始 → クラウド録画ON → 30秒話す → 終了
2. 5〜15分後、`curl -s -u test:test https://jizo-dev.com/api/zoom/pending` で pending 1件確認
3. 管理画面リロード → 「Zoom取り込み待ち 1件」表示確認
4. 「取り込む」クリック → STTモデル指定 → 完了確認
5. プロジェクト詳細で話者名（自分の表示名）が正しく入っていることを確認

- [ ] **Step 2: 2人MTGテスト**

1. 同僚1名と2人MTG → 各自10秒ずつ交互に話す → 終了
2. 取り込み実行
3. 話者分離が完璧か（A=自分・B=同僚で混線なし）確認

- [ ] **Step 3: 手動アップロードテスト**

1. Zoom Web から既存録画をDL（separate audio files）
2. 管理画面の「他組織MTGをファイルアップロード」→ 複数ファイル選択
3. 話者名編集 → アップロード
4. プロジェクト一覧に source=upload で表示されることを確認

- [ ] **Step 4: 完了コミット**

```bash
git commit --allow-empty -m "test: zoom integration acceptance tests passed"
```

---

## 自己レビュー結果

**1. 仕様カバレッジ**
- ✅ Cloud Recording API / Server-to-Server OAuth → Task 3, 5, 14
- ✅ 話者別音声DL → Task 6
- ✅ zoom_pending テーブル / source カラム → Task 2
- ✅ Webhook 5本 → Task 5, 7, 8, 9, 11
- ✅ Gemini source='zoom' 分岐 → Task 10
- ✅ 管理画面UI → Task 12, 13
- ✅ 手動アップロード → Task 11, 13
- ✅ Zoom Marketplace 手順 → Task 14
- ✅ デプロイ・受け入れ → Task 15, 16

**2. プレースホルダー**
- Task 8 Step 2 / Task 10 Step 1 で「既存ロジックをコピー」指示あり。これは _main_remote.py の既存実装を読まないと正確なコピー元が決まらないため意図的。実装時に該当箇所をRead→流用すること。

**3. 型整合**
- `ZoomClient` / `verify_signature` / `_zoom_download_worker` / `_run_stt_for_recording` のシグネチャ全タスクで一致。
- DB列名 `zoom_participant_name` / `source` 全タスクで一致。
