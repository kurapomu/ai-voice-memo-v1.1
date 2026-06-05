# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## プロジェクト概要

対面録音特化のAIボイスメモPWA。iPhoneのSafariからホーム画面に追加して使用する。
**二段階方式**：モバイルで録音＋Web Speechの話者ボタン（参考データ）→ VPSにアップロード →
Whisper／Deepgram で高精度文字起こし（Run1/Run2 各エンジン選択可）→
Gemini 2.5 Flash で**重み付き統合**（WS:Run1:Run2 = 合計10）→ PC管理画面で確認。

---

## 全体構成（重要）

```
┌──────────────────────────────────────────────────────────────────┐
│ ① モバイルPWA  https://jizo-dev.com/ai-voice-memo/               │
│   ・MediaRecorder（32kbps webm）                                  │
│   ・Web Speech APIで録音中リアルタイム文字起こし＋話者ボタン      │
│   ・MTG名入力 → 録音 → 一時停止/再開 → 「保存して完了」          │
│   ・チャンクあたり20分制限・残り時間表示・20分で自動pause         │
│   ・STT無反応20秒で確認バナー（モーダルではないインライン）       │
│   ・IndexedDBにも保存（ローカル履歴）                             │
└──────────────────┬───────────────────────────────────────────────┘
                   │ POST /api/projects（multipart：音声＋meta JSON）
                   ▼
┌──────────────────────────────────────────────────────────────────┐
│ ② VPS（root@162.43.14.31 / jizo-dev.com / Ubuntu 22.04）          │
│   Nginx（HTTPS / Let's Encrypt）                                  │
│   ├ /ai-voice-memo/       静的PWA                                 │
│   ├ /ai-voice-memo/admin/ PC管理画面（Basic認証）                 │
│   └ /api/                 FastAPI（systemd: jizo-api / :8002）    │
│                                                                   │
│   ストレージ:                                                     │
│   ├ /var/jizo/audio/*.webm   録音音声                            │
│   └ /var/jizo/jizo.db        SQLite                              │
└─────────┬────────────────────────────────────────────────────────┘
          │
          ├──► ③ 文字起こし（Runごとに選択）                       │
          │     ・OpenAI Whisper API（whisper-1）同期・即completed │
          │     ・Deepgram API（nova-3 / nova-2）話者分離あり      │
          │       Nova-3 は keyterm 必須・Nova-2 は keywords        │
          │                                                         │
          └──► ④ Google Gemini API（gemini-2.5-flash / pro）       │
                  OpenAI互換エンドポイント                         │
                  3ソースを重み付け統合：                          │
                    1. 重み最大ソースを「骨組み」→ 行数決定        │
                    2. 各行 ±2秒以内で他2ソースを候補として添える │
                    3. 話者は WS押下イベント優先度チェーンで決定  │
                    4. LLMは「行追加・削除・マージ禁止」で候補から│
                       重みに従って選ぶ／合成                      │
                  【要確認】タグで不確実箇所をマーク               │
```

---

## ファイル構成

### ローカル（このディレクトリ）
- `index.html` — モバイルPWA全体（VanillaJS・約1000行）※**正典：これを編集→VPSへscp**
- `manifest.json` — PWAマニフェスト
- `sw.js` — Service Worker（HTMLはネットワーク優先・他はキャッシュ優先）。現バージョンは `sw.js` 冒頭の `const CACHE = 'voicememo-vX.Y';` を直接確認。**編集時は必ず +0.1 インクリメント**
- `CONCEPT.md` — 人間向け：プロダクト哲学・背景（Claude は通常参照不要）
- `docs/system-overview.html` — 社外説明用 HTML
- `docs/superpowers/plans/` — 中長期実装プラン（`YYYY-MM-DD-トピック.md`）
- `docs/superpowers/specs/` — **設計仕様の正典**（`YYYY-MM-DD-トピック.md`）。CLAUDE.md より詳細な実装ロジックはここを参照
- `_main_remote.py` / `_admin_remote.html` / `_db_remote.py` — **ローカル正典（編集可・コミット対象）**。編集 → VPS の `/opt/jizo-api/main.py` / `/var/www/jizo-dev.com/ai-voice-memo/admin/index.html` / `/opt/jizo-api/db.py` へ scp → 必要に応じ `systemctl restart jizo-api`

### VPS（`root@162.43.14.31`）
- `/var/www/jizo-dev.com/ai-voice-memo/index.html` — モバイルPWA（↑のコピー）
- `/var/www/jizo-dev.com/ai-voice-memo/admin/index.html` — PC管理画面
- `/opt/jizo-api/main.py` — FastAPI 全API
- `/opt/jizo-api/db.py` — SQLiteスキーマ＋接続
- `/opt/jizo-api/.env` — APIキー等（ASSEMBLYAI / OPENAI / DEEPGRAM / GEMINI / ADMIN_USER / ADMIN_PASS）
- `/etc/systemd/system/jizo-api.service` — FastAPI systemd

---

## よく使うコマンド

### デプロイ

作業ディレクトリ：
- Windows: `D:\voice`
- Mac: `~/Desktop/ai-voice-memo-v1-2/ai-voice-memo-v1.1/`

`scp` / `ssh` は Git Bash（Windows）・標準ターミナル（Mac）どちらでも同一コマンドで動作。

```bash
# モバイルアプリ更新（index.html）
scp index.html root@162.43.14.31:/var/www/jizo-dev.com/ai-voice-memo/index.html

# Service Worker更新（sw.js）— 編集時は CACHE 定数を必ずインクリメント
scp sw.js root@162.43.14.31:/var/www/jizo-dev.com/ai-voice-memo/sw.js

# FastAPI更新（_main_remote.py がローカル正典）
scp _main_remote.py root@162.43.14.31:/opt/jizo-api/main.py
ssh root@162.43.14.31 "systemctl restart jizo-api"

# 管理画面更新（_admin_remote.html がローカル正典）
scp _admin_remote.html root@162.43.14.31:/var/www/jizo-dev.com/ai-voice-memo/admin/index.html
```

### ログ確認
```bash
# FastAPIログ（12h ローテーション）
curl -s -u test:test "https://jizo-dev.com/api/logs?lines=200"

# systemdログ
ssh root@162.43.14.31 "journalctl -u jizo-api -n 50 --no-pager"

# Nginx設定
ssh root@162.43.14.31 "cat /etc/nginx/sites-available/jizo-dev.com"
```

### DB確認
```bash
ssh root@162.43.14.31 "python3 -c \"
import sqlite3
c = sqlite3.connect('/var/jizo/jizo.db')
print(c.execute('SELECT id,name,status FROM projects ORDER BY created_at DESC LIMIT 5').fetchall())
\""
```

### ローカルテスト
```bash
python3 -m http.server 5500  # → http://localhost:5500
```

---

## DBスキーマ（VPS SQLite）

```sql
projects(id TEXT PK, name TEXT, created_at TEXT, status TEXT)
  -- status: uploaded | analyzing_run1 | analyzing_run2 | completed_run1 | completed_run2 | merged | error

recordings(id INTEGER PK AUTOINCREMENT, project_id, seq, audio_path, mime, duration, created_at)

ref_segments(id, project_id, seq, text, speaker_idx, ts, highlight)
  -- Web Speech + 話者ボタン参考データ

participants(project_id, idx, name)

ai_transcripts(id, project_id, recording_id, run_number, aai_id, status,
               full_text, utterances_json, speaker_map_json, engine, settings_json, error)
  -- run_number=1 or 2、aai_id は '{engine}:{rec_id}:{run}' 形式（engine=whisper or deepgram）
  -- engine カラムでフィルタリング可能。Deepgram は speaker_map_json に話者分離結果あり

merged_transcripts(id, project_id, model, result_json, notes_json, created_at)
  -- LLM統合結果。result_json = [{ts, speaker, text, note}]
```

---

## API エンドポイント

| メソッド | パス | 認証 | 用途 |
|---|---|---|---|
| GET | `/api/health` | なし | ヘルスチェック |
| GET | `/api/logs?lines=N` | Basic | サーバーログ閲覧 |
| POST | `/api/projects` | なし | モバイルからのアップロード（multipart） |
| GET | `/api/projects` | Basic | プロジェクト一覧（管理画面用） |
| GET | `/api/projects/{id}` | Basic | プロジェクト詳細 |
| PATCH | `/api/projects/{id}` | Basic | MTG名変更（`{name: ...}`） |
| DELETE | `/api/projects/{id}` | Basic | プロジェクト削除（音声＋DB全件） |
| POST | `/api/projects/{id}/analyze?run=1or2&force=true` | なし | Whisper解析実行 |
| GET | `/api/projects/{id}/poll` | なし | 解析状態ポーリング（Whisperはスキップ） |
| POST | `/api/projects/{id}/merge?model=...&w_ws=X&w_run1=Y&w_run2=Z` | Basic | Gemini で重み付き統合（合計10） |
| GET | `/api/audio/{id}/{seq}` | Basic | 音声ファイル配信 |

---

## 設計方針（厳守）

### STT差し替えポイント
`createSTT()` 関数のみを差し替えることでWeb Speech API → Whisper等へ移行可能。
UIロジック・録音処理・IndexedDB保存には**一切手を入れない**。

### 二段階方式（話者分離）
- **リアルタイム層**：Web Speech＋話者ボタン → **話者ラベルが人手で確認済み・最高信頼度**
- **後処理層**：Whisper Run1/Run2 → テキスト精度は高いが話者分離なし（全`'A'`）
- **統合**：LLMが「話者＝Web Speech、テキスト＝Whisper」で前後文脈を加味して最終版を作る

### Whisper運用ルール
- Run1: `language='ja'` 固定
- Run2: `language=None` + `prompt`（参加者名＋カタカナ固有名詞をヒント）
- 同期API＝即`completed`で保存。`aai_id` プレフィクスは `whisper:` で識別

### LLM統合（ゼロ欠落・時系列整流アーキテクチャ：5フェーズ）
2026-06-05 改修。**詳細仕様の正典：`docs/superpowers/specs/2026-06-05-llm-merge-redesign-design.md`**。実装ロジック変更時は必ず spec を参照。

**3保証**
- **ゼロ欠落**: 主軸全行 + 他ソース独自発話（オーファン）を必ず含む
- **時系列順**: 主軸1本で時系列が混線しない、LLM は行順を変えられない
- **LLM 障害耐性**: チャンクごとに失敗時は主軸生テキストで埋める

**5フェーズ概要**
1. **主軸選定（決定論）** — 完了済ソースから最大量1本を選ぶ（`_select_backbone`）。**新デフォルト: `backbone_fixed=run2`（Deepgram主軸＝レコード分離・話者を優先）**
2. **行構築** — 主軸 + 他ソースを ±`cluster_window_ms` で添付、窓外+低類似度はオーファンとして時系列挿入。ここで行数確定（`_build_rows_with_orphans`）
3. **話者解決** — `speaker_priority`（`deepgram`/`ws`/`hybrid`）で分岐。**新デフォルト `deepgram`**: 行 ms 近傍の Run2 utterance speaker → 参加者idxマッピング（0=A,1=B,...）→ 未マップは「Speaker X」（`_resolve_speaker_for_row` + `_dg_speaker_at`）
4. **チャンク分割 LLM 整流（並列）** — `chunk_size` 行ずつ JSONL 出力。LLM は補完・順序修正・句読点整形のみ可、行追加/削除/並べ替えは禁止。プロンプトに「**テキスト本文は Run1（Whisper）候補を最優先採用、Run2 はレコード分離・話者用途**」を明示。重み運用デフォルト **0:7:3**
5. **強制整合（決定論）** — チャンク失敗 or 行数不一致 → 主軸直採用。候補に無い 4文字以上連続部分文字列を幻覚として上書き＋`note='要確認:幻覚検出'`

**役割分担（Run2 主軸化の意図）**
- Run2 (Deepgram) → **レコード分離 + 話者分離**（タイムスタンプ・行の切れ目・Speaker A/B が正確）
- Run1 (Whisper) → **テキスト本文の精度**（LLM が Run2 行枠内に Run1 テキストを充当）
- 旧仕様（Run1 主軸 + WS 押下話者）は設定画面で `backbone_fixed=run1`, `speaker_priority=ws`, 重み `1:7:2` に戻せる

**出力形式**: `result_json = [{ts, speaker, text, note, sources?}]`（形状不変・Excel/Q&A/要約は無改修対応）

**設定パラメータ（`merge_settings` テーブル・設定画面から変更可）**
| キー | デフォルト | 範囲 |
|---|---|---|
| `chunk_size` | 25 | 10–50 |
| `cluster_window_ms` | 2000 | 500–5000 |
| `orphan_sim_threshold` | 0.4 | 0.0–1.0 |
| `backbone_algo` | `fixed`（運用デフォルト） | 4種：`fixed` / `rows_x_log_chars` / `rows_only` / `chars_only` |
| `backbone_fixed` | `run2`（Deepgram 主軸） | `''`/`ws`/`run1`/`run2` |
| `speaker_priority` | `deepgram` | `deepgram`/`ws`/`hybrid` |
| `default_w_ws` / `default_w_run1` / `default_w_run2` | 0 / 7 / 3 | 各 0–10 |
| `default_model` | `gemini-2.5-flash` | ALLOWED_MODELS |
| `parallel` | 3 | 1–5 |
| `max_tokens_per_chunk` | 8192 | 4096–16384 |
| `retry_per_chunk` | 1 | 0–3 |

> **既存DBの注意**: `merge_settings` は INSERT OR IGNORE のため、本番DBでは旧 `backbone_fixed=run1` が残っている。新仕様を有効化するには管理画面「⚙ 設定 → LLM統合詳細設定」で固定主軸を `Run2` に、話者解決優先度を `Deepgram` に、既定重みを `0:7:3` に保存する。

**設定 API**
- `GET /api/settings/merge`（Basic 認証）— 現在の設定取得
- `PUT /api/settings/merge`（Basic 認証）— 部分更新、範囲外は自動クランプ

**マージ Response の追加情報**
`POST /api/projects/{id}/merge` は `{ok, segments, notes, backbone, fallback_chunks, hallucinations}` を返す。`fallback_chunks > 0` は LLM が一部チャンクで指示違反したことを示すサーバ側計測値（決定論で補償済み）。

### 差分比較の正規化
管理画面の4カラム比較で「内容の差異」だけを検出するため、比較時は：
- 日本語間スペース除去
- 句読点（`。、！？!?,\.・`）除去
- `【要確認...】`タグ除去

表示テキストは元のまま、比較だけ正規化版を使う。

---

## 制約・注意事項

- **HTTPS必須**：Web Speech API・マイクアクセスともHTTPSでのみ動作
- **iOS Safari固有**：バックグラウンドでMediaRecorderが止まる。STT自動再起動（`onEnd`→300ms後）で対処
- **iOS 7日間削除**：PWAホーム画面追加で緩和。完全永続はVPS側
- **録音上限20分/チャンク**：20分到達で自動 pause（stop ではない）。同プロジェクト内で再開すると2本目チャンクが追加。残り5分で黄色警告、1分で赤
- **STT無反応検知**：Web Speechから20秒以上結果がない場合、録音画面にインラインバナー表示（モーダル/alert禁止）。再起動ボタン or 閉じるボタン付き
- **フォントサイズ**：16px基準・4の倍数のみ（16/20/24/28/32px）
- **配色**：単色のみ・**グラデーション全面禁止**（CLAUDE.md global rule）。WCAG AAコントラスト遵守
- **APIキーは絶対クライアントに出さない**：すべてVPSの`.env`管理、FastAPIでプロキシ
- **成果物に個人名・法人名を含めない**

---

## Git ワークフロー

- リポジトリ: `https://github.com/kurapomu/ai-voice-memo-v1.1`（mainブランチのみ運用）
- **GitHub Pages は無効**。デプロイは VPS への scp が正のため、commit と本番反映は別操作
- 編集 → ローカル動作確認 → VPS へ scp → `git add/commit/push`（履歴目的）の順
- `_main_remote.py` / `_admin_remote.html` / `_db_remote.py` は **ローカル正典としてコミット対象**（履歴管理）
- 現状の `.gitignore` は `pics/` のみ

---

## 認証情報・URL

- VPS SSH: `ssh root@162.43.14.31`（鍵認証済み）
- モバイルアプリ: `https://jizo-dev.com/ai-voice-memo/`
- PC管理画面: `https://jizo-dev.com/ai-voice-memo/admin/`（ID: `test` / PW: `test`）
- **管理画面 設定モーダル PIN: `5963`**（`⚙ 設定` ボタン押下時に要求。セッション中は再入力不要）
- GitHubリポジトリ: `https://github.com/kurapomu/ai-voice-memo-v1.1`（mainブランチ）
- GitHub Pages: **無効化済み**（VPSのみで運用）

---

## 進捗・未完了

### 完了
- ✅ モバイルPWA（録音・Web Speech・話者ボタン・MTG名・残り時間・IndexedDB）
- ✅ VPS構築（Nginx + HTTPS + FastAPI + SQLite + systemd）
- ✅ Whisper / Deepgram 連携（Run1/Run2・モデル選択・force再実行）
- ✅ Gemini LLM統合（旧重み付きアーキテクチャ、2026-06-05に新設計で置換）
- ✅ **LLM統合・ゼロ欠落時系列整流アーキテクチャ（5フェーズ新設計）** — 2026-06-05 完成。`docs/superpowers/specs/2026-06-05-llm-merge-redesign-design.md`
- ✅ PC管理画面（4カラム比較・タイムライン軸マージ2秒窓・歯車設定・MTG削除/改名・音声ダウンロード）
- ✅ **LLM最終版トランスクリプトの Excel エクスポート（管理画面ビューと同一の4カラム + Summary/Tasks/Issues シート）** — 2026-06-05
- ✅ **LLM統合詳細設定の管理画面 UI 化**（チャンクサイズ・並列・主軸選定アルゴリズム等9項目） — 2026-06-05
- ✅ 録音20分自動pause・STT無反応バナー
- ✅ ログシステム（12hローテーション・`/api/logs`で閲覧）

- ✅ **Zoom連携（v2：Pull型一覧フロー）** — 2026-06-05 完成。Server-to-Server OAuth で自Zoomアカウント主催の Cloud Recording を取り込み。
  - 管理画面の「☁ Zoom録画一覧から取り込み」モーダル → Zoom API live取得＋Webhook通知ログをマージ表示 → 1件ずつ「取り込む」→ DL→projects 化（1ステップ）
  - Webhook (`recording.completed`) は **通知のみ記録**（自動DLしない）
  - 主要 API: `GET /api/zoom/cloud-recordings?days=N` / `POST /api/zoom/cloud-recordings/{uuid:path}/import`
  - 他組織主催MTGは「他組織MTGをファイルアップロード」モーダル（既存）で対応
  - 設計仕様: `docs/superpowers/specs/2026-06-04-zoom-integration-design.md`

### 未着手（フェーズ3〜4）
- ⬜ Claude API での要約・ToDo抽出・Q&A（CLAUDE.mdガイドラインでHaiku優先想定）
- ⬜ 用途別テンプレート（会議・インタビュー・講義）
- ⬜ Presidio + GiNZA によるPII保護
- ⬜ プライバシーポリシー整備・公開
- ⬜ 一時停止/再開フロー（フェーズ2f：プラン承認済み・未実装）

### 検討事項
- AssemblyAI連携コードは`main.py`に残置（Whisper/Deepgram移行後に再評価）
- AssemblyAI $50無料クレジットは未使用
- Run設定モーダルは「モデル」のみ選択（エンジンはモデルから自動派生：`whisper-1`→whisper / `nova-*`→deepgram）
