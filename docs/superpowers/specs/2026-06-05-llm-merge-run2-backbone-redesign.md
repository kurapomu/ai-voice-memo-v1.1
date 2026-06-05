# LLM 統合 Run2 主軸再設計 — 設計仕様

- 作成日: 2026-06-05
- 関連: `2026-06-05-llm-merge-redesign-design.md`（ベース 5フェーズ設計）
- ステータス: 設計確定（実装未着手）

## 背景

### 現状（2026-06-05 既存設計）
- 主軸 (`backbone_fixed`) のデフォルトは `run1` (Whisper)
- Run1 がレコード分割・タイムスタンプ・テキストすべての骨格を担う
- 話者解決は Phase 3 で WS 押下イベント優先

### 観測された問題（管理画面比較ビュー証拠：`pics/image copy 4.png`）

| カラム | 話者 | テキスト |
|---|---|---|
| 参考データ (WS+押下) | 参加者3 | 「もういいのかな？ もう少しよろしいでしょ？」 |
| Run1 (Whisper) | Speaker A 固定 | 「もういいのかなもう少しよろしいでしょ」（**1行に圧縮・話者分離なし**） |
| Run2 (Deepgram) | Speaker B / B | 「。もういいのかな」「もう少しよろしいでしょうか」（**2行に正しく分割・話者分離あり**） |

→ Whisper はテキスト精度が高いが発話境界を取りこぼす。Deepgram は発話境界・話者分離が適切。
→ **「行分割は Deepgram、テキスト精度は Whisper」を両取りすべき**。

## 設計方針

各エンジンの強みを役割分担：
- **Run2 (Deepgram)**: レコード分離（行分割）+ タイムスタンプ + 話者分離
- **Run1 (Whisper)**: テキスト本文の精度

## 変更内容（5フェーズへの差分）

既存5フェーズ構造は維持。設定デフォルトとLLMプロンプトを変更する。

### Phase 1: 主軸選定
- **デフォルト変更**: `backbone_fixed` を `run1` → **`run2`** に変更
- 旧仕様運用は設定画面で `run1` 選択により復帰可能

### Phase 2: 行構築
- 変更なし。Run2 主軸の各行に Run1 / WS を ±`cluster_window_ms` で添付

### Phase 3: 話者解決
- **新設定追加**: `speaker_priority` (`deepgram` / `ws` / `hybrid`)、デフォルト `deepgram`
- `deepgram`: Run2 の `speaker_map_json` を最優先 → 行に紐づく Deepgram 話者ID → 参加者マッピング → 未マップなら「Speaker A/B/C…」
- `ws`: 既存ロジック（押下±2秒 → 直前継承 → WS speaker_idx ±4秒 → 未設定）
- `hybrid`: WS 押下±2秒があればWS、無ければ Deepgram、それも無ければ「未設定」
- 実装: `_resolve_speaker_for_row(...)` を分岐化

### Phase 4: LLM 整流
- **重みデフォルト変更**: `w_ws:w_run1:w_run2 = 1:7:2` → **`0:7:3`**（Run1 テキスト最優先、Run2 は補助）
  - `w_ws=0` の運用意義: 主軸を Run2 にしたため WS は候補補完源として弱い位置付け
- **プロンプト追記**（`_build_chunk_prompt` 内）:
  ```
  ### テキスト本文の優先採用ルール
  - 主軸（Run2 / Deepgram）はレコード分離と話者の正確性のために選定されているが、
    テキスト本文は精度が低い場合がある
  - 同じ発話に Run1 (Whisper) 候補が存在する場合、テキスト本文は Run1 を最優先採用すること
  - Run2 のテキストは「Run1 候補が無い箇所の補完」「Run1 と意味が一致する場合の整合確認」のみに使う
  - 行の構造（行数・順序・話者）は主軸 Run2 を絶対遵守し、変更してはならない
  ```
- 他制約（行追加禁止・JSONL 出力・3並列）は不変

### Phase 5: 強制整合
- 変更なし

## 設定パラメータ追加・変更

| キー | 旧デフォルト | 新デフォルト | 範囲 | 備考 |
|---|---|---|---|---|
| `backbone_fixed` | `run1` | `run2` | `''`/`ws`/`run1`/`run2` | Run2 主軸化 |
| `default_w_ws` | 1 | 0 | 0–10 | 重み再配分 |
| `default_w_run1` | 7 | 7 | 0–10 | 維持 |
| `default_w_run2` | 2 | 3 | 0–10 | Run2 優先度 +1 |
| `speaker_priority` | （新規）| `deepgram` | `deepgram`/`ws`/`hybrid` | 新規追加 |

その他（`chunk_size` / `cluster_window_ms` 等）は不変。

## 実装対象ファイル

### バックエンド
- `_main_remote.py`
  - `_select_backbone(...)` — Phase 1 デフォルト変更
  - `_resolve_speaker_for_row(...)` — `speaker_priority` 分岐
  - `_build_chunk_prompt(...)` — テキスト優先ルール追記
  - `MERGE_SETTINGS_DEFAULTS` — デフォルト値更新
  - `GET/PUT /api/settings/merge` — `speaker_priority` フィールド追加

- `_db_remote.py`
  - `merge_settings` テーブルに `speaker_priority` カラム追加（既存DB向けマイグレーション：起動時 ALTER TABLE IF NOT EXISTS 相当）

### フロントエンド
- `_admin_remote.html`
  - 「LLM統合詳細設定」セクションに話者優先度ドロップダウン追加
  - 重みスライダーのデフォルト値変更
  - 主軸選定 UI のヘルプテキスト更新（推奨：Run2）

### CLAUDE.md
- 「LLM統合」セクションの運用デフォルトを `0:7:3` / 主軸 `run2` / 話者 `deepgram` に更新

## 検証方法

1. ローカルで `_main_remote.py` をシンタックスチェック (`python -c "import ast; ast.parse(open('_main_remote.py').read())"`)
2. VPS に scp → `systemctl restart jizo-api`
3. 既存マージ済プロジェクトを `/api/projects/{id}/merge` で**新設定で再実行**
4. 管理画面の4カラム比較ビューで：
   - 行数が Run2 に近づくこと
   - 話者が Deepgram の Speaker A/B 由来になること
   - テキスト本文が Whisper の精度を保っていること
5. `pics/image copy 4.png` で見た事例が、最終版で「2行に分かれ、話者がDeepgram由来、テキストが Whisper の精度」になることを確認
6. `fallback_chunks` が極端に増えていないこと（LLM 指示遵守率の劣化チェック）

## ロールバック手順

問題発生時は設定画面で：
- `backbone_fixed` → `run1`
- `speaker_priority` → `ws`
- 重み → `1:7:2`

を指定し再マージで旧挙動に戻る。コード変更でなく設定変更で戻せる設計。

## 未確定事項

- Run2 が未完了のプロジェクトでマージ実行された場合のフォールバック：
  - 案A: Run1 主軸に自動フォールバック（既存 `_select_backbone` のロジックを残す）
  - 案B: エラーで停止し、Run2 完了を促す
  - **推奨: 案A**（既存挙動と互換）
