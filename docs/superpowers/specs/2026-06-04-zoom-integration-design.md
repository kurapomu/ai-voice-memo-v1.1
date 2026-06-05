# Zoom連携設計（v1 → v2: Pull型一覧フロー）

作成日: 2026-06-04 / 改訂: 2026-06-05
対象: AI Voice Memo（jizo-dev.com/ai-voice-memo）

---

## 【v2 改訂】 2026-06-05

v1（Webhook自動DL方式）を運用してみたところ、**複数録画が並んだ際の不整合**（重複Webhook・DLタイミング差・Zoom側削除済との乖離）と、**ユーザー意思と無関係に資源消費**する点が問題となった。v2 で「Pull型一覧→1件ずつ取り込み」に再設計した。

### v2 のデータフロー

```
[平時]
Zoom Cloud → recording.completed Webhook
  → /api/zoom/webhook（署名検証のみ）
  → zoom_pending(status='notified') を INSERT するだけ
    ※ ファイル DL は行わない

[ユーザー操作]
管理画面「☁ Zoom録画一覧から取り込み」ボタン
  → GET /api/zoom/cloud-recordings?days=N
      - Zoom API `/v2/users/me/recordings` をライブ取得
      - zoom_pending（notified/imported/error）とマージ（key=zoom_uuid）
      - status='available'（未取得）/ 'notified' / 'imported' / 'error' で表示
  → モーダルに一覧表示。1件ずつ「取り込む」ボタン
  → POST /api/zoom/cloud-recordings/{uuid:path}/import
      - Zoom API `/v2/meetings/{uuid}/recordings` で録画情報を再取得
      - zoom_pending を upsert（status='pending_download'）
      - `_zoom_download_worker(pending_id, payload, dl_token=None)` で OAuth Bearer DL
      - `_zoom_pending_to_project(pending_id)` で projects/recordings/participants 作成
      - status='imported' + imported_project_id をセット
      - 即 projects 一覧に表示
```

### v2 の保証

- **ゼロ自動DL**: Webhook では DL しないため、不要録画でのディスク消費なし
- **整合性**: Zoom API がライブで真実 = Zoom側の削除即時反映、過去録画も遡及表示可
- **1ステップ取り込み**: 「取り込む」押下 → DL → projects 化が1回のAPI呼び出しで完結
- **多重 import 防止**: `zoom_uuid UNIQUE` + status チェックで再取り込みを拒否

### v2 の API（新規）

| メソッド | パス | 認証 | 用途 |
|---|---|---|---|
| GET | `/api/zoom/cloud-recordings?days=N` | Basic | Zoom API + zoom_pending マージ一覧（過去N日） |
| POST | `/api/zoom/cloud-recordings/{uuid:path}/import` | Basic | UUIDの録画を DL → projects 化（1ステップ） |

v1 の `/api/zoom/pending`, `/api/zoom/pending/{id}/import`, `/api/zoom/pending/{id}` は**互換のため残存**（旧テストデータ向け）。

### v2 の Webhook 動作

```python
@app.post('/api/zoom/webhook')
async def zoom_webhook(request: Request):
    # 署名検証 / URL Validation Challenge はそのまま
    # ↓ v1 と異なる点
    # recording.completed の場合:
    #   zoom_pending(status='notified') を INSERT のみ
    #   _zoom_download_worker は呼ばない
    return {'status': 'notified'}
```

Marketplace 側の Event Subscription は**残して可**（来ても DB に記録するだけで害なし。ユーザーへの「新着あり」気付きトリガーとして機能）。

### v2 の管理画面 UI

サイドバー上部に2ボタン構成:
- **「☁ Zoom録画一覧から取り込み」（青）** — v2 新フロー（自アカウント主催）
- **「他組織MTGをファイルアップロード」（グレー）** — 既存（外部主催）

モーダル「Zoom Cloud 録画一覧」:
- 期間選択（7日/30日/90日/180日、デフォルト30日）
- 各行: トピック / 開始時刻 / 時間 / ファイル数 / 話者別音声有無 / ステータスバッジ / アクション
- ステータスバッジ色:
  - `available`: グレー「未取得」
  - `notified`: 青「Webhook通知済」
  - `imported`: 緑「取り込み済」+「プロジェクトを開く」
  - `error`: 赤「エラー」+「再試行」

### v2 の制約（v1から継承）

- **自アカウント主催の録画のみ取得可**（Server-to-Server OAuth の仕様上、他組織主催の録画は API で取れない）
- 外部主催 MTG は「他組織MTGをファイルアップロード」モーダルで対応
- ホストの Zoom アカウント設定で「Record a separate audio file for each participant」が ON でないと話者別音声が生成されない（1人テストでは participant_audio が空になる）

### v2 の Zoom Marketplace 必須 Scope（実機検証済）

```
recording:read:admin                                ← 基本
cloud_recording:read:list_user_recordings:admin     ← 必須（v1には記載なし、運用で判明）
cloud_recording:read:recording:admin                ← 必須
meeting:read:meeting:admin                          ← 推奨
user:read:user:admin                                ← 推奨
```

`:master` 系 Scope は不要（Master Account 契約していない単一組織運用のため）。

---

## 【v1 原典】（履歴・参考用に保持）

## 目的

オンラインMTG（Zoom）の音声を本システムに取り込み、参加者ごとに**完全な話者分離**を実現する。
既存のモバイル録音フロー（録音→アップロード→Whisper/Deepgram→Gemini統合）を踏襲し、
Zoom MTGも同じ管理画面で確認できるようにする。

## スコープ

### v1 で実装
- Zoom Cloud Recording API 経由の**録画後取り込み**（非リアルタイム）
- **Server-to-Server OAuth**（単一Zoomアカウント前提）
- **話者別音声トラック**（Separate Audio Files）で完璧な話者分離
- **半自動取り込み**（Webhook自動検知＋管理画面で手動承認）
- 他組織主催MTG用の**手動アップロード**機能

### v1 で実装しない（YAGNI）
- リアルタイムストリーミング（RTMS）
- マルチテナント（User-level OAuth）— 拡張ポイントだけ確保
- VTT字幕の併用
- Zoom以外のWeb会議ツール（Teams/Meet等）

## 前提条件

- ZoomアカウントがPro以上（Cloud Recording機能必須）
- アカウント設定で「Record a separate audio file for each participant」がON
- MTG中に「クラウドに録画」が押される（または自動録画ON）
- 他組織主催のMTGは自動取り込み対象外（手動アップロードで対応）

## アーキテクチャ

### データフロー

```
[Zoom MTG]
  ├─ MTG終了
  └─ Zoom側で録画処理（数分〜数十分）
       └─ recording.completed Webhook
            ↓
[VPS: /api/zoom/webhook]
  ├─ 署名検証（HMAC-SHA256）
  ├─ URL Validation Challenge 応答
  ├─ ペイロードからparticipant別 .m4a のDLリンク抽出
  ├─ 全ファイルを /var/jizo/audio/zoom/{meetingId}/ にDL
  └─ zoom_pending テーブルに登録（status=ready）
       ↓
[管理画面]
  ├─ 「Zoom取り込み待ち N件」バッジ
  └─ ユーザー操作：[取り込む] クリック
       ↓
[VPS: /api/zoom/pending/{id}/import]
  ├─ projects 新規作成（source='zoom', name='{topic} {date}'）
  ├─ 参加者ごとに recordings レコード作成
  │   （zoom_participant_name に表示名を保存）
  ├─ participants テーブルにZoom表示名を投入
  └─ Run設定に従い各recordingをWhisper/Deepgramに投入
       ↓
[Gemini統合]
  └─ 話者は確定済み → タイムスタンプでマージ
      Run1/Run2のテキストのみ重み付け統合
```

### 既存設計との対応

| 既存（モバイル） | Zoom版での対応 |
|---|---|
| Web Speech＝話者確定（人手押下） | Zoom Separate Audio＝話者確定（音源分離・100%） |
| MediaRecorder 1ファイル | 参加者N人＝Nファイル（rec_id を N個発行） |
| Whisper Run1/Run2 | 各話者ファイルを個別にWhisper処理 |
| Gemini重み付け統合（w_ws/w_run1/w_run2） | 話者既知のため w_ws相当は固定、Run1/Run2のみ重み付け |
| participants テーブル | Zoom表示名を自動投入 |
| `POST /api/projects`（モバイル） | `POST /api/zoom/pending/{id}/import`（Zoom）|

## データモデル変更

### 新規テーブル

```sql
CREATE TABLE zoom_pending (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  zoom_meeting_id TEXT NOT NULL,
  zoom_uuid TEXT UNIQUE NOT NULL,        -- ZoomのMTG一意ID
  topic TEXT,                            -- MTG名
  host_email TEXT,
  start_time TEXT,                       -- ISO8601
  duration INTEGER,                      -- 秒
  participants_json TEXT,                -- [{name, email, audio_path, duration}]
  download_token TEXT,                   -- Zoom DL用（短期トークン）
  downloaded_at TEXT,                    -- 全DL完了時刻
  status TEXT NOT NULL,                  -- pending_download | ready | imported | error
  error_message TEXT,
  created_at TEXT NOT NULL,
  imported_project_id TEXT               -- 取り込み後のproject ID
);
```

### 既存テーブルへの追加カラム

```sql
ALTER TABLE recordings ADD COLUMN zoom_participant_name TEXT;
ALTER TABLE projects ADD COLUMN source TEXT NOT NULL DEFAULT 'mobile';
-- source: 'mobile' | 'zoom' | 'upload'
```

`ai_transcripts` テーブルは既存のまま（`recording_id` で話者別に紐づく）。

## API エンドポイント（新規）

| メソッド | パス | 認証 | 用途 |
|---|---|---|---|
| POST | `/api/zoom/webhook` | Zoom署名検証 | Webhook受信（recording.completed・URL検証Challenge）|
| GET | `/api/zoom/pending` | Basic | 取り込み待ちリスト取得 |
| POST | `/api/zoom/pending/{id}/import` | Basic | projects化＋STT起動（Run設定をbodyで受領）|
| DELETE | `/api/zoom/pending/{id}` | Basic | 取り込み待ち破棄（音声ファイルも削除）|
| POST | `/api/zoom/upload` | Basic | 他組織MTG用手動アップロード（複数ファイル）|

### Webhook ペイロード処理

Zoomから飛んでくる `recording.completed` の主要フィールド：
- `payload.object.uuid` — MTG UUID
- `payload.object.topic` — MTG名
- `payload.object.host_email`
- `payload.object.start_time` / `duration`
- `payload.object.recording_files[]` — 各ファイル
  - `recording_type`: `audio_only` / `audio_transcript` / `shared_screen_with_speaker_view` 等
  - `participant_audio_files`: 話者別音声（v1ではこれを優先利用）
  - `download_url` + `download_token`

**v1の取得対象**: `participant_audio_files`（または `recording_type=audio_only` の各participantエントリ）

## セキュリティ

### Zoom Webhook 署名検証
```
署名ヘッダ: x-zm-signature: v0=<HMAC-SHA256>
タイムスタンプ: x-zm-request-timestamp: <unix秒>

message = f"v0:{timestamp}:{request_body}"
expected = "v0=" + hmac.new(ZOOM_WEBHOOK_SECRET, message, sha256).hexdigest()
verify: expected == x-zm-signature
```

### URL Validation Challenge（Zoom Marketplace初期検証）
Webhook URL登録時にZoomから飛んでくる `event=endpoint.url_validation` に対し、
`plainToken` を HMAC-SHA256 で `encryptedToken` 化して即時返却。

### DLトークン管理
`download_token` は数時間で期限切れ → Webhook着弾後**即座に**全ファイルDLする。
DL中はワーカープロセス（asyncioのbackground task）で実行、Webhookレスポンスは即200返却。

## 環境変数（VPS `/opt/jizo-api/.env` 追加）

```
ZOOM_CLIENT_ID=...
ZOOM_CLIENT_SECRET=...
ZOOM_ACCOUNT_ID=...
ZOOM_WEBHOOK_SECRET=...
```

## ストレージ

```
/var/jizo/audio/
  ├─ {project_id}/         # 既存（モバイル録音）
  └─ zoom/
       └─ {zoom_meeting_id}/
            ├─ 田中太郎.m4a
            ├─ 鈴木花子.m4a
            └─ 山田次郎.m4a
```

取り込み実行時：これらのファイルを `/var/jizo/audio/{project_id}/` に移動 or シンボリックリンク。

## 管理画面UI

### Zoom取り込み待ちセクション（プロジェクト一覧の上部）
- バッジ：「Zoom取り込み待ち {N}件」
- 各行：MTG名 / 開始時刻 / 参加者数 / [取り込む] [破棄]
- [取り込む] クリック → 既存のRun設定モーダル（Whisper/Deepgramモデル選択）→ 実行

### 手動アップロード機能
- 「Zoom録画ファイルからインポート」ボタン
- 複数ファイルD&D → ファイル名から話者名を抽出（Zoom命名規則: `audio{N}.m4a` または参加者名）
- MTG名・話者名を確認モーダルで編集可能 → import実行

### プロジェクト詳細画面
- 既存の4カラム比較ビューを再利用
- 「ソース」バッジ表示（mobile / zoom / upload）
- Zoomの場合は WS列の代わりに「Zoom話者ラベル列」として表示（中身は同じ「話者確定情報」）

## エラーハンドリング

| ケース | 挙動 |
|---|---|
| 署名検証失敗 | 401返却・ログ記録 |
| DL失敗（トークン期限切れ等） | `status=error`・管理画面に表示・手動リトライボタン |
| Zoom APIレート制限 | 指数バックオフで最大3回リトライ |
| 録画ファイル0件（録画なしMTG） | Webhook即無視（zoom_pendingに登録しない） |
| participant_audio_files が空 | アカウント設定未ONの可能性 → ログに警告・通常録音にフォールバック |

## テスト戦略

- **Webhook署名検証**: Zoom公式ドキュメントのサンプルペイロードでユニットテスト
- **DLフロー**: モックHTTPサーバーで participant_audio_files の取得 → ファイル配置を検証
- **取り込み統合テスト**: Webhook → DL → import → STT起動までの一連を、Whisper APIモックで実行
- **手動受け入れテスト**: 実際のZoomで2〜3人MTG → 録画 → 取り込み → 話者分離確認

## Gemini統合の調整

既存の `w_ws / w_run1 / w_run2` パラメータの扱い：
- **モバイル録音**: 既存通り（Web Speech + Run1/Run2）
- **Zoom MTG**: 話者は確定済み（Zoom音源分離） → `w_ws` は「話者ラベル固定・テキスト不参加」として動作
  - 骨組み選定はRun1/Run2の重み比で決定（同点時 run2>run1）
  - 各セグメントの話者は Zoom participant 由来で確定
  - LLMタスクは「Run1/Run2の候補からテキストを選ぶ・合成」のみ

実装：`merge_transcripts` 関数に `source` 判定を追加し、Zoomの場合は話者決定ロジックをスキップ。

## デプロイ手順（v1リリース時）

1. **Zoom側**
   - Zoom Marketplace で Server-to-Server OAuth App作成
   - Scope: `recording:read:admin`, `meeting:read:admin`, `user:read:admin`
   - Event Subscription: `recording.completed`
   - Webhook URL: `https://jizo-dev.com/api/zoom/webhook`
   - アカウント設定: Cloud Recording ON, Separate Audio Files ON
2. **VPS側**
   - `.env` に Zoom credentials 追加
   - `db.py` にスキーマ追加 → マイグレーション実行
   - `main.py` に新APIエンドポイント追加
   - `systemctl restart jizo-api`
3. **Nginx**
   - `/api/zoom/webhook` のサイズ上限緩和（10MB程度）
4. **管理画面**
   - Zoom取り込み待ちセクション追加・手動アップロードボタン追加
5. **動作確認**
   - テストMTG（自分1人）で Webhook 着弾確認
   - 2人MTGで話者分離確認

## 将来拡張ポイント

- **User-level OAuth**: 認証層のみ差し替えで多組織対応（DBスキーマは互換）
- **Teams/Meet対応**: `source` カラムに `teams` / `meet` を追加
- **リアルタイム取り込み**: RTMS対応する場合は新規エンドポイント追加（既存フロー非破壊）
- **Webhookの完全自動取り込みモード**: 設定フラグ1つで半自動→完全自動切替

## 制約・既知の問題

- ホストが録画ボタンを押し忘れたMTGは検知不可（Zoom側に録画が存在しないため）
- DL中（数分〜数十分）はディスク容量を一時的に消費（最大数GB／長尺MTG）
- Whisper APIコストが参加者数に比例（10人MTG = 10倍のAPI呼び出し）
- Zoom無料プランでは利用不可（Pro以上必須）
