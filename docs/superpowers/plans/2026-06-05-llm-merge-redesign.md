# LLM最終版マージ・ゼロ欠落時系列整流アーキテクチャ（実装プラン）

- 作成日: 2026-06-05
- 設計仕様: `docs/superpowers/specs/2026-06-05-llm-merge-redesign-design.md`
- 状態: **実装完了・本番デプロイ済・初期検証 OK**（既存問題 MTG で 15 行 → 141 行・幻覚 0）

---

## Context

`merge_project` の旧設計が抱える3つの欠陥（出力トークン truncation、LLM 指示不遵守、検証不在）を、決定論的行構成 + LLM テキスト整流 + チャンク分割 + 強制整合の5フェーズに再設計する。設計詳細は spec ファイル参照。

ユーザー要件:
- ゼロ欠落（すべての発話が含まれる）
- 時系列の単一カラム（動画の流れに沿う）
- 動画をすべて聞かなくて済む状態が最終目的
- 「正しい順番で録音の発話が並んでいること」が最重要

---

## 実装ステップ（順序）

### Step 1: DB スキーマ拡張
- `_db_remote.py` を `/opt/jizo-api/db.py` から localize（ローカル正典化）
- `init_db()` 内に `merge_settings(key TEXT PK, value TEXT, updated_at TEXT)` テーブルを追加
- 9 個のデフォルト値を `INSERT OR IGNORE` で初期投入
- 既存 DB 環境では再起動時にテーブルが自動作成される

**変更ファイル**: `_db_remote.py`（新規・コミット対象）

### Step 2: 補助関数の Python 実装
`_main_remote.py` に以下を追加（`@app.post('/api/projects/{project_id}/merge')` の手前にブロック挿入）:

- `ALLOWED_MODELS`: 既存維持（書換え時に保護）
- `MERGE_SETTINGS_RANGES`: 数値設定の (型, lo, hi) タプル
- `MERGE_SETTINGS_DEFAULTS`: デフォルト値辞書（9 キー）
- `_get_merge_settings() -> dict`: DB 読出 + クランプ + デフォルト補完
- `_clamp_merge_settings(d) -> dict`: PUT 入力のサニタイズ
- `_normalize_for_compare(text) -> str`: 句読点・スペース・要確認タグ除去
- `_text_sim(a, b) -> float`: 包含優先 + bigram Dice 係数（JS の textSim 移植）
- `_select_backbone(ws, r1, r2, algo, fixed) -> (key, items)`: Phase 1
- `_build_rows_with_orphans(...) -> rows`: Phase 2 全体
- `_resolve_speaker_for_row(ms, sp_events_sorted, ws_items, name_map) -> (name, uncertain)`: Phase 3
- `_chunk_rows(rows, size, ctx) -> chunks`: Phase 4 のチャンク化
- `_detect_hallucination(text, candidates) -> bool`: Phase 5 の幻覚検出
- `_parse_jsonl(raw, expected_n) -> list[dict] | None`: LLM 出力解析（JSONL/JSON配列両対応）
- `_call_gemini_chunk(sys, user, model, max_tokens, retry) -> str | None`: 単一チャンク LLM 呼出（リトライ込み）
- `_build_chunk_prompt(chunk, plist, ws, r1, r2, run_engines) -> (sys, user)`: プロンプト組立
- `_make_segment(row, text, note) -> dict`: 最終 result_json 要素生成

### Step 3: `merge_project` リライト
既存の `merge_project` 関数を全面置換:
1. 設定取得（`_get_merge_settings()`）
2. `model` 未指定なら `settings['default_model']`
3. 既存の入力取得部分（proj/participants/ref_segs/speaker_events/txs/recordings/offsets）は流用
4. 重み正規化（合計10、LLM ヒントとしてのみ使用）
5. ソース正規化: `ws_items / run_items[1] / run_items[2] / run_engines`
6. Phase 1: `_select_backbone(...)`
7. Phase 2: `_build_rows_with_orphans(...)`
8. Phase 3: 全行に `_resolve_speaker_for_row` を適用
9. Phase 4: `_chunk_rows(...)` → `asyncio.gather(*_process_chunk(c))` with `Semaphore(parallel)`
10. Phase 5: 各 chunk の出力検証 → `final_segs` 構築
11. `sources` フィールド付加（既存仕様の管理画面・Excel 互換）
12. notes 集約・DB 保存・`status='merged'` 更新
13. Response: `{ok, segments, notes, backbone, fallback_chunks, hallucinations}` を追加情報込みで返却

**変更ファイル**: `_main_remote.py`（既存正典）

### Step 4: 設定 API 実装
- `@app.get('/api/settings/merge')` → `get_merge_settings_endpoint()`
- `@app.put('/api/settings/merge')` → `update_merge_settings_endpoint()`（Basic 認証 + クランプ後 UPSERT）

**変更ファイル**: `_main_remote.py`

### Step 5: 管理画面：マージ呼出 UI 微修正
- マージ POST の URL から `&base=...` を削除（line 2013–2015）
- `getMergeSettings()` から `base` フィールドを削除（localStorage `merge_base` も使わなくなる）
- `saveMergeBase / onMergeBaseChange` 関数を削除
- `loadMergeSettingsUI` から base ラジオ操作を削除

**変更ファイル**: `_admin_remote.html`

### Step 6: 管理画面：設定モーダル拡張
- 重みスライダーセクションのヘッダーを「LLM統合の重み付け（合計10・補完優先度ヒント）」に変更
- 基準ソースのラジオボタン（base 選択）UI を削除
- 説明文を「補完優先度ヒント」「主軸選定は別アルゴリズム」に書換え
- 新セクション「⚙ LLM統合詳細設定」を追加:
  - 主軸選定アルゴリズム（select、4 オプション）
  - 固定主軸（select、algo=fixed 時のみ表示）
  - デフォルトモデル（select）
  - チャンクサイズ / 並列LLM呼出 / クラスタ統合窓 / オーファン類似度閾値 / max_tokens / リトライ回数（number input × 6）
  - 「デフォルト」「保存」ボタン + ステータス表示
- 対応 JS 関数を追加: `loadMergeAdvanced / saveMergeAdvanced / resetMergeAdvanced`
- `openApiSettings()` から `loadMergeAdvanced()` を呼出
- `change` イベントリスナーで `ms-backbone-algo` 変更時に固定主軸行の表示制御

**変更ファイル**: `_admin_remote.html`

### Step 7: ローカルテスト
- `python -c "import ast; ast.parse(open('_main_remote.py').read())"` で構文チェック
- `python -c "import ast; ast.parse(open('_db_remote.py').read())"` で構文チェック

### Step 8: VPS デプロイ
```bash
scp _main_remote.py root@162.43.14.31:/opt/jizo-api/main.py
scp _db_remote.py   root@162.43.14.31:/opt/jizo-api/db.py
scp _admin_remote.html root@162.43.14.31:/var/www/jizo-dev.com/ai-voice-memo/admin/index.html
ssh root@162.43.14.31 "systemctl restart jizo-api"
```

### Step 9: 本番検証
詳細手順は spec ファイル §8 参照。最低限:
```bash
# 1. 設定 API 動作確認
curl -u test:test https://jizo-dev.com/api/settings/merge

# 2. 設定 PUT クランプ確認
curl -u test:test -X PUT -H "content-type: application/json" \
  -d '{"chunk_size":999}' https://jizo-dev.com/api/settings/merge
# → "chunk_size":50 にクランプされて返ること

# 3. 既存問題 MTG で再マージ
curl -u test:test -X POST \
  'https://jizo-dev.com/api/projects/63fd7662-62c8-4253-9243-b693c368487a/merge'
# → {"ok":true,"segments":141,"notes":...,"backbone":"run1","fallback_chunks":<6,"hallucinations":0}

# 4. result_json 内容確認
ssh root@162.43.14.31 "/opt/jizo-api/venv/bin/python -c \"
import sqlite3, json
c = sqlite3.connect('/var/jizo/jizo.db'); c.row_factory = sqlite3.Row
m = c.execute('SELECT result_json FROM merged_transcripts WHERE project_id=? ORDER BY id DESC LIMIT 1', ('63fd7662-62c8-4253-9243-b693c368487a',)).fetchone()
r = json.loads(m['result_json'])
print('rows', len(r), 'empty', sum(1 for s in r if not s.get('text','').strip()))
for s in r[-5:]: print(s.get('ts'), s.get('speaker'), s.get('text','')[:60])
\""
# → 末尾 5 行が空でないこと、コヒーレントなテキストであること
```

### Step 10: 関連ドキュメント更新
- `docs/superpowers/specs/2026-06-05-llm-merge-redesign-design.md`（spec、新規作成済）
- `docs/superpowers/plans/2026-06-05-llm-merge-redesign.md`（このファイル）
- `CLAUDE.md` の「LLM統合（重み付きアーキテクチャ）」セクション全面書換え
- `CONCEPT.md` のマージ哲学セクション再記述
- `docs/system-overview.html` のフロー図とパラメータ表更新

---

## 実装結果（2026-06-05 16:34 時点）

### 数値結果（プロジェクト `63fd7662-62c8-4253-9243-b693c368487a`）
| 指標 | 旧設計 | 新設計 |
|---|---|---|
| 出力行数 | 15 | **141** |
| 末尾の空テキスト行 | 不明（壊れ末尾） | 0 |
| 検出された幻覚 | 複数（要目視） | **0** |
| LLM 失敗チャンク | — | 1–2 / 6（フォールバックで救済） |
| 主軸ソース | run1 (旧重み計算) | run1 (自動選定) |

### ログ抜粋
```
2026-06-05 16:33:47 INFO: merge start project=63fd7662-... model=gemini-2.5-flash
  backbone=run1 rows=141 ws=24 r1=139 r2=97
  settings={'chunk_size': 25, 'cluster_window_ms': 2000, ..., 'parallel': 3, ...}
2026-06-05 16:34:50 INFO: merge llm done chunks=6 fallback=2 hallucinations=0 final_rows=141
```

### 末尾品質（9 分台の発話）
```
09:27 参加者3 そこへ警察から連絡が入り
09:29 参加者3 彼女は婚約者の身に何が起こったのか全く分からないまま店を飛び出したそうです。
09:35 参加者3 しかし彼女が病院に駆けつけた時
09:41 参加者3 里山さんはすでに息を引き取っていました。
09:45 参加者3 呆然と立ち尽くす彼女の前に警察官が来て
```
→ 旧設計では到達すらできなかった末尾領域が、コヒーレントなテキストで埋まっている。

---

## 残課題・後続タスク

### 後続 1: 長尺 MTG での検証
- 30 分以上の MTG での挙動確認
- 並列度 3 で API レート制限に達しないか観測
- fallback_chunks 比率が高い場合はチャンクサイズを下げて再評価

### 後続 2: fallback_chunks をユーザーに可視化
- 現状はサーバログのみ
- 管理画面のマージ完了表示で `LLM 6 チャンク中 2 失敗（決定論で補完）` のように見える化する候補
- spec §5.2 の追加項目として検討

### 後続 3: オーファン挿入のチューニング
- 現状の閾値（0.4 / window×3 / 500ms連結）は経験値
- MTG の音響条件・話者数により最適値は変動
- 実運用データを集めて A/B 比較できる構造を後で導入

### 後続 4: Pro モデルでの品質比較
- 現状デフォルトは Flash
- Pro での fallback_chunks 比率と幻覚率を計測して、推奨モデルを判断
- コストとの兼ね合いで設定画面のデフォルトを切り替える可能性
