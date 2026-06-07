# 用途別テンプレート（会議/インタビュー/講義）— 設計仕様

作成日: 2026-06-07

## Context（背景）
要約・Q&A は現在「会議」前提の固定プロンプトで生成される。用途（会議・インタビュー・講義）によって望ましい要約観点・抽出項目・回答姿勢が異なる。**プロンプト文言のみ**を用途別に切り替え、出力形状 `{summary, tasks}` は維持したまま品質を用途適合させる。テンプレートは **MTG（project）ごとに永続**し、要約・Q&A・画面のセクション名に反映する。

CLAUDE.md「未着手（フェーズ3）」の「用途別テンプレート（会議・インタビュー・講義）」に対応。

## 方針サマリ
- 出力形状 `{summary, tasks(list[str])}` 不変（UI/Excel/DB の大改修なし）。
- テンプレは `projects.template`（既定 `meeting`）に永続。`summarize`/`ask` が project から読んでプロンプト構築。
- 画面の「残タスク」見出しはテンプレ依存の動的ラベル。

## テンプレート定義（サーバ `_main_remote.py` に dict）
`SUMMARY_TEMPLATES = { key: {...} }`。3種。各 key は以下を持つ：
- `summary_focus`: 要約で重視する観点（system/user プロンプトに差し込む）
- `tasks_focus`: tasks 配列に何を入れるか
- `tasks_label`: 画面・将来Excelで使う見出し（フロントにも同じ定義を持つ）
- `qa_role`: Q&A の system_prompt の役割文

| key | summary_focus | tasks_focus | tasks_label | qa_role |
|---|---|---|---|---|
| `meeting`（既定） | 決定事項・議論の流れ・結論 | 残タスク/アクションアイテム（担当者がいれば「[名前] 〇〇する」） | 残タスク | 会議の議事録から回答する専門家 |
| `interview` | インタビュイーの発言要旨・主要トピック・重要な見解 | フォローアップ事項・追加確認したい点・深掘りポイント | フォローアップ事項 | インタビュー記録から回答する専門家 |
| `lecture` | 講義の要点・主要概念・結論 | 学習ポイント・復習すべき項目・重要キーワード | 学習ポイント | 講義内容から回答する専門家 |

- 不正・未知 template は `meeting` にフォールバック。
- 既存の summarize/ask の固定 system/user プロンプト文言を、選択テンプレの focus/role で組み立てる形に置換（JSON 出力形式・パース・保存ロジックは不変）。

## データモデル
`_db_remote.py`：
- `projects` CREATE TABLE に `template TEXT NOT NULL DEFAULT 'meeting'` を追加。
- マイグレーション配列に `"ALTER TABLE projects ADD COLUMN template TEXT NOT NULL DEFAULT 'meeting'"` を追加（既存DB対応・try/except）。

## API（`_main_remote.py`）
- `summarize_project`（1806）/ `ask_project`：プロジェクトの `template` を読み（`SELECT ... template FROM projects`）、`SUMMARY_TEMPLATES` からプロンプトを構築。`?model=` は従来どおり。`?template=` の上書きは設けない（projects が唯一の真実）。
- `update_project`（2262・`PATCH /api/projects/{id}`）：`{template}` を受理。許可値（meeting/interview/lecture）以外は 400。`UPDATE projects SET template=?`。
- **`GET /api/projects/{id}` 詳細が `template` を返すこと**を確認・必要なら追加（フロントが復元に使う）。

## UI（`_admin_remote.html`）
- フロントにも `TEMPLATE_LABELS = {meeting:{name,tasksLabel}, ...}` を定義。
- 要約・Q&Aモデルバー（`#summary-model-bar`）の隣に `#summary-template-select`（会議/インタビュー/講義）を追加。
- 描画時：`p.template`（既定 meeting）でセレクタ値と「残タスク」見出しラベルを設定。
- onChange：`PATCH /api/projects/{id} {template}` で永続 → `currentProject.template` 更新 → tasks 見出しラベル即時更新 → toast「テンプレートを変更しました。『🔄要約を再生成』で反映されます」。
- `tasks-section` の `<h4>✅ 残タスク</h4>` を動的に（`✅ <tasksLabel>`）。
- merge後の自動要約・`🔄要約を再生成` は projects.template を読むため自動で新テンプレ反映。

## 影響範囲
- `_db_remote.py`→`/opt/jizo-api/db.py`、`_main_remote.py`→`main.py`（scp＋`systemctl restart jizo-api`）。`_admin_remote.html`→admin（scp）。
- `index.html`/`sw.js` 変更なし。app-version は更新ルールにより加算（admin 変更あり）。

## 検証
1. 既存プロジェクトを開く → テンプレ既定 `会議`、見出し「✅ 残タスク」。
2. テンプレを「インタビュー」に変更 → PATCH 成功、見出しが「✅ フォローアップ事項」に即時変化、リロード後も保持（projects.template 永続）。
3. 「🔄要約を再生成」→ サーバーログ確認、要約の観点・tasks の内容がインタビュー向けになる。
4. Q&A をインタビューテンプレで実行 → 回答の姿勢がインタビュー記録ベース。
5. 「講義」でも同様（学習ポイント）。
6. merge → 自動要約が現在のテンプレで生成されることを確認。

## スコープ外（YAGNI）
- 出力セクション構造そのものの可変化（{summary,tasks} 維持）
- テンプレの追加・編集UI（3種固定）
- Excel 見出しの動的化（まず画面のみ。必要なら次段）
- `?template=` による生成時上書き（projects を単一の真実とする）
