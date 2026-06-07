# 要約・Q&A専用モデル分離 / merge境界重複排除 / LLMセル内容クリア — 設計仕様

作成日: 2026-06-07

## Context（背景）

CLAUDE.md「未着手」に記載の「Claude API での要約・ToDo抽出・Q&A」は、調査の結果**バックエンド・管理画面UIとも実装済み**で、唯一 Claude Haiku がモデル選択肢としてUI非表示だった（CLAUDE.md「UIは非表示で保留」の実体）。これを露出するにあたり、現状は単一セレクタ `#model-select` が **merge → 要約 → Q&A の3用途を兼用**しており、Gemini向けに最適化された merge（`_main_remote.py:913` の設計意図・ゼロ欠落5フェーズ）に Claude が混入する品質リスクがある。

あわせてユーザーから2件の追加要件：
- **B**: LLM最終まとめで、隣接レコードの前後に同一発話の断片が重複する（漢字変換違い・区切り違い）。merge プロンプトでこの境界重複を排除したい。
- **C**: 生成結果から不要な重複セルの内容を消したい。**行ごと削除ではなく、LLM最終まとめ列の該当セルの `text` を空文字にするだけ**（行・ts・話者・Run1/Run2/WS の比較データは保持）。

## 方針サマリ

3機能。バックエンドは C のみ新endpoint追加、A は改修不要、B はプロンプト文言追加のみ。

---

## A. 要約・Q&A専用モデルセレクタの分離

**変更対象**: `_admin_remote.html` のみ（バックエンド改修なし）

- LLM最終版セクション（`#merged-section` 付近）に新セレクタ `#summary-model-select` を新設。
  - `gemini-2.5-flash`（推奨・既定）/ `gemini-2.5-pro` / `claude-haiku-4-5-20251001`（要約・Q&A向け）
  - 横に「🔄 要約を再生成」ボタン `#btn-resummarize`：既存 `POST /api/projects/{id}/summarize?model=<選択値>` を呼ぶだけ。
- モデル参照の付け替え：
  | 処理 | 現状の参照 | 変更後 |
  |---|---|---|
  | merge 実行（btn-merge, ~2641） | `#model-select` | **変更なし**（Gemini専用維持） |
  | 自動要約（merge後, ~2664） | `#model-select` | `#summary-model-select` |
  | Q&A（btn-ask, ~2695） | `#model-select` | `#summary-model-select` |
- `#model-select`（merge用）・`#ms-default-model`（merge既定）は Gemini 2択のまま不変。
- バックエンド: `summarize`(`_main_remote.py:1755`) / qa は既に任意 `model` を受理し `_call_llm` が Claude へ振り分け済み。allowlist 無し。改修不要。

## B. merge チャンクプロンプトで境界重複を排除（プロンプトのみ）

**変更対象**: `_main_remote.py` の `_build_chunk_prompt`（1298-）の system_prompt

- 許可操作リスト（現「1.補完 / 2.順序修正 / 3.句読点整形」）に4項目目を追加：
  > 4. 隣接レコード境界の重複除去：直前/直後の行（「文脈」に表示済み）と**同一発話の断片**が当該行の先頭または末尾に重複している場合（**漢字変換違い・区切り違い・送り仮名違いを含む同一発話**）、当該行から重複部分のみをトリムして自然な境界にする。**明確な重複のみ**トリムし、別発話か判断に迷う場合は残す。
- **禁止事項は不変**：行の追加・削除・統合・分割・並べ替えは引き続き禁止。トリムは行を消さずテキストのみ短縮するため、行構造・ゼロ欠落保証・時系列順は保持される。
- 幻覚ガード（Phase5：候補に無い4文字以上連続部分文字列の検出）はテキスト「追加」を検出する仕組みのため、トリム（削減）では誤検出しない。
- ユーザー指定どおり**プロンプトのみ**。決定論的 dedup パスは追加しない（YAGNI）。

## C. LLM最終まとめセルの内容クリア

**新endpoint**: `_main_remote.py`
```
PATCH /api/projects/{project_id}/merged/text   (Basic認証)
body: {"row_idx": int, "text": str}   # text は空文字 "" を許可
```
- 話者編集endpoint（`update_merged_speaker`, 1825）と同型。最新 `merged_transcripts` を取得 → `result_json` をパース → `row_idx` 範囲チェック（非負・len未満）→ `rows[row_idx]['text'] = text` → 永続化。
- speaker endpoint と違い **空文字を許可**（クリアが目的のため非空バリデーションを入れない）。`text` は `isinstance(str)` のみ検証。

**フロント**: `_admin_remote.html`
- `renderMerged` のLLMカラムセル（`_resultIdx` を持つ）に「✕」ボタンを追加（`title="このセルの内容を空白にする"`）。
- クリック → `confirm('このセルの内容を空白にします。よろしいですか？')` → `PATCH /merged/text {row_idx: seg._resultIdx, text: ''}`。
- 成功後：**再読込不要**。行は残りindexも不変なので、当該セルのテキスト表示を空にし、`currentProject.merged.result[rowIdx].text=''` を更新するだけ。
- 失敗時 alert。

**波及**:
- Excel/要約/Q&A は `result_json` を参照するため、空文字セルは「行は残るがLLMテキストが空」で反映。**Run1/Run2/WS は別ソース由来で保持される**（ユーザー意図どおり）。
- 再 merge すると `result_json` が再生成されクリアは失われる（手動後処理の性質上、許容。注記する）。

---

## 影響ファイル

- `_main_remote.py`：B（`_build_chunk_prompt` プロンプト追記）/ C（新endpoint `update_merged_text`）→ VPS `/opt/jizo-api/main.py` へ scp ＋ `systemctl restart jizo-api`
- `_admin_remote.html`：A（新セレクタ＋再生成ボタン＋参照付替）/ C（✕ボタン＋ハンドラ）→ VPS admin へ scp
- `index.html` / `sw.js` / DB スキーマ：**変更なし**

## 検証

1. **A**: 管理画面で `#summary-model-select` に Claude Haiku を選び「🔄要約を再生成」→ サーバーログ `summarize start ... model=claude-haiku-4-5-20251001` を確認、要約が更新される。Q&Aで Claude が応答する。merge は `#model-select`（Gemini）で動くことを確認（Claudeがmergeに混入しない）。
2. **B**: 境界重複が出やすい既存プロジェクトを再 merge し、隣接行の重複断片がトリムされること、かつ行数・話者・時系列が不変であることを4カラム比較で確認。`fallback_chunks` が増えていない（指示違反でない）ことを merge レスポンスで確認。
3. **C**: LLMセルの✕で内容が空になり、同行の Run1/Run2 が残ること、再読込後も空が永続していること、Excel出力で当該行のLLM列のみ空・他列は保持を確認。

## スコープ外（YAGNI）

- 要約・Q&A既定モデルの `merge_settings` 永続化（DOM既定 Gemini Flash で足りる）
- LLMセルのインライン**テキスト編集**（今回は「クリア」のみ。endpoint は将来の編集にも流用可能な汎用形）
- クリアのundo（Run1/Run2が隣に見えており、再mergeで復元可能なため不要）
- B の決定論的 dedup フォールバック
