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
- `sw.js` — Service Worker（HTMLはネットワーク優先・他はキャッシュ優先）。現バージョン定数 `const CACHE = 'voicememo-vX.Y';`（編集後は必ずインクリメント）
- `CONCEPT.md` — プロダクト哲学・全体構成の図解ドキュメント（人間向け補足。CLAUDE.mdより詳細な背景）
- `docs/system-overview.html` — システム概要のHTML版（社外説明用）
- `_main_remote.py` / `_admin_remote.html` — **ローカル正典（編集可・scpデプロイ正規ルート）**。これらを編集 → VPS の `/opt/jizo-api/main.py` および `/var/www/jizo-dev.com/ai-voice-memo/admin/index.html` へ scp → 必要に応じ `systemctl restart jizo-api`

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
```bash
# モバイルアプリ更新
scp "c:\Users\taka\Downloads\files (1)\index.html" root@162.43.14.31:/var/www/jizo-dev.com/ai-voice-memo/index.html

# FastAPI更新
scp local_main.py root@162.43.14.31:/opt/jizo-api/main.py
ssh root@162.43.14.31 "systemctl restart jizo-api"

# 管理画面更新
scp admin.html root@162.43.14.31:/var/www/jizo-dev.com/ai-voice-memo/admin/index.html

# Service Workerを更新したら必ず sw.js の CACHE バージョンを上げる（voicememo-vX.Y）
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

### LLM統合（重み付きアーキテクチャ）
旧 `summary_level` / `webspeech_fidelity` は撤廃。**重み3軸**で制御：
- パラメータ：`w_ws` / `w_run1` / `w_run2`（合計10になるよう正規化）
- **骨組み選定**：重み最大ソースが行数を決める（同点時 run2>run1>ws）
- **クラスタ構築**：骨組みの各セグメント=1行、他ソースは ±2秒以内の最近傍を候補に
- **話者決定**：優先度チェーン（押下±2秒 → 直前継承 → WS ref_segments → 未設定）
- **LLMタスク**：行追加/削除/マージ禁止。各行で「重みに従って候補から選ぶ・合成」
- 出力形式：`[{ts, speaker, text, note}]` のJSON
- 不確実箇所は `【要確認:理由】` インラインタグ＋`note` フィールド
- 話者「未設定」の行は `text` 末尾に `【要確認:話者】` を付記

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
- `_main_remote.py` / `_admin_remote.html` は本番取得スナップショットのため、原則コミットしない（.gitignore推奨）

---

## 認証情報・URL

- VPS SSH: `ssh root@162.43.14.31`（鍵認証済み）
- モバイルアプリ: `https://jizo-dev.com/ai-voice-memo/`
- PC管理画面: `https://jizo-dev.com/ai-voice-memo/admin/`（ID: `test` / PW: `test`）
- GitHubリポジトリ: `https://github.com/kurapomu/ai-voice-memo-v1.1`（mainブランチ）
- GitHub Pages: **無効化済み**（VPSのみで運用）

---

## 進捗・未完了

### 完了
- ✅ モバイルPWA（録音・Web Speech・話者ボタン・MTG名・残り時間・IndexedDB）
- ✅ VPS構築（Nginx + HTTPS + FastAPI + SQLite + systemd）
- ✅ Whisper / Deepgram 連携（Run1/Run2・モデル選択・force再実行）
- ✅ Gemini LLM統合（**重み付き 3軸スライダー**・差分検出・要確認タグ）
- ✅ PC管理画面（4カラム比較・タイムライン軸マージ2秒窓・歯車設定・MTG削除/改名・音声ダウンロード）
- ✅ 録音20分自動pause・STT無反応バナー
- ✅ ログシステム（12hローテーション・`/api/logs`で閲覧）

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
