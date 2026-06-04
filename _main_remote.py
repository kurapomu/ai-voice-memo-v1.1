import os, uuid, json, re, logging, asyncio
from datetime import datetime, timezone, timedelta
from logging.handlers import TimedRotatingFileHandler

from fastapi import FastAPI, UploadFile, Form, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import FileResponse
import httpx, secrets
from openai import AsyncOpenAI
from dotenv import load_dotenv
from db import init_db, get_conn
from zoom_client import ZoomClient
from zoom_webhook import verify_signature, build_url_validation_response

load_dotenv()
init_db()

# ── ロギング設定 ──────────────────────────────────
LOG_DIR  = '/var/log/jizo-api'
LOG_FILE = os.path.join(LOG_DIR, 'app.log')
os.makedirs(LOG_DIR, exist_ok=True)

_fmt = logging.Formatter('%(asctime)s %(levelname)s: %(message)s',
                          datefmt='%Y-%m-%d %H:%M:%S')
# 12時間ごとにローテーション・2世代保持（=最大24時間分）
_fh = TimedRotatingFileHandler(LOG_FILE, when='H', interval=12, backupCount=2, encoding='utf-8')
_fh.setFormatter(_fmt)
_fh.setLevel(logging.DEBUG)

logger = logging.getLogger('jizo-api')
logger.setLevel(logging.DEBUG)
logger.addHandler(_fh)
logger.propagate = False  # uvicornのrootロガーに伝播させない

API_KEY     = os.environ['ASSEMBLYAI_API_KEY']
AAI_HDR     = {'authorization': API_KEY}
AAI_BASE    = 'https://api.assemblyai.com'
GEMINI_KEY    = os.environ.get('GEMINI_API_KEY', '')
GEMINI_BASE   = 'https://generativelanguage.googleapis.com/v1beta/openai'
OPENAI_KEY    = os.environ.get('OPENAI_API_KEY', '')
DEEPGRAM_KEY  = os.environ.get('DEEPGRAM_API_KEY', '')
DEEPGRAM_BASE = 'https://api.deepgram.com/v1'

# ── Zoom 連携（v1） ──────────────────────────────
ZOOM_CLIENT_ID      = os.environ.get('ZOOM_CLIENT_ID', '')
ZOOM_CLIENT_SECRET  = os.environ.get('ZOOM_CLIENT_SECRET', '')
ZOOM_ACCOUNT_ID     = os.environ.get('ZOOM_ACCOUNT_ID', '')
ZOOM_WEBHOOK_SECRET = os.environ.get('ZOOM_WEBHOOK_SECRET', '')
ZOOM_AUDIO_DIR      = '/var/jizo/audio/zoom'
os.makedirs(ZOOM_AUDIO_DIR, exist_ok=True)

# 環境変数が未設定なら None（Webhook受信時に明示エラー）
zoom_client = ZoomClient(ZOOM_CLIENT_ID, ZOOM_CLIENT_SECRET, ZOOM_ACCOUNT_ID) \
              if ZOOM_CLIENT_ID and ZOOM_CLIENT_SECRET and ZOOM_ACCOUNT_ID else None

# ── 使用量上限（無料枠ベース・デフォルト）─────────
USAGE_LIMITS = {
    'whisper': {
        'daily_minutes':   30,
        'monthly_minutes': 100,
        'rate_usd_per_min': 0.006,
        'free_tier_note':  '事前チャージ（最低$5）',
    },
    'deepgram': {
        'daily_minutes':   60,
        'monthly_minutes': 750,
        'rate_usd_per_min': 0.0043,
        'free_tier_note':  '$200無料クレジット（約12時間/750分）',
    },
    'gemini-flash': {
        'daily_requests': 1000,
        'daily_tokens':   800_000,
        'monthly_requests': 30_000,
        'rate_usd_per_1m_in':  0.30,
        'rate_usd_per_1m_out': 2.50,
        'free_tier_note': '無料枠: 1500req/日・15req/分',
    },
    'gemini-pro': {
        'daily_requests': 80,
        'daily_tokens':   200_000,
        'monthly_requests': 2_400,
        'rate_usd_per_1m_in':  1.25,
        'rate_usd_per_1m_out': 10.00,
        'free_tier_note': '無料枠: 100req/日・5req/分',
    },
}

def _service_key(engine_or_model: str) -> str:
    """エンジン名/モデル名から使用量集計キーを返す"""
    e = (engine_or_model or '').lower()
    if e == 'whisper' or 'whisper' in e: return 'whisper'
    if e == 'deepgram' or 'nova' in e:   return 'deepgram'
    if 'flash' in e: return 'gemini-flash'
    if 'pro'   in e: return 'gemini-pro'
    return e or 'unknown'


def _record_usage(service: str, units: float, unit_type: str,
                  project_id: str = None, endpoint: str = None,
                  cost_usd: float = 0.0):
    """使用量を api_usage テーブルに記録"""
    try:
        c = get_conn()
        c.execute(
            'INSERT INTO api_usage(service,units,unit_type,project_id,endpoint,cost_usd,created_at) '
            'VALUES(?,?,?,?,?,?,?)',
            (service, units, unit_type, project_id, endpoint, cost_usd, now_iso())
        )
        c.commit()
        c.close()
        logger.info(f'usage: {service}={units}{unit_type} cost=${cost_usd:.4f}')
    except Exception as e:
        logger.warning(f'usage record failed: {e}')


def _get_usage(service: str, period: str = 'day') -> dict:
    """指定サービス・期間（day/month）の使用量集計"""
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    if period == 'day':
        since = (now - timedelta(days=1)).isoformat()
    else:
        since = (now - timedelta(days=30)).isoformat()
    c = get_conn()
    rows = c.execute(
        'SELECT unit_type, SUM(units) AS u, SUM(cost_usd) AS cost, COUNT(*) AS cnt '
        'FROM api_usage WHERE service=? AND created_at>=? GROUP BY unit_type',
        (service, since)
    ).fetchall()
    c.close()
    result = {'requests': 0, 'minutes': 0.0, 'tokens': 0, 'cost_usd': 0.0}
    for r in rows:
        d = dict(r)
        if d['unit_type'] == 'minutes':  result['minutes'] = d['u']
        elif d['unit_type'] == 'tokens': result['tokens']  = int(d['u'])
        result['cost_usd'] += d['cost'] or 0
        result['requests'] += d['cnt']
    return result


def _check_limit(service: str, expected_units: float = 0, expected_unit_type: str = 'minutes'):
    """上限超過チェック。超えていたら HTTPException(429) raise"""
    limits = USAGE_LIMITS.get(service, {})
    if not limits: return  # 未定義サービスはスルー

    daily   = _get_usage(service, 'day')
    monthly = _get_usage(service, 'month')

    if expected_unit_type == 'minutes':
        if 'daily_minutes' in limits and daily['minutes'] + expected_units > limits['daily_minutes']:
            raise HTTPException(429,
                f'{service} 日次上限超過：{daily["minutes"]:.1f}分 / {limits["daily_minutes"]}分'
                f'（予定追加: {expected_units:.1f}分）')
        if 'monthly_minutes' in limits and monthly['minutes'] + expected_units > limits['monthly_minutes']:
            raise HTTPException(429,
                f'{service} 月次上限超過：{monthly["minutes"]:.1f}分 / {limits["monthly_minutes"]}分')
    elif expected_unit_type == 'requests':
        if 'daily_requests' in limits and daily['requests'] + 1 > limits['daily_requests']:
            raise HTTPException(429,
                f'{service} 日次リクエスト上限超過：{daily["requests"]} / {limits["daily_requests"]}')
AUDIO_DIR   = os.environ.get('AUDIO_DIR', '/var/jizo/audio')
ADMIN_USER  = os.environ.get('ADMIN_USER', 'admin')
ADMIN_PASS  = os.environ.get('ADMIN_PASS', 'changeme')

os.makedirs(AUDIO_DIR, exist_ok=True)

app = FastAPI()
security = HTTPBasic()

app.add_middleware(
    CORSMiddleware,
    allow_origins=['https://jizo-dev.com'],
    allow_methods=['*'],
    allow_headers=['*'],
)

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def require_admin(creds: HTTPBasicCredentials = Depends(security)):
    ok = (
        secrets.compare_digest(creds.username.encode(), ADMIN_USER.encode()) and
        secrets.compare_digest(creds.password.encode(), ADMIN_PASS.encode())
    )
    if not ok:
        raise HTTPException(401, 'Unauthorized', headers={'WWW-Authenticate': 'Basic'})
    return creds.username


# ── ヘルス ──────────────────────────────────────────
@app.get('/api/health')
async def health():
    logger.info('health check')
    return {'ok': True}


# ── ログ閲覧（管理画面・Claude Code用） ──────────────
@app.get('/api/logs')
async def get_logs(lines: int = 200, _: str = Depends(require_admin)):
    try:
        with open(LOG_FILE, encoding='utf-8', errors='replace') as f:
            all_lines = f.readlines()
        tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
        return {'lines': len(tail), 'log': ''.join(tail)}
    except FileNotFoundError:
        return {'lines': 0, 'log': '(ログファイルなし)'}


# ── プロジェクト作成（モバイルからのアップロード） ──────
@app.post('/api/projects')
async def create_project(
    meta: str = Form(...),
    audio: list[UploadFile] = [],
):
    """
    multipart/form-data:
      meta  : JSON文字列 { name, participants:[str], segments:[{text,speakerIdx,ts,highlight}] }
      audio : 音声ファイル（0個以上、停止/再開ごとの複数チャンク）
    """
    try:
        m = json.loads(meta)
    except Exception:
        raise HTTPException(400, 'meta must be valid JSON')

    project_id   = str(uuid.uuid4())
    name           = m.get('name', '')
    participants   = m.get('participants', [])
    segments       = m.get('segments', [])
    speaker_events = m.get('speaker_events', []) or []

    conn = get_conn()
    try:
        conn.execute(
            'INSERT INTO projects(id,name,created_at,status) VALUES(?,?,?,?)',
            (project_id, name, now_iso(), 'uploaded')
        )
        for idx, pname in enumerate(participants):
            conn.execute(
                'INSERT INTO participants(project_id,idx,name) VALUES(?,?,?)',
                (project_id, idx, pname or '')
            )
        for i, seg in enumerate(segments):
            conn.execute(
                'INSERT INTO ref_segments(project_id,seq,text,speaker_idx,ts,highlight) VALUES(?,?,?,?,?,?)',
                (project_id, i, seg.get('text', ''), seg.get('speakerIdx'),
                 seg.get('ts', ''), int(bool(seg.get('highlight', False))))
            )
        # 話者ボタン押下イベント（LLM突合の権威データ）
        for i, ev in enumerate(speaker_events):
            conn.execute(
                'INSERT INTO speaker_events(project_id,seq,ts,ms,speaker_idx,speaker_name) VALUES(?,?,?,?,?,?)',
                (project_id, i,
                 ev.get('ts', ''),
                 int(ev.get('ms') or 0),
                 ev.get('speaker_idx'),
                 ev.get('speaker_name', ''))
            )
        _MIME_EXT = {
            'audio/webm': 'webm', 'audio/wav': 'wav', 'audio/x-wav': 'wav',
            'audio/mpeg': 'mp3', 'audio/mp3': 'mp3',
            'audio/mp4': 'mp4', 'audio/m4a': 'm4a', 'audio/x-m4a': 'm4a',
            'audio/ogg': 'ogg', 'audio/flac': 'flac', 'audio/x-flac': 'flac',
        }
        def _ext_for(upload):
            mt = (upload.content_type or '').lower()
            if mt in _MIME_EXT: return _MIME_EXT[mt]
            fn = (upload.filename or '').lower()
            for ext in ('webm','wav','mp3','m4a','mp4','ogg','flac'):
                if fn.endswith('.' + ext): return ext
            return 'webm'

        saved_seq = 0
        for f in audio:
            data = await f.read()
            if not data:
                logger.warning(f'create_project: empty audio chunk skipped (project={project_id}, filename={f.filename})')
                continue
            ext = _ext_for(f)
            path = os.path.join(AUDIO_DIR, f'{project_id}_{saved_seq}.{ext}')
            with open(path, 'wb') as fp:
                fp.write(data)
            conn.execute(
                'INSERT INTO recordings(project_id,seq,audio_path,mime,duration,created_at) VALUES(?,?,?,?,?,?)',
                (project_id, saved_seq, path, f.content_type or f'audio/{ext}', '', now_iso())
            )
            saved_seq += 1
        conn.commit()
    finally:
        conn.close()

    return {'project_id': project_id, 'recordings': len(audio)}


# ── keyterms生成（参加者名 + カタカナ固有名詞） ─────────
def _build_keyterms(project_id: str, conn) -> list:
    names = [
        r['name'] for r in conn.execute(
            'SELECT name FROM participants WHERE project_id=? AND name != ""', (project_id,)
        ).fetchall()
    ]
    texts = ' '.join(
        r['text'] for r in conn.execute(
            'SELECT text FROM ref_segments WHERE project_id=?', (project_id,)
        ).fetchall()
    )
    # カタカナ3文字以上を固有名詞候補として抽出
    katakana = re.findall(r'[ァ-ヶー]{3,}', texts)
    terms = list({t for t in (names + katakana) if t})[:100]
    return terms


# ── デフォルトRun設定取得（管理画面用）──────────────
@app.get('/api/run-defaults')
async def get_run_defaults(_: str = Depends(require_admin)):
    return DEFAULT_SETTINGS


# ── 設定画面用：APIキー状態・上限・使用量 ────────────
def _mask_key(k: str) -> str:
    if not k: return ''
    if len(k) <= 12: return '*' * len(k)
    return k[:7] + '...' + k[-4:]

# ── Deepgram公式：プロジェクトID・使用量・残高 ──────
_dg_project_id = None

async def _get_dg_project_id():
    global _dg_project_id
    if _dg_project_id:
        return _dg_project_id
    if not DEEPGRAM_KEY:
        raise HTTPException(503, 'Deepgram key not configured')
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f'{DEEPGRAM_BASE}/projects',
            headers={'Authorization': f'Token {DEEPGRAM_KEY}'},
        )
        if r.status_code != 200:
            raise HTTPException(502, f'Deepgram projects: {r.text[:300]}')
        projs = r.json().get('projects') or []
        if not projs:
            raise HTTPException(500, 'No Deepgram projects found')
        _dg_project_id = projs[0]['project_id']
        return _dg_project_id


@app.get('/api/deepgram-stats')
async def get_deepgram_stats(days: int = 30, _: str = Depends(require_admin)):
    """Deepgram公式集計（使用量＋残高）を取得"""
    pid = await _get_dg_project_id()
    now   = datetime.now(timezone.utc)
    start = (now - timedelta(days=days)).date().isoformat()
    end   = now.date().isoformat()
    hdr   = {'Authorization': f'Token {DEEPGRAM_KEY}'}

    async with httpx.AsyncClient(timeout=20) as client:
        u = await client.get(
            f'{DEEPGRAM_BASE}/projects/{pid}/usage/breakdown',
            params={'start': start, 'end': end}, headers=hdr,
        )
        b = await client.get(
            f'{DEEPGRAM_BASE}/projects/{pid}/balances', headers=hdr,
        )
    return {
        'project_id': pid,
        'period': {'start': start, 'end': end},
        'usage':   u.json() if u.status_code == 200 else {'error': u.text[:300]},
        'balance': b.json() if b.status_code == 200 else {'error': b.text[:300]},
    }


# ── 使用履歴（運用デバッグ・Claude Code閲覧用）──────
@app.get('/api/usage-history')
async def get_usage_history(days: int = 7, service: str = None,
                            limit: int = 200, _: str = Depends(require_admin)):
    """
    API使用履歴を時系列で返す（プロジェクト名と紐付け）
    days: 過去N日（デフォルト7）
    service: 'whisper' / 'deepgram' / 'gemini-flash' / 'gemini-pro' でフィルタ
    limit: 最大件数
    """
    from datetime import timedelta
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    sql = '''
        SELECT u.id, u.service, u.units, u.unit_type, u.endpoint,
               u.cost_usd, u.created_at,
               u.project_id, p.name AS project_name
        FROM api_usage u
        LEFT JOIN projects p ON p.id = u.project_id
        WHERE u.created_at >= ?
    '''
    args = [since]
    if service:
        sql += ' AND u.service = ?'
        args.append(service)
    sql += ' ORDER BY u.created_at DESC LIMIT ?'
    args.append(limit)

    conn = get_conn()
    rows = conn.execute(sql, args).fetchall()

    # 集計
    summary_rows = conn.execute(
        '''SELECT service, unit_type, COUNT(*) AS calls,
                  SUM(units) AS total, SUM(cost_usd) AS cost
           FROM api_usage WHERE created_at >= ?
           GROUP BY service, unit_type ORDER BY service''',
        [since]
    ).fetchall()
    conn.close()

    return {
        'since': since,
        'days': days,
        'summary': [dict(r) for r in summary_rows],
        'records': [dict(r) for r in rows],
    }


# ── プロジェクト操作ログ（プロジェクト一覧 + 解析履歴）─
@app.get('/api/project-activity')
async def get_project_activity(_: str = Depends(require_admin)):
    """全プロジェクトの活動サマリーを返す（運用閲覧用）"""
    conn = get_conn()
    rows = conn.execute('''
        SELECT p.id, p.name, p.created_at, p.status,
               (SELECT COUNT(*) FROM recordings WHERE project_id=p.id) AS recordings,
               (SELECT COUNT(*) FROM ai_transcripts WHERE project_id=p.id) AS analyses,
               (SELECT COUNT(*) FROM merged_transcripts WHERE project_id=p.id) AS merges,
               (SELECT COUNT(*) FROM qa_history WHERE project_id=p.id) AS qa_count,
               (SELECT SUM(cost_usd) FROM api_usage WHERE project_id=p.id) AS cost_usd
        FROM projects p
        ORDER BY p.created_at DESC
    ''').fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── APIキー更新（.envを書き換え＋プロセス内反映）─────
@app.patch('/api/settings/keys')
async def update_api_keys(request: Request, _: str = Depends(require_admin)):
    """
    APIキーを更新。Body: { "whisper": "sk-...", "deepgram": "...", "gemini": "..." }
    指定されたキーのみ更新（空文字や省略は無視）。
    .envファイルを書き換え、プロセス内の変数も即時更新。
    """
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, 'body must be a JSON object')

    # キー名 → .envの変数名
    KEY_MAP = {
        'whisper':  'OPENAI_API_KEY',
        'deepgram': 'DEEPGRAM_API_KEY',
        'gemini':   'GEMINI_API_KEY',
    }
    updates = {}
    for k, env_name in KEY_MAP.items():
        v = (body.get(k) or '').strip()
        if v and not v.startswith('*'):  # マスク表示そのまま送られたら無視
            updates[env_name] = v

    if not updates:
        raise HTTPException(400, 'no valid keys to update')

    # .envを読んで更新（既存行を置換 or 末尾追加）
    env_path = '/opt/jizo-api/.env'
    try:
        with open(env_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except FileNotFoundError:
        lines = []

    updated_keys = set()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        # コメント・空行はそのまま
        if not stripped or stripped.startswith('#'):
            new_lines.append(line)
            continue
        if '=' in stripped:
            key_name = stripped.split('=', 1)[0].strip()
            if key_name in updates:
                new_lines.append(f'{key_name}={updates[key_name]}\n')
                updated_keys.add(key_name)
                continue
        new_lines.append(line)

    # 未追記のものは末尾に追加
    for key_name, value in updates.items():
        if key_name not in updated_keys:
            if new_lines and not new_lines[-1].endswith('\n'):
                new_lines.append('\n')
            new_lines.append(f'{key_name}={value}\n')

    with open(env_path, 'w', encoding='utf-8') as f:
        f.writelines(new_lines)

    # プロセス内変数を更新（再起動なしで反映）
    global OPENAI_KEY, DEEPGRAM_KEY, GEMINI_KEY, _dg_project_id
    if 'OPENAI_API_KEY' in updates:
        OPENAI_KEY = updates['OPENAI_API_KEY']
        os.environ['OPENAI_API_KEY'] = OPENAI_KEY
    if 'DEEPGRAM_API_KEY' in updates:
        DEEPGRAM_KEY = updates['DEEPGRAM_API_KEY']
        os.environ['DEEPGRAM_API_KEY'] = DEEPGRAM_KEY
        _dg_project_id = None  # キー変更時はproject_idキャッシュをリセット
    if 'GEMINI_API_KEY' in updates:
        GEMINI_KEY = updates['GEMINI_API_KEY']
        os.environ['GEMINI_API_KEY'] = GEMINI_KEY

    logger.info(f'API keys updated: {list(updates.keys())}')
    return {'ok': True, 'updated': list(updates.keys())}


@app.get('/api/settings')
async def get_settings(_: str = Depends(require_admin)):
    """APIキー（マスク済み）・上限・現在の使用量を返す"""
    services = ['whisper', 'deepgram', 'gemini-flash', 'gemini-pro']
    keys = {
        'whisper':      {'configured': bool(OPENAI_KEY),   'masked': _mask_key(OPENAI_KEY)},
        'deepgram':     {'configured': bool(DEEPGRAM_KEY), 'masked': _mask_key(DEEPGRAM_KEY)},
        'gemini-flash': {'configured': bool(GEMINI_KEY),   'masked': _mask_key(GEMINI_KEY)},
        'gemini-pro':   {'configured': bool(GEMINI_KEY),   'masked': _mask_key(GEMINI_KEY)},
    }
    usage = {}
    for svc in services:
        daily   = _get_usage(svc, 'day')
        monthly = _get_usage(svc, 'month')
        limits  = USAGE_LIMITS.get(svc, {})
        usage[svc] = {
            'limits':  limits,
            'daily':   daily,
            'monthly': monthly,
        }
    return {'keys': keys, 'usage': usage}


# ── 解析エンジン（Whisper / Deepgram）─────────────
DEFAULT_SETTINGS = {
    1: {  # Run1 デフォルト：Whisper・日本語固定・プロンプトなし
        'engine': 'whisper',
        'language': 'ja',
        'prompt': '',
        'model': 'whisper-1',
        'diarize': False,
    },
    2: {  # Run2 デフォルト：Deepgram・日本語・話者分離on
        'engine': 'deepgram',
        'language': 'ja',
        'prompt': '',
        'model': 'nova-3',
        'diarize': True,
    },
}


async def _run_whisper(audio_path: str, settings: dict, pnames: list, keyterms: list,
                       project_id: str = None) -> tuple:
    """Whisper API呼び出し。(utterances, full_text) を返す"""
    oai = AsyncOpenAI(api_key=OPENAI_KEY)
    with open(audio_path, 'rb') as fp:
        kwargs = {
            'model': settings.get('model') or 'whisper-1',
            'file': fp,
            'response_format': 'verbose_json',
            'timestamp_granularities': ['segment'],
        }
        lang = settings.get('language') or ''
        if lang and lang != 'auto':
            kwargs['language'] = lang
        prompt = (settings.get('prompt') or '').strip()
        if prompt:
            kwargs['prompt'] = prompt
        transcript = await oai.audio.transcriptions.create(**kwargs)
    segs = getattr(transcript, 'segments', None) or []
    utterances = [
        {'speaker': 'A',
         'start': int(s.start * 1000),
         'end':   int(s.end   * 1000),
         'text':  s.text.strip()}
        for s in segs
    ]
    # 使用量記録（duration秒→分）
    duration_sec = float(getattr(transcript, 'duration', 0) or 0)
    minutes = duration_sec / 60.0
    cost = minutes * USAGE_LIMITS['whisper']['rate_usd_per_min']
    _record_usage('whisper', minutes, 'minutes', project_id, '/analyze', cost)
    return utterances, (transcript.text or ''), duration_sec


async def _run_deepgram(audio_path: str, settings: dict, pnames: list, keyterms: list,
                        project_id: str = None) -> tuple:
    """Deepgram API呼び出し。(utterances, full_text) を返す"""
    if not DEEPGRAM_KEY:
        raise HTTPException(500, 'DEEPGRAM_API_KEY not configured')

    params = {
        'model': settings.get('model') or 'nova-3',
        'language': settings.get('language') or 'ja',
        'smart_format': 'true',
        'punctuate': 'true',
        'paragraphs': 'true',
        'utterances': 'true',
    }
    if settings.get('diarize'):
        params['diarize'] = 'true'
    # 固有名詞・参加者名を補強（Nova-3は keyterm、それ以前は keywords）
    kws = list(set([*pnames, *(keyterms or [])]))[:20]
    if kws:
        model_name = (settings.get('model') or 'nova-3').lower()
        if model_name.startswith('nova-3'):
            params['keyterm'] = kws
        else:
            params['keywords'] = kws
    prompt = (settings.get('prompt') or '').strip()
    if prompt:
        # Deepgramは直接的なpromptはないが、keytermsで代用済み。promptはログだけ
        logger.info(f'Deepgram prompt (info only): {prompt[:80]}')

    with open(audio_path, 'rb') as fp:
        audio_bytes = fp.read()

    async with httpx.AsyncClient(timeout=300) as client:
        r = await client.post(
            f'{DEEPGRAM_BASE}/listen',
            params=params,
            headers={
                'Authorization': f'Token {DEEPGRAM_KEY}',
                'content-type': 'audio/webm',
            },
            content=audio_bytes,
        )
        if r.status_code != 200:
            raise HTTPException(502, f'Deepgram failed: {r.text[:500]}')
        data = r.json()

    # utterances（話者分離あり）優先、なければwordsから合成
    utts_raw = (data.get('results', {}).get('utterances') or [])
    if utts_raw:
        utterances = [
            {
                'speaker': chr(65 + (u.get('speaker') or 0)),  # 0→'A', 1→'B'…
                'start': int((u.get('start') or 0) * 1000),
                'end':   int((u.get('end')   or 0) * 1000),
                'text':  (u.get('transcript') or '').strip(),
            }
            for u in utts_raw
        ]
    else:
        # フォールバック：channels[0].alternatives[0].paragraphs
        utterances = []
        channels = data.get('results', {}).get('channels') or []
        if channels and channels[0].get('alternatives'):
            alt = channels[0]['alternatives'][0]
            for para in (alt.get('paragraphs', {}).get('paragraphs') or []):
                for s in para.get('sentences', []):
                    utterances.append({
                        'speaker': chr(65 + (para.get('speaker') or 0)),
                        'start': int((s.get('start') or 0) * 1000),
                        'end':   int((s.get('end')   or 0) * 1000),
                        'text':  (s.get('text') or '').strip(),
                    })

    full_text = ''
    chans = data.get('results', {}).get('channels') or []
    if chans and chans[0].get('alternatives'):
        full_text = chans[0]['alternatives'][0].get('transcript', '')

    # 使用量記録
    duration_sec = float(data.get('metadata', {}).get('duration') or 0)
    minutes = duration_sec / 60.0
    cost = minutes * USAGE_LIMITS['deepgram']['rate_usd_per_min']
    _record_usage('deepgram', minutes, 'minutes', project_id, '/analyze', cost)
    return utterances, full_text, duration_sec


@app.post('/api/projects/{project_id}/analyze')
async def analyze_project(project_id: str, run: int = 1, force: bool = False,
                          request: Request = None):
    """
    Body (optional JSON): { engine, language, prompt, model, diarize }
    指定なければ DEFAULT_SETTINGS[run] を使用
    """
    if run not in (1, 2):
        raise HTTPException(400, 'run must be 1 or 2')

    # 設定マージ
    settings = dict(DEFAULT_SETTINGS[run])
    try:
        body = await request.json() if request else {}
        if isinstance(body, dict):
            for k in ('engine', 'language', 'prompt', 'model', 'diarize'):
                if k in body and body[k] is not None:
                    settings[k] = body[k]
    except Exception:
        pass

    engine = settings.get('engine') or 'whisper'
    if engine not in ('whisper', 'deepgram'):
        raise HTTPException(400, f'engine must be whisper or deepgram')

    conn = get_conn()
    existing = conn.execute(
        "SELECT COUNT(*) FROM ai_transcripts WHERE project_id=? AND run_number=? AND status NOT IN ('error')",
        (project_id, run)
    ).fetchone()[0]
    if existing > 0:
        if force:
            conn.execute('DELETE FROM ai_transcripts WHERE project_id=? AND run_number=?',
                         (project_id, run))
            conn.commit()
        else:
            conn.close()
            raise HTTPException(409, f'Run {run} already exists for this project')

    recs_all = conn.execute(
        'SELECT * FROM recordings WHERE project_id=? ORDER BY seq', (project_id,)
    ).fetchall()
    if not recs_all:
        conn.close()
        raise HTTPException(404, 'No recordings for this project')

    # 0バイト or 欠損ファイルを除外（Whisper/Deepgramが落ちるのを防ぐ）
    recs = []
    for r in recs_all:
        p = r['audio_path']
        if not os.path.exists(p) or os.path.getsize(p) == 0:
            logger.warning(f'analyze: skip empty/missing audio rec_id={r["id"]} path={p}')
            continue
        recs.append(r)
    if not recs:
        conn.close()
        raise HTTPException(400, 'All recordings are empty or missing')

    keyterms = _build_keyterms(project_id, conn)
    pnames = [r['name'] for r in conn.execute(
        'SELECT name FROM participants WHERE project_id=? AND name != ""', (project_id,)
    ).fetchall()]

    # 上限チェック（音声合計の推定。録音ファイル数で粗推定し、実際の使用量は呼び出し後に記録）
    try:
        _check_limit(engine, expected_units=0, expected_unit_type='minutes')
    except HTTPException:
        conn.close()
        raise

    total_segments = 0
    for rec in recs:
        logger.info(f'analyze run={run} engine={engine} rec={rec["id"]} settings={settings}')
        try:
            if engine == 'whisper':
                utterances, full_text, duration_sec = await _run_whisper(rec['audio_path'], settings, pnames, keyterms, project_id)
            else:
                utterances, full_text, duration_sec = await _run_deepgram(rec['audio_path'], settings, pnames, keyterms, project_id)
        except HTTPException:
            conn.close()
            raise
        except Exception as e:
            conn.close()
            logger.error(f'{engine} error: {e}')
            raise HTTPException(502, f'{engine} failed: {e}')

        logger.info(f'{engine} done segments={len(utterances)} duration={duration_sec:.2f}s')
        total_segments += len(utterances)

        # recordings.duration を秒文字列で更新（管理画面のチャンクオフセット計算に使用）
        if duration_sec > 0 and not (rec['duration'] or '').strip():
            conn.execute(
                'UPDATE recordings SET duration=? WHERE id=?',
                (f'{duration_sec:.3f}', rec['id'])
            )

        ext_id = f'{engine}:{rec["id"]}:{run}'
        conn.execute(
            '''INSERT INTO ai_transcripts
               (project_id,recording_id,run_number,aai_id,status,full_text,
                utterances_json,speaker_map_json,engine,settings_json)
               VALUES(?,?,?,?,?,?,?,?,?,?)''',
            (project_id, rec['id'], run, ext_id, 'completed',
             full_text,
             json.dumps(utterances, ensure_ascii=False),
             json.dumps({}, ensure_ascii=False),
             engine,
             json.dumps(settings, ensure_ascii=False))
        )

    status_map = {1: 'completed_run1', 2: 'completed_run2'}
    conn.execute('UPDATE projects SET status=? WHERE id=?', (status_map[run], project_id))
    conn.commit()
    conn.close()
    return {'ok': True, 'run': run, 'recordings': len(recs),
            'segments': total_segments, 'engine': engine, 'settings': settings}


# ── 解析状態ポーリング + 突合 ──────────────────────
@app.get('/api/projects/{project_id}/poll')
async def poll_project(project_id: str):
    conn = get_conn()
    txs = conn.execute(
        "SELECT * FROM ai_transcripts WHERE project_id=? AND status NOT IN ('completed','error')",
        (project_id,)
    ).fetchall()

    async with httpx.AsyncClient(timeout=15) as client:
        for tx in txs:
            # Whisper は同期完了済みのためポーリング不要
            if str(tx['aai_id']).startswith('whisper:'):
                continue
            r = await client.get(f'{AAI_BASE}/v2/transcript/{tx["aai_id"]}', headers=AAI_HDR)
            d = r.json()
            st = d.get('status')
            if st == 'completed':
                utterances  = d.get('utterances') or []
                speaker_map = _build_speaker_map(project_id, utterances, conn)
                conn.execute(
                    '''UPDATE ai_transcripts
                       SET status=?, full_text=?, utterances_json=?, speaker_map_json=?
                       WHERE id=?''',
                    ('completed', d.get('text', ''),
                     json.dumps(utterances, ensure_ascii=False),
                     json.dumps(speaker_map, ensure_ascii=False),
                     tx['id'])
                )
            elif st == 'error':
                conn.execute(
                    "UPDATE ai_transcripts SET status='error', error=? WHERE id=?",
                    (d.get('error', 'unknown'), tx['id'])
                )

    # 全件の状態を集計してprojectステータスを更新
    all_txs = conn.execute(
        'SELECT run_number, status FROM ai_transcripts WHERE project_id=?', (project_id,)
    ).fetchall()

    pending = sum(1 for t in all_txs if t['status'] not in ('completed', 'error'))
    if pending == 0 and all_txs:
        run_numbers = {t['run_number'] for t in all_txs}
        if 2 in run_numbers:
            conn.execute("UPDATE projects SET status='completed_run2' WHERE id=?", (project_id,))
        else:
            conn.execute("UPDATE projects SET status='completed_run1' WHERE id=?", (project_id,))

    conn.commit()
    proj = conn.execute('SELECT * FROM projects WHERE id=?', (project_id,)).fetchone()
    conn.close()
    return {'status': proj['status'], 'pending': pending}


def _build_speaker_map(project_id: str, utterances: list, conn) -> dict:
    """タイムスタンプ突合でAssemblyAI話者クラスタ→参加者名マッピングを生成"""
    participants = {
        row['idx']: row['name']
        for row in conn.execute(
            'SELECT idx,name FROM participants WHERE project_id=?', (project_id,)
        ).fetchall()
    }
    ref_segs = conn.execute(
        'SELECT speaker_idx,ts FROM ref_segments WHERE project_id=? AND speaker_idx IS NOT NULL',
        (project_id,)
    ).fetchall()

    def ts_to_ms(ts: str) -> int:
        parts = ts.split(':')
        try:
            return (int(parts[0]) * 60 + int(parts[1])) * 1000
        except Exception:
            return 0

    ref_list = [(ts_to_ms(r['ts']), r['speaker_idx']) for r in ref_segs]

    votes: dict = {}
    for utt in utterances:
        spk   = utt.get('speaker', 'A')
        start = utt.get('start', 0)
        end   = utt.get('end', 0)
        mid   = (start + end) / 2
        votes.setdefault(spk, {})
        for (ref_ms, ref_idx) in ref_list:
            if abs(ref_ms - mid) < 10000:
                votes[spk][ref_idx] = votes[spk].get(ref_idx, 0) + 1

    result = {}
    for spk, vote_map in votes.items():
        if vote_map:
            best_idx = max(vote_map, key=lambda k: vote_map[k])
            result[spk] = participants.get(best_idx, f'話者{best_idx + 1}')
        else:
            result[spk] = f'Speaker {spk}'
    return result


# ── LLM突合で最終版トランスクリプト生成 ──────────────
ALLOWED_MODELS = {
    'gemini-2.5-flash':  'Gemini 2.5 Flash（推奨）',
    'gemini-2.5-pro':    'Gemini 2.5 Pro（高品質）',
}

@app.post('/api/projects/{project_id}/merge')
async def merge_project(project_id: str,
                        model: str = 'gemini-2.5-flash',
                        w_ws: int = 2,
                        w_run1: int = 4,
                        w_run2: int = 4,
                        base: str = '',
                        _: str = Depends(require_admin)):
    conn = get_conn()
    proj = conn.execute('SELECT * FROM projects WHERE id=?', (project_id,)).fetchone()
    if not proj:
        conn.close()
        raise HTTPException(404, 'Project not found')

    # source カラムは Task 2 のスキーマ拡張で追加される。未存在時は 'mobile' 扱い
    try:
        source = (proj['source'] or 'mobile') if 'source' in proj.keys() else 'mobile'
    except Exception:
        source = 'mobile'

    participants = conn.execute(
        'SELECT idx,name FROM participants WHERE project_id=? ORDER BY idx', (project_id,)
    ).fetchall()
    ref_segs = conn.execute(
        'SELECT seq,text,speaker_idx,ts FROM ref_segments WHERE project_id=? ORDER BY seq',
        (project_id,)
    ).fetchall()
    speaker_events = conn.execute(
        'SELECT ts,ms,speaker_idx,speaker_name FROM speaker_events WHERE project_id=? ORDER BY ms',
        (project_id,)
    ).fetchall()
    name_map = {r['idx']: (r['name'] or f'話者{r["idx"]+1}') for r in participants}

    txs = conn.execute(
        "SELECT * FROM ai_transcripts WHERE project_id=? AND status='completed' ORDER BY run_number,id",
        (project_id,)
    ).fetchall()
    # Zoom MTG の場合、recordings.zoom_participant_name で話者を確定する
    if source == 'zoom':
        recs_for_offset = conn.execute(
            'SELECT id,seq,duration,zoom_participant_name FROM recordings WHERE project_id=? ORDER BY seq',
            (project_id,)
        ).fetchall()
    else:
        recs_for_offset = conn.execute(
            'SELECT id,seq,duration FROM recordings WHERE project_id=? ORDER BY seq', (project_id,)
        ).fetchall()
    conn.close()

    # Zoom 用：recording_id → 話者ラベル（既に確定済み）
    zoom_speaker_by_rec_id: dict[int, str] = {}
    if source == 'zoom':
        for r in recs_for_offset:
            try:
                zoom_speaker_by_rec_id[r['id']] = r['zoom_participant_name'] or ''
            except (KeyError, IndexError):
                pass

    # recording_id → 録音開始からの累積オフセット(ms)
    offset_by_rec_id = {}
    _cum = 0
    for r in recs_for_offset:
        offset_by_rec_id[r['id']] = _cum
        try:
            _cum += int(float(r['duration'] or 0) * 1000)
        except Exception:
            pass

    # ── 重み正規化（合計10）───────────────────────
    w_ws   = max(0, int(w_ws))
    w_run1 = max(0, int(w_run1))
    w_run2 = max(0, int(w_run2))
    wsum = w_ws + w_run1 + w_run2
    if wsum == 0:
        w_ws, w_run1, w_run2, wsum = 2, 4, 4, 10
    if wsum != 10:
        w_ws   = round(w_ws   * 10 / wsum)
        w_run1 = round(w_run1 * 10 / wsum)
        w_run2 = 10 - w_ws - w_run1

    plist = ', '.join(f'{r["name"]}（{r["idx"]}）' for r in participants) or '（参加者登録なし）'

    # ── 各ソースをタイムスタンプ付き発言リストに揃える ──
    def _ms_to_ts(ms: int) -> str:
        sec = max(0, int(ms / 1000))
        return f'{sec // 60:02d}:{sec % 60:02d}'

    def _ts_to_ms(ts: str) -> int:
        try:
            mm, ss = ts.split(':')
            return (int(mm) * 60 + int(ss)) * 1000
        except Exception:
            return 0

    ws_items = [
        {'ms': _ts_to_ms(r['ts']), 'ts': r['ts'], 'text': (r['text'] or '').strip(),
         'speaker_idx': r['speaker_idx']}
        for r in ref_segs if (r['text'] or '').strip()
    ]
    run_items = {1: [], 2: []}
    run_engines = {1: 'ASR', 2: 'ASR'}
    run_speaker_maps = {1: {}, 2: {}}
    for tx in txs:
        rn = tx['run_number']
        eng = (tx['engine'] or '').lower() if 'engine' in tx.keys() else ''
        if not eng and tx['aai_id']:
            eng = str(tx['aai_id']).split(':', 1)[0]
        run_engines[rn] = {'whisper': 'Whisper', 'deepgram': 'Deepgram'}.get(eng, eng or 'ASR')
        run_speaker_maps[rn] = json.loads(tx['speaker_map_json']) if tx['speaker_map_json'] else {}
        utts = json.loads(tx['utterances_json']) if tx['utterances_json'] else []
        base_offset = offset_by_rec_id.get(tx['recording_id'], 0)
        for u in utts:
            text = (u.get('text') or '').strip()
            if not text:
                continue
            abs_ms = int(u.get('start') or 0) + base_offset
            spk = u.get('speaker', 'A')
            # Zoom MTG では話者ラベルが recording_id で確定（音源分離済み）
            if source == 'zoom':
                spk = zoom_speaker_by_rec_id.get(tx['recording_id'], spk) or spk
            run_items[rn].append({
                'ms': abs_ms,
                'ts': _ms_to_ts(abs_ms),
                'text': text,
                'speaker': spk,
            })

    # ── 骨組み（=出力の時間軸）：base 指定優先、無ければ重み最大 ──
    base_key = (base or '').strip().lower()
    if base_key in ('ws', 'run1', 'run2'):
        _src_map = {'ws': ws_items, 'run1': run_items[1], 'run2': run_items[2]}
        if not _src_map[base_key]:
            raise HTTPException(400, f'基準ソース {base_key} にデータがありません。別のソースを選んでください。')
        backbone_key = base_key
    else:
        src_pool = []
        if w_ws   > 0 and ws_items:     src_pool.append(('ws',   w_ws,   len(ws_items),     0))
        if w_run1 > 0 and run_items[1]: src_pool.append(('run1', w_run1, len(run_items[1]), 1))
        if w_run2 > 0 and run_items[2]: src_pool.append(('run2', w_run2, len(run_items[2]), 2))
        if not src_pool:
            raise HTTPException(400, '統合可能なソースがありません。重みを見直してください。')
        src_pool.sort(key=lambda x: (-x[1], -x[2], -x[3]))
        backbone_key = src_pool[0][0]

    backbone_items = (
        ws_items if backbone_key == 'ws'
        else run_items[1] if backbone_key == 'run1'
        else run_items[2]
    )

    # ── 話者決定（優先度チェーン）─────────────────
    sp_events_sorted = sorted(speaker_events, key=lambda e: e['ms'])

    def resolve_speaker(ms: int) -> tuple[str, bool]:
        """returns (speaker_name, is_uncertain)"""
        # 1) ±2秒以内の押下イベント
        near = [e for e in sp_events_sorted if abs(e['ms'] - ms) <= 2000]
        if near:
            best = min(near, key=lambda e: abs(e['ms'] - ms))
            return (best['speaker_name'] or '未設定', False)
        # 2) 直前押下の継承
        prior = [e for e in sp_events_sorted if e['ms'] <= ms]
        if prior:
            return (prior[-1]['speaker_name'] or '未設定', False)
        # 3) WS ref_segments の speaker_idx を参照
        if ws_items:
            nearest_ws = min(ws_items, key=lambda x: abs(x['ms'] - ms))
            if abs(nearest_ws['ms'] - ms) <= 4000 and nearest_ws['speaker_idx'] is not None:
                return (name_map.get(nearest_ws['speaker_idx'], '未設定'), False)
        # 4) どれも該当なし
        return ('未設定', True)

    # ── クラスタ構築（骨組みの各セグメント = 1行）─
    def nearest_within(items, ms, win=2000):
        if not items: return None
        best = min(items, key=lambda x: abs(x['ms'] - ms))
        return best if abs(best['ms'] - ms) <= win else None

    rows = []
    for i, base in enumerate(backbone_items):
        ms = base['ms']
        sp_name, sp_uncertain = resolve_speaker(ms)
        cand_ws   = base if backbone_key == 'ws'   else nearest_within(ws_items,   ms)
        cand_run1 = base if backbone_key == 'run1' else nearest_within(run_items[1], ms)
        cand_run2 = base if backbone_key == 'run2' else nearest_within(run_items[2], ms)
        rows.append({
            'idx': i + 1,
            'ts': _ms_to_ts(ms),
            'speaker': sp_name,
            'sp_uncertain': sp_uncertain,
            'cand_ws':   (cand_ws['text']   if cand_ws   else ''),
            'cand_run1': (cand_run1['text'] if cand_run1 else ''),
            'cand_run2': (cand_run2['text'] if cand_run2 else ''),
        })

    # ── 入力テーブル（LLMに渡す）────────────────
    src_label = {
        'ws':   'WebSpeech',
        'run1': f'Run1({run_engines[1]})',
        'run2': f'Run2({run_engines[2]})',
    }
    table_lines = []
    for row in rows:
        parts = [
            f'WS={"" if w_ws==0 else (row["cand_ws"] or "（なし）")}',
            f'Run1={"" if w_run1==0 else (row["cand_run1"] or "（なし）")}',
            f'Run2={"" if w_run2==0 else (row["cand_run2"] or "（なし）")}',
        ]
        table_lines.append(
            f'行{row["idx"]:03d} [{row["ts"]}] [話者={row["speaker"]}] '
            + ' / '.join(parts)
        )
    table_block = '\n'.join(table_lines) or '（データなし）'

    backbone_label = src_label[backbone_key]

    base_specified = base_key in ('ws', 'run1', 'run2')
    system_prompt = (
        'あなたは日本語音声書き起こしの統合エディタです。'
        '**基準ソース**のテキストとセグメント分割を最優先で尊重し、'
        '他のソースは**補完情報**としてのみ参照してください。'
        '行の追加・削除・マージ・分割・並べ替えは絶対に行ってはいけません。'
        '発話の主体・順序は基準ソースに従い、補完で文章の流れを変えてはいけません。'
    )

    user_prompt = f"""## 参加者
{plist}

## 基準ソース（時間軸・行構成の主軸）
{backbone_label}{'（ユーザー指定）' if base_specified else '（自動選定）'}

## 補完優先度（基準以外の2ソースをどれだけ採用するか。0 は無視）
WebSpeech : Run1({run_engines[1]}) : Run2({run_engines[2]}) = {w_ws} : {w_run1} : {w_run2}

## 必須ルール
1. 出力は入力テーブルと **同じ行数・同じts・同じspeaker**（変更禁止）
2. 各行の `text` は **基準ソースのテキストを最優先で採用**
3. 基準ソースが空・極端に短い・明らかな誤認識の場合のみ、補完優先度の高い他ソースで置換または合成可
4. 補完優先度 0 のソースは無視（候補から除外）
5. すべての候補が空または「（なし）」なら、textは空文字 ""
6. 単語間の不自然なスペース・全角スペースを除去
7. 句読点（。、）を自然に補う
8. 基準と補完で内容が大きく食い違う箇所は `text` 末尾に「【要確認:理由】」を付記（基準のテキストは残す）
9. 話者ラベルが「未設定」の行は `text` 末尾に「【要確認:話者】」を付記
10. 出力はJSONのみ（説明文・コードフェンス・余計な空行不要）

## 入力テーブル（{len(rows)}行）
{table_block}

## 出力形式
[
  {{"ts": "MM:SS", "speaker": "参加者名", "text": "...", "note": null または "要確認の理由"}},
  ...
]
※ 出力は必ず {len(rows)} 要素の配列にすること。"""

    if model not in ALLOWED_MODELS:
        raise HTTPException(400, f'model must be one of: {list(ALLOWED_MODELS.keys())}')

    logger.info(f'merge start project={project_id} model={model} '
                f'weights ws={w_ws} run1={w_run1} run2={w_run2} '
                f'backbone={backbone_key} rows={len(rows)} '
                f'ref_segs={len(ref_segs)} txs={len(txs)}')

    # ── Gemini API呼び出し（OpenAI互換エンドポイント）──
    llm_payload = {
        'model': model,
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user',   'content': user_prompt},
        ],
        'max_tokens': 16384,
    }

    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(
            f'{GEMINI_BASE}/chat/completions',
            headers={
                'Authorization': f'Bearer {GEMINI_KEY}',
                'content-type': 'application/json',
            },
            json=llm_payload,
        )
        logger.info(f'Gemini response status={r.status_code}')
        if r.status_code != 200:
            logger.error(f'Gemini error: {r.text[:500]}')
            raise HTTPException(502, f'Gemini API failed: {r.text}')
        llm_resp = r.json()

    raw = llm_resp['choices'][0]['message']['content'].strip()

    # JSONブロック抽出（フェンスあり/なし/閉じ無し すべて対応）
    fence_match = re.search(r'```(?:json)?\s*(.*?)(?:```|\Z)', raw, re.DOTALL)
    if fence_match:
        raw = fence_match.group(1).strip()

    # 末尾を [ で始まり ] で終わる形に整形
    start = raw.find('[')
    if start != -1:
        end = raw.rfind(']')
        raw = raw[start:end+1] if end > start else raw[start:]

    def _try_parse(s):
        try: return json.loads(s)
        except Exception: return None

    result = _try_parse(raw)

    # 切り詰めJSONの自動修復：最後の完全な } までで配列を閉じる
    if result is None and raw.startswith('['):
        last_obj_end = raw.rfind('}')
        if last_obj_end > 0:
            repaired = raw[:last_obj_end + 1] + ']'
            result = _try_parse(repaired)
            if result is not None:
                logger.warning(f'LLM JSON repaired (truncated, salvaged {len(result)} items)')

    if result is None:
        logger.error(f'LLM parse error: raw={raw[:500]}')
        raise HTTPException(502, f'LLM output parse error\nRaw head: {raw[:500]}')

    logger.info(f'LLM parsed OK segments={len(result)}')

    # ── ソーステキスト差分付加 ────────────────────────
    def _ts_ms(ts: str) -> int:
        parts = ts.split(':')
        try:
            return (int(parts[0]) * 60 + int(parts[1])) * 1000
        except Exception:
            return -1

    def _strip_ja(text: str) -> str:
        t = re.sub(r'([^\x00-\x7F])\s+([^\x00-\x7F])', r'\1\2', text or '')
        t = re.sub(r'([^\x00-\x7F])\s+([^\x00-\x7F])', r'\1\2', t)
        return re.sub(r'【要確認[^】]*】', '', t).strip()

    # Web Speech: ts → text
    ws_map = [(r['ts'], r['text']) for r in ref_segs]
    # AAI: (start_ms, run_number, text)
    aai_map = []
    for tx in txs:
        utts = json.loads(tx['utterances_json']) if tx['utterances_json'] else []
        for utt in utts:
            aai_map.append((
                utt.get('start', 0),
                f'Run{tx["run_number"]}',
                _strip_ja(utt.get('text', ''))
            ))

    WINDOW_MS = 8000
    for seg in result:
        seg_ms = _ts_ms(seg.get('ts', '00:00'))
        clean = _strip_ja(seg.get('text', ''))
        sources = {}

        # Web Speech の最近傍セグメントを探す
        best_ws = min(ws_map, key=lambda x: abs(_ts_ms(x[0]) - seg_ms), default=None)
        if best_ws and abs(_ts_ms(best_ws[0]) - seg_ms) < WINDOW_MS:
            ws_text = best_ws[1]
            if _strip_ja(ws_text) != clean:
                sources['Web Speech'] = ws_text

        # AAI の最近傍発言を run ごとに探す
        seen_runs = set()
        for aai_ms, run_key, aai_text in sorted(aai_map, key=lambda x: abs(x[0] - seg_ms)):
            if run_key in seen_runs:
                continue
            if abs(aai_ms - seg_ms) < WINDOW_MS:
                if aai_text != clean:
                    sources[run_key] = aai_text
                seen_runs.add(run_key)
            if len(seen_runs) >= 2:
                break

        if sources:
            seg['sources'] = sources

    # 要確認リスト生成
    notes = [
        {'ts': seg.get('ts'), 'speaker': seg.get('speaker'), 'note': seg.get('note')}
        for seg in result if seg.get('note')
    ]

    conn = get_conn()
    conn.execute(
        'INSERT INTO merged_transcripts(project_id,model,result_json,notes_json,created_at) VALUES(?,?,?,?,?)',
        (project_id, model,
         json.dumps(result, ensure_ascii=False),
         json.dumps(notes, ensure_ascii=False),
         now_iso())
    )
    conn.execute("UPDATE projects SET status='merged' WHERE id=?", (project_id,))
    conn.commit()
    conn.close()

    return {'ok': True, 'segments': len(result), 'notes': len(notes)}


# ── 共通：Gemini呼び出しヘルパー ──────────────────
async def _call_gemini(system_prompt: str, user_prompt: str, model: str = 'gemini-2.5-flash',
                       max_tokens: int = 4096, project_id: str = None) -> str:
    if model not in ALLOWED_MODELS:
        raise HTTPException(400, f'model must be one of: {list(ALLOWED_MODELS.keys())}')
    svc = _service_key(model)
    _check_limit(svc, expected_units=1, expected_unit_type='requests')

    payload = {
        'model': model,
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user',   'content': user_prompt},
        ],
        'max_tokens': max_tokens,
    }
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(
            f'{GEMINI_BASE}/chat/completions',
            headers={
                'Authorization': f'Bearer {GEMINI_KEY}',
                'content-type': 'application/json',
            },
            json=payload,
        )
        if r.status_code != 200:
            logger.error(f'Gemini error: {r.text[:500]}')
            raise HTTPException(502, f'Gemini API failed: {r.text}')
        data = r.json()

    # 使用量記録
    usage = data.get('usage') or {}
    pt = int(usage.get('prompt_tokens')     or 0)
    ct = int(usage.get('completion_tokens') or 0)
    total = pt + ct
    if total > 0:
        lim = USAGE_LIMITS.get(svc, {})
        cost = (pt / 1_000_000) * lim.get('rate_usd_per_1m_in', 0) \
             + (ct / 1_000_000) * lim.get('rate_usd_per_1m_out', 0)
        _record_usage(svc, total, 'tokens', project_id, '/llm', cost)

    return data['choices'][0]['message']['content'].strip()


# ── 要約＋残タスク生成 ──────────────────────────────
@app.post('/api/projects/{project_id}/summarize')
async def summarize_project(project_id: str, model: str = 'gemini-2.5-flash',
                            _: str = Depends(require_admin)):
    conn = get_conn()
    merged = conn.execute(
        'SELECT * FROM merged_transcripts WHERE project_id=? ORDER BY id DESC LIMIT 1',
        (project_id,)
    ).fetchone()
    if not merged:
        conn.close()
        raise HTTPException(404, 'No merged transcript. Run /merge first.')

    result = json.loads(merged['result_json']) if merged['result_json'] else []
    if not result:
        conn.close()
        raise HTTPException(400, 'Empty merged transcript')

    # トランスクリプトを単純テキストに整形
    lines = [f'{s.get("ts","")} [{s.get("speaker","")}] {s.get("text","")}' for s in result]
    transcript_text = '\n'.join(lines)

    system_prompt = (
        'あなたは日本語会議分析の専門家です。'
        '与えられた文字起こしから、要約と残タスクを抽出してください。'
    )
    user_prompt = f"""## 会議の文字起こし
{transcript_text}

## 出力指示
以下のJSON形式のみで出力してください（説明文・コードフェンス不要）:
{{
  "summary": "200〜400字程度の会議要約（決定事項・議論の流れを含む）",
  "tasks": [
    "残タスクを1つずつ箇条書きで。担当者がいれば「[名前] 〇〇する」形式",
    "ない場合は空配列 []"
  ]
}}"""

    logger.info(f'summarize start project={project_id} model={model}')
    raw = await _call_gemini(system_prompt, user_prompt, model, max_tokens=2048)

    # JSON抽出（オブジェクト形式）
    fence_match = re.search(r'```(?:json)?\s*(.*?)(?:```|\Z)', raw, re.DOTALL)
    if fence_match:
        raw = fence_match.group(1).strip()
    start = raw.find('{')
    end   = raw.rfind('}')
    if start != -1 and end > start:
        raw = raw[start:end+1]

    try:
        data = json.loads(raw)
    except Exception as e:
        logger.error(f'summarize parse error: {e} raw={raw[:300]}')
        conn.close()
        raise HTTPException(502, f'Summary parse error\nRaw head: {raw[:500]}')

    summary = data.get('summary', '').strip()
    tasks   = data.get('tasks', []) or []

    conn.execute(
        'UPDATE merged_transcripts SET summary=?, tasks_json=? WHERE id=?',
        (summary, json.dumps(tasks, ensure_ascii=False), merged['id'])
    )
    conn.commit()
    conn.close()
    logger.info(f'summarize OK summary_len={len(summary)} tasks={len(tasks)}')
    return {'ok': True, 'summary': summary, 'tasks': tasks}


# ── Q&A（文字起こしへの質問） ─────────────────────
@app.post('/api/projects/{project_id}/ask')
async def ask_project(project_id: str, request: Request,
                      model: str = 'gemini-2.5-flash',
                      _: str = Depends(require_admin)):
    body = await request.json()
    question = (body.get('question') or '').strip()
    if not question:
        raise HTTPException(400, 'question is required')
    if len(question) > 500:
        raise HTTPException(400, 'question too long (max 500 chars)')

    conn = get_conn()
    merged = conn.execute(
        'SELECT * FROM merged_transcripts WHERE project_id=? ORDER BY id DESC LIMIT 1',
        (project_id,)
    ).fetchone()
    if not merged:
        conn.close()
        raise HTTPException(404, 'No merged transcript. Run /merge first.')

    result = json.loads(merged['result_json']) if merged['result_json'] else []
    summary = merged['summary'] or ''
    lines = [f'{s.get("ts","")} [{s.get("speaker","")}] {s.get("text","")}' for s in result]
    transcript_text = '\n'.join(lines) or '（文字起こしなし）'

    system_prompt = (
        'あなたは会議の議事録から質問に答える専門家です。'
        '提供された文字起こしの内容に基づいてのみ回答してください。'
        '推測ではなく、文字起こしに書かれていることだけを根拠にしてください。'
        '不明な場合は「文字起こしには記載がありません」と正直に答えてください。'
    )
    user_prompt = f"""## 会議の要約
{summary or '（要約未生成）'}

## 文字起こし全文
{transcript_text}

## 質問
{question}

## 回答指示
- 簡潔・自然な日本語で
- 該当する発言があれば「[話者] xxx」と引用する
- 長くても400字以内"""

    logger.info(f'ask start project={project_id} q_len={len(question)}')
    answer = await _call_gemini(system_prompt, user_prompt, model, max_tokens=2048)
    logger.info(f'ask OK answer_len={len(answer)}')

    conn.execute(
        'INSERT INTO qa_history(project_id,question,answer,model,created_at) VALUES(?,?,?,?,?)',
        (project_id, question, answer, model, now_iso())
    )
    conn.commit()
    conn.close()
    return {'ok': True, 'answer': answer}


# ── プロジェクト一覧（管理画面） ────────────────────
@app.get('/api/projects')
async def list_projects(_: str = Depends(require_admin)):
    conn = get_conn()
    rows = conn.execute(
        "SELECT p.id, p.name, p.created_at, p.status, "
        "GROUP_CONCAT(pt.name, ',') AS pnames "
        "FROM projects p "
        "LEFT JOIN participants pt ON pt.project_id=p.id "
        "GROUP BY p.id ORDER BY p.created_at DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── プロジェクト詳細（管理画面） ────────────────────
@app.get('/api/projects/{project_id}')
async def get_project(project_id: str, _: str = Depends(require_admin)):
    conn = get_conn()
    proj = conn.execute('SELECT * FROM projects WHERE id=?', (project_id,)).fetchone()
    if not proj:
        conn.close()
        raise HTTPException(404, 'Project not found')

    participants = [dict(r) for r in conn.execute(
        'SELECT idx,name FROM participants WHERE project_id=? ORDER BY idx', (project_id,)
    ).fetchall()]

    recordings = [dict(r) for r in conn.execute(
        'SELECT id,seq,mime,duration,created_at FROM recordings WHERE project_id=? ORDER BY seq',
        (project_id,)
    ).fetchall()]

    ref_segs = [dict(r) for r in conn.execute(
        'SELECT seq,text,speaker_idx,ts,highlight FROM ref_segments WHERE project_id=? ORDER BY seq',
        (project_id,)
    ).fetchall()]

    txs_raw = conn.execute(
        'SELECT * FROM ai_transcripts WHERE project_id=? ORDER BY run_number,id', (project_id,)
    ).fetchall()
    transcripts = []
    for tx in txs_raw:
        t = dict(tx)
        t['utterances']  = json.loads(t['utterances_json'])  if t['utterances_json']  else []
        t['speaker_map'] = json.loads(t['speaker_map_json']) if t['speaker_map_json'] else {}
        del t['utterances_json'], t['speaker_map_json']
        transcripts.append(t)

    # LLM突合結果（最新1件）
    merged_raw = conn.execute(
        'SELECT * FROM merged_transcripts WHERE project_id=? ORDER BY id DESC LIMIT 1',
        (project_id,)
    ).fetchone()
    merged = None
    if merged_raw:
        merged = dict(merged_raw)
        merged['result'] = json.loads(merged['result_json']) if merged['result_json'] else []
        merged['notes']  = json.loads(merged['notes_json'])  if merged['notes_json']  else []
        merged['tasks']  = json.loads(merged['tasks_json'])  if merged.get('tasks_json') else []
        del merged['result_json'], merged['notes_json']
        if 'tasks_json' in merged: del merged['tasks_json']

    # Q&A履歴
    qa = [dict(r) for r in conn.execute(
        'SELECT id, question, answer, model, created_at FROM qa_history '
        'WHERE project_id=? ORDER BY id ASC', (project_id,)
    ).fetchall()]

    conn.close()
    return {
        **dict(proj),
        'participants': participants,
        'recordings':   recordings,
        'ref_segments': ref_segs,
        'transcripts':  transcripts,
        'merged':       merged,
        'qa':           qa,
    }


# ── MTG名変更 ────────────────────────────────────
@app.patch('/api/projects/{project_id}')
async def update_project(project_id: str, request: Request, _: str = Depends(require_admin)):
    body = await request.json()
    conn = get_conn()
    proj = conn.execute('SELECT id FROM projects WHERE id=?', (project_id,)).fetchone()
    if not proj:
        conn.close()
        raise HTTPException(404, 'Project not found')
    if 'name' in body:
        conn.execute('UPDATE projects SET name=? WHERE id=?', (body['name'].strip(), project_id))
        conn.commit()
        logger.info(f'Project renamed: {project_id} → {body["name"]}')
    conn.close()
    return {'ok': True}


# ── MTG削除 ──────────────────────────────────────
@app.delete('/api/projects/{project_id}')
async def delete_project(project_id: str, _: str = Depends(require_admin)):
    conn = get_conn()
    proj = conn.execute('SELECT id FROM projects WHERE id=?', (project_id,)).fetchone()
    if not proj:
        conn.close()
        raise HTTPException(404, 'Project not found')
    # 音声ファイルをディスクから削除
    recs = conn.execute('SELECT audio_path FROM recordings WHERE project_id=?', (project_id,)).fetchall()
    for rec in recs:
        try:
            os.remove(rec['audio_path'])
        except Exception:
            pass
    # DB レコードを全削除（外部キー順）
    for table in ('qa_history', 'merged_transcripts', 'ai_transcripts', 'ref_segments',
                  'speaker_events', 'participants', 'recordings'):
        conn.execute(f'DELETE FROM {table} WHERE project_id=?', (project_id,))
    conn.execute('DELETE FROM projects WHERE id=?', (project_id,))
    conn.commit()
    conn.close()
    logger.info(f'Project deleted: {project_id}')
    return {'ok': True}


# ── 音声ファイル配信（管理画面） ────────────────────
@app.get('/api/audio/{project_id}/{seq}')
async def get_audio(project_id: str, seq: int, _: str = Depends(require_admin)):
    conn = get_conn()
    rec = conn.execute(
        'SELECT * FROM recordings WHERE project_id=? AND seq=?', (project_id, seq)
    ).fetchone()
    conn.close()
    if not rec or not os.path.exists(rec['audio_path']):
        raise HTTPException(404, 'Audio not found')
    return FileResponse(rec['audio_path'], media_type=rec['mime'])


# ── Zoom 連携：録画ファイル DL ワーカー ─────────────
async def _zoom_download_worker(pending_id: int, payload: dict, dl_token: str):
    """recording_files から participant_audio_files を抽出して個別 DL する。

    話者別音声ファイル（participant_audio）が無い場合は audio_only にフォールバック
    （話者分離なし＝既存モバイル録音と同等のフロー）。
    """
    uuid_     = payload.get('uuid', '')
    safe_uuid = re.sub(r'[^A-Za-z0-9_-]', '_', uuid_)
    out_dir   = os.path.join(ZOOM_AUDIO_DIR, safe_uuid)
    os.makedirs(out_dir, exist_ok=True)

    files = payload.get('recording_files', [])

    # 話者別音声を優先（recording_type=audio_interpretation 等の特殊型も participant_email がある場合は含める）
    p_audio = [f for f in files
               if f.get('recording_type') == 'participant_audio'
               or f.get('participant_email')]
    targets = p_audio if p_audio else [
        f for f in files if f.get('recording_type') == 'audio_only'
    ]

    if not p_audio:
        logger.warning(
            f'zoom dl {uuid_}: no participant_audio (fallback to mixed audio)'
        )

    participants: list[dict] = []
    try:
        for f in targets:
            url = f.get('download_url')
            if not url:
                continue
            raw_name = (
                f.get('participant_email')
                or f.get('file_name')
                or f.get('id', 'unknown')
            )
            # ファイル名安全化（英数・日本語・記号一部のみ許可）
            safe_name = re.sub(r'[^\w\-.@ぁ-んァ-ヴー一-龥]', '_', raw_name)
            ext = (f.get('file_extension') or 'm4a').lower()
            dest = os.path.join(out_dir, f'{safe_name}.{ext}')

            await zoom_client.download_file(url, dest, token=dl_token)
            participants.append({
                'name': safe_name,
                'audio_path': dest,
                'file_size': int(f.get('file_size', 0)),
            })

        conn = get_conn()
        conn.execute("""
            UPDATE zoom_pending
               SET participants_json=?, downloaded_at=?, status='ready'
             WHERE id=?
        """, (
            json.dumps(participants, ensure_ascii=False),
            datetime.now(timezone.utc).isoformat(),
            pending_id,
        ))
        conn.commit()
        conn.close()
        logger.info(f'zoom dl {uuid_}: {len(participants)} files done')

    except Exception as e:
        logger.exception(f'zoom dl {uuid_} failed: {e}')
        conn = get_conn()
        conn.execute(
            "UPDATE zoom_pending SET status='error', error_message=? WHERE id=?",
            (str(e), pending_id),
        )
        conn.commit()
        conn.close()


# ── Zoom 連携：Webhook 受信 ─────────────────────────
@app.post('/api/zoom/webhook')
async def zoom_webhook(request: Request):
    """Zoom Webhook 受信エンドポイント。

    - `endpoint.url_validation`: 初期検証チャレンジに即時応答
    - `recording.completed`: 署名検証 → zoom_pending 登録 → 非同期DL起動
    - その他イベント: 無視
    """
    body = await request.body()
    ts   = request.headers.get('x-zm-request-timestamp', '')
    sig  = request.headers.get('x-zm-signature', '')

    try:
        data = json.loads(body)
    except Exception:
        raise HTTPException(400, 'invalid json')

    # 1) URL Validation Challenge（署名検証より先に処理）
    if data.get('event') == 'endpoint.url_validation':
        plain = data.get('payload', {}).get('plainToken', '')
        return build_url_validation_response(plain, ZOOM_WEBHOOK_SECRET)

    # 2) 通常イベントの署名検証
    if not verify_signature(body, ts, sig, ZOOM_WEBHOOK_SECRET):
        logger.warning(f'zoom webhook: invalid signature ts={ts}')
        raise HTTPException(401, 'invalid signature')

    # 3) recording.completed 以外は無視
    if data.get('event') != 'recording.completed':
        logger.info(f"zoom webhook: ignored event={data.get('event')}")
        return {'status': 'ignored'}

    payload  = data.get('payload', {}).get('object', {})
    # download_token は payload 直下 or ペイロード内（Zoom API バージョンで揺れる）
    dl_token = data.get('download_token') \
               or data.get('payload', {}).get('download_token', '')

    uuid_ = payload.get('uuid', '')
    conn = get_conn()
    try:
        cur = conn.execute("""
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
            datetime.now(timezone.utc).isoformat(),
        ))
        conn.commit()
        pending_id = cur.lastrowid
    except Exception as e:
        # UNIQUE 違反（同じ MTG の重複 Webhook）は握りつぶす
        logger.info(f'zoom webhook: duplicate uuid {uuid_} ({e})')
        conn.close()
        return {'status': 'duplicate'}
    conn.close()

    if zoom_client is None:
        logger.error('zoom webhook: ZOOM_* env vars not configured')
        raise HTTPException(503, 'zoom integration not configured')

    # バックグラウンドでDL開始
    asyncio.create_task(_zoom_download_worker(pending_id, payload, dl_token))
    return {'status': 'accepted', 'pending_id': pending_id}


# ── Zoom 連携：取り込み待ち一覧 ─────────────────────
@app.get('/api/zoom/pending')
async def zoom_pending_list(_: str = Depends(require_admin)):
    """取り込み待ち（pending_download / ready / error）の Zoom MTG を返す"""
    conn = get_conn()
    rows = conn.execute("""
        SELECT id, zoom_meeting_id, zoom_uuid, topic, host_email,
               start_time, duration, participants_json, status,
               error_message, created_at, imported_project_id
          FROM zoom_pending
         WHERE status IN ('pending_download','ready','error')
         ORDER BY created_at DESC
    """).fetchall()
    conn.close()

    out = []
    for r in rows:
        d = dict(r)
        try:
            d['participants'] = json.loads(d.pop('participants_json') or '[]')
        except Exception:
            d['participants'] = []
        out.append(d)
    return {'pending': out}


# ── Zoom 連携：取り込み実行（projects 作成・音声ファイル配置） ─────
@app.post('/api/zoom/pending/{pending_id}/import')
async def zoom_import(pending_id: int, _: str = Depends(require_admin)):
    """zoom_pending → projects/recordings/participants 化する。

    STT 実行は本 API では行わない。管理画面の既存 Run 設定モーダルから
    POST /api/projects/{id}/analyze を呼ぶ運用とする（疎結合維持）。
    """
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM zoom_pending WHERE id=? AND status='ready'",
        (pending_id,),
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, 'pending not found or not ready')

    topic        = row['topic'] or 'Zoom MTG'
    start_time   = row['start_time'] or datetime.now(timezone.utc).isoformat()
    project_id   = uuid.uuid4().hex
    project_name = f'{topic} ({start_time[:16]})'
    proj_dir     = f'/var/jizo/audio/{project_id}'
    os.makedirs(proj_dir, exist_ok=True)

    conn.execute(
        '''INSERT INTO projects(id, name, created_at, status, source)
           VALUES(?, ?, ?, ?, 'zoom')''',
        (project_id, project_name,
         datetime.now(timezone.utc).isoformat(), 'uploaded'),
    )

    participants = json.loads(row['participants_json'] or '[]')
    rec_ids: list[int] = []
    for idx, p in enumerate(participants, start=1):
        src = p['audio_path']
        ext = os.path.splitext(src)[1] or '.m4a'
        dst = os.path.join(proj_dir, f'{idx:02d}_{os.path.basename(src)}')
        try:
            os.rename(src, dst)
        except OSError:
            import shutil
            shutil.copy2(src, dst)

        mime = 'audio/m4a' if ext.lower() in ('.m4a', '.mp4') else 'audio/wav'
        cur = conn.execute(
            '''INSERT INTO recordings
               (project_id, seq, audio_path, mime, duration, created_at,
                zoom_participant_name)
               VALUES(?,?,?,?,?,?,?)''',
            (project_id, idx, dst, mime, '',
             datetime.now(timezone.utc).isoformat(),
             p.get('name', '')),
        )
        rec_ids.append(cur.lastrowid)
        conn.execute(
            'INSERT INTO participants(project_id, idx, name) VALUES(?,?,?)',
            (project_id, idx, p.get('name', '')),
        )

    conn.execute(
        '''UPDATE zoom_pending
              SET status='imported', imported_project_id=?
            WHERE id=?''',
        (project_id, pending_id),
    )
    conn.commit()
    conn.close()

    logger.info(f'zoom import: project={project_id} recordings={len(rec_ids)}')
    return {
        'project_id': project_id,
        'recordings': len(rec_ids),
        'name': project_name,
    }


# ── Zoom 連携：取り込み待ち破棄 ─────────────────────
@app.delete('/api/zoom/pending/{pending_id}')
async def zoom_pending_delete(pending_id: int,
                              _: str = Depends(require_admin)):
    """取り込みせずに破棄。ダウンロード済み音声ファイルも削除する"""
    conn = get_conn()
    row = conn.execute(
        'SELECT zoom_uuid, participants_json FROM zoom_pending WHERE id=?',
        (pending_id,),
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, 'not found')

    try:
        for p in json.loads(row['participants_json'] or '[]'):
            try:
                os.remove(p['audio_path'])
            except OSError:
                pass
        safe_uuid = re.sub(r'[^A-Za-z0-9_-]', '_', row['zoom_uuid'])
        d = os.path.join(ZOOM_AUDIO_DIR, safe_uuid)
        if os.path.isdir(d) and not os.listdir(d):
            os.rmdir(d)
    finally:
        conn.execute('DELETE FROM zoom_pending WHERE id=?', (pending_id,))
        conn.commit()
        conn.close()

    logger.info(f'zoom pending deleted: id={pending_id}')
    return {'deleted': pending_id}


# ── Zoom 連携：手動アップロード（他組織主催 MTG 用） ──
@app.post('/api/zoom/upload')
async def zoom_upload(request: Request,
                      _: str = Depends(require_admin)):
    """multipart/form-data:
       - files[]   : 音声ファイル（.m4a / .mp4 / .wav）
       - meta      : JSON 文字列 {topic, participants:[{filename, name}]}
    """
    form = await request.form()
    try:
        meta = json.loads(form.get('meta', '{}'))
    except Exception:
        raise HTTPException(400, 'invalid meta json')
    files = form.getlist('files')
    if not files:
        raise HTTPException(400, 'no files uploaded')

    project_id = uuid.uuid4().hex
    proj_dir   = f'/var/jizo/audio/{project_id}'
    os.makedirs(proj_dir, exist_ok=True)

    # ファイル名 → 表示話者名のマッピング
    name_map = {p['filename']: p['name']
                for p in meta.get('participants', []) if 'filename' in p}

    topic = meta.get('topic', 'Zoom upload')

    conn = get_conn()
    conn.execute(
        '''INSERT INTO projects(id, name, created_at, status, source)
           VALUES(?, ?, ?, ?, 'upload')''',
        (project_id, topic,
         datetime.now(timezone.utc).isoformat(), 'uploaded'),
    )

    for idx, f in enumerate(files, start=1):
        fname = getattr(f, 'filename', f'audio{idx}.m4a')
        dst   = os.path.join(proj_dir, f'{idx:02d}_{fname}')
        content = await f.read()
        with open(dst, 'wb') as out:
            out.write(content)

        speaker = name_map.get(fname) or os.path.splitext(fname)[0]
        mime = getattr(f, 'content_type', None) or 'audio/m4a'

        conn.execute(
            '''INSERT INTO recordings
               (project_id, seq, audio_path, mime, duration, created_at,
                zoom_participant_name)
               VALUES(?,?,?,?,?,?,?)''',
            (project_id, idx, dst, mime, '',
             datetime.now(timezone.utc).isoformat(), speaker),
        )
        conn.execute(
            'INSERT INTO participants(project_id, idx, name) VALUES(?,?,?)',
            (project_id, idx, speaker),
        )

    conn.commit()
    conn.close()

    logger.info(f'zoom upload: project={project_id} files={len(files)}')
    return {'project_id': project_id, 'recordings': len(files), 'name': topic}
