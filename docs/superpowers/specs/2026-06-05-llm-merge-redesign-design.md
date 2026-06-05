# LLM最終版マージ・ゼロ欠落時系列整流アーキテクチャ（設計仕様）

- 作成日: 2026-06-05
- 対象: `_main_remote.py` `merge_project` / `_admin_remote.html` 設定モーダル / `_db_remote.py` スキーマ
- 関連: `docs/superpowers/plans/2026-06-05-llm-merge-redesign.md`（実装プラン）

---

## 1. 背景と問題定義

### 1.1 旧設計の構造
旧 `merge_project` は「重み最大ソース1本を骨組み（時間軸の主軸）として LLM に渡し、各行のテキストを候補ソースから選ばせる／合成させる」設計だった。

- 入力: WS（Web Speech）/ Run1 / Run2 の3ソース × 重み（w_ws:w_run1:w_run2、合計10）
- 骨組み選定: 重み最大ソース（同点時はユーザー指定 `base` 優先）
- LLM: 「骨組みの全行をテキスト整流して返せ」と指示、JSON配列で全行を一度に出力
- 出力: `merged_transcripts.result_json = [{ts, speaker, text, note, sources}]`

### 1.2 観測された具体的不具合
プロジェクト `63fd7662-62c8-4253-9243-b693c368487a` で検証:

| 指標 | 入力 | 旧出力 |
|---|---|---|
| Run1 (Whisper, 主軸) 行数 | 36 | — |
| Run2 (Deepgram) 行数 | 26 | — |
| WS 行数 | 24 | — |
| LLM 出力行数 | — | **15** ← 21行が消失 |
| 候補に無い文言（幻覚） | — | 検出（例: 「9月10日午後9時頃…」候補に無し） |
| 末尾の劣化 | — | 後半数行が空・短文化 |

ユーザー報告:
> 冒頭はなんとなく良いが、後半になるにつれて欠損だらけで使い物にならない

### 1.3 根本原因（3つの独立要因）
1. **出力トークン上限**: 旧コード `max_tokens=16384` に対し、長文 MTG の JSON 配列は早期に truncation。`_main_remote.py:1210-1217` の「壊れた末尾を silent salvage して保存」経路が、欠落を表面化させず DB に保存していた。
2. **Gemini Flash の指示不遵守**: 「行追加・削除・マージ禁止」の指示を無視。36→15 などの大幅圧縮を実施。
3. **検証の不在**: LLM 出力を無検証で result_json に書き込む経路。行数・幻覚・空テキストの validation 無し。

### 1.4 解決の方針（ユーザー承認済）
> 最大量の文字起こしデータを「時系列の主軸」として整列させ、他ソースで欠損単語と語順を補完。LLM は文脈による単語補完・順序修正・整形だけを担う。最重要は「正しい順番で録音の発話が並んでいること」。

すなわち **行構成を決定論的に確定** し、LLM の役割を **行内テキストの整流のみ** に限定する。さらに **チャンク分割 + 強制整合** で欠落・幻覚を構造的に排除する。

---

## 2. 中心思想と保証する性質

### 2.1 中心思想
- LLM に「行を作らせる／消させる」設計を捨てる
- 行構成は Python の決定論的処理で確定 → LLM はテキスト編集のみ
- LLM が壊れたら決定論結果（主軸生テキスト）を返す経路を必ず持つ

### 2.2 保証する3性質
1. **ゼロ欠落**: 主軸ソースの全発話 + オーファン挿入された他ソース独自発話が、最終出力に必ず含まれる
2. **時系列順**: 主軸1本の時系列に他ソースを整列。LLM は行自体の順番を変えられない
3. **LLM 障害でも結果が返る**: チャンクごとの LLM 失敗時はそのチャンクを主軸生テキストで埋める

### 2.3 これにより捨てるもの
- LLM が「同じ内容の隣接2行を統合する」最適化 — ゼロ欠落要件と相反するため意図的に放棄
- LLM が「明らかにノイズな短文を削除する」最適化 — 同上
- 単一 LLM 呼出による「全体俯瞰した編集」 — チャンク分割で代替

---

## 3. 5フェーズアーキテクチャ詳細

### Phase 1: 主軸選定（決定論的・Python のみ）
完了済ソース（WS / Run1 / Run2）から最大量1本を「主軸」として選定。

**選定スコア（デフォルト）**: `rows × log(total_chars + 1)`
- 行数優位、文字数で同点打破
- 空ソース（rows=0）はスコア -1 で除外

**設定可能なアルゴリズム**:
- `rows_x_log_chars`（推奨・デフォルト）
- `rows_only`: 行数のみ
- `chars_only`: 文字数のみ
- `fixed`: ユーザー指定（`backbone_fixed=ws|run1|run2`）

**実装関数**: `_select_backbone(ws_items, run1_items, run2_items, algo, fixed) -> (key, items)`

### Phase 2: 行構築（主軸 + オーファン挿入）
主軸の各行に他2ソースの最近傍候補を添付し、主軸外オーファンを時系列で挿入する。

**Step 2-1: 主軸行の候補化**
- 主軸の各行 `(ms_i, ts_i, text_i)` について
- `nearest_within(items, ms_i, window_ms=CLUSTER_WINDOW_MS)` で他2ソースの最近傍を1つずつ取得
- 各 row に `cand_ws / cand_run1 / cand_run2` を持たせる（主軸ソースの列はそのソース自身のテキスト）

**Step 2-2: オーファン検出**
- 他ソースの各発話 `(ms_j, src, text_j)` について:
  - 主軸どの行とも時間距離 > `CLUSTER_WINDOW_MS` → オーファン候補
  - ただし、`±CLUSTER_WINDOW_MS × 3` 範囲内の主軸行とテキスト類似度 ≥ `ORPHAN_SIM_THRESHOLD` がある場合は「同一発話とみなして」オーファン化しない（広域類似マッチで救済）
- 同一ソース内、ms 順で 500ms 以内の隣接オーファンを連結（テキストを `" "` で結合）

**Step 2-3: オーファンを行として挿入、時系列ソート**
- オーファン行: `backbone_text = (オーファンソースのテキスト)`, `backbone_src = (オーファンの src キー)`, 該当ソース列にもテキスト
- 全行を ms で昇順ソート
- idx を 1〜N で割振り

**実装関数**: `_build_rows_with_orphans(backbone_key, backbone_items, ws_items, run1_items, run2_items, window_ms, sim_threshold) -> rows`

### Phase 3: 話者解決（決定論的・既存ロジック流用）
各行について `_resolve_speaker_for_row(ms, sp_events_sorted, ws_items, name_map)` を呼ぶ:

1. `speaker_events` の中で ms ±2000ms 内に押下があれば、最も近い押下の `speaker_name`
2. ms 以下の最新押下があれば、その `speaker_name`（直前継承）
3. `ws_items` の中で ms ±4000ms 内かつ `speaker_idx` が非 None な最近傍があれば、参加者名解決
4. それ以外は `('未設定', is_uncertain=True)`

`is_uncertain=True` の行は最終テキスト末尾に `【要確認:話者】` を付記。

### Phase 4: チャンク分割 LLM 整流（並列）
全行を `CHUNK_SIZE`（デフォルト25）行ずつに分割し、`asyncio.gather + Semaphore(PARALLEL)` で並列処理。

**チャンク構造**:
```python
{
  'idx_start': int,  # rows 内の開始 index
  'idx_end':   int,  # rows 内の終了 index (exclusive)
  'rows':      list,  # 編集対象
  'ctx_prev':  list,  # 前 ctx 行（参照のみ・出力に含めない）
  'ctx_next':  list,  # 後 ctx 行（参照のみ・出力に含めない）
}
```
`ctx` のデフォルトは 2 行（チャンク境界での文脈断絶の緩和）。

**LLM 呼出**:
- OpenAI 互換エンドポイント `{GEMINI_BASE}/chat/completions`
- `max_tokens = MAX_TOKENS_PER_CHUNK`（デフォルト 8192、25行 JSON で十分余裕）
- timeout 90 秒
- 失敗時は `RETRY_PER_CHUNK` 回（デフォルト 1）リトライ
- それでも失敗なら `None` を返し、フォールバックへ

**プロンプト**: §4 参照。

**並列制御**:
```python
sem = asyncio.Semaphore(settings['parallel'])
async def _process_chunk(chunk):
    async with sem:
        ...
llm_outputs = await asyncio.gather(*[_process_chunk(c) for c in chunks])
```

**実装関数**:
- `_chunk_rows(rows, size, ctx) -> list[chunk]`
- `_call_gemini_chunk(system, user, model, max_tokens, retry) -> str | None`
- `_build_chunk_prompt(chunk, plist, w_ws, w_run1, w_run2, run_engines) -> (sys, user)`

### Phase 5: 強制整合（決定論的）
各チャンクの LLM 出力を検証し、不整合は決定論的フォールバックで埋める。

**チャンクレベルの整合**:
- LLM 出力が `None`（API/パース失敗）または `len(output) != len(chunk['rows'])`
  → そのチャンクは全行を主軸生テキスト（`backbone_text`）で埋める、`note=None`
  → `fallback_chunks += 1` を集計

**行レベルの整合（LLM 出力が形上正しい場合）**:
- 各行で:
  1. 出力 `text` が空文字 → 主軸生テキストで埋める
  2. `_detect_hallucination(text, candidates)` が True → 主軸生テキストで上書き、`note='要確認:LLM過剰補填可能性'`、`halluc_rows += 1`
  3. それ以外は LLM 出力 `text` を採用、`note` も LLM 出力を採用
- どの場合も `sp_uncertain=True` なら末尾に `【要確認:話者】` を付記

**幻覚検出ロジック** (`_detect_hallucination`):
```python
def _detect_hallucination(text, candidates):
    norm = _normalize_for_compare(text)
    joined = ''.join(_normalize_for_compare(c) for c in candidates if c)
    if not norm or not joined or len(norm) < 4:
        return False
    for i in range(0, len(norm) - 3):
        if norm[i:i+4] not in joined:
            return True
    return False
```
- text を正規化（句読点・スペース・要確認タグ除去）
- 全候補（cand_ws / cand_run1 / cand_run2 / backbone_text）を結合正規化
- text の 4文字 sliding window で「候補連結に1つも含まれない 4-gram」があれば幻覚

**sources フィールド構築（Excel・管理画面互換維持）**:
- 各 final_seg について、`_cand_ws / _cand_run1 / _cand_run2` のうち、正規化テキストが最終テキストと異なるものを `sources['Web Speech'/'Run1'/'Run2']` に保存
- 一致するものは省略（管理画面が「差分あり」で色付け表示）
- 内部フィールド `_cand_*` は削除して result_json に出さない

**実装**: `merge_project` 本体内インライン処理。

---

## 4. LLM プロンプト設計

### 4.1 System Prompt
```
あなたは日本語会議録音の文字起こしを整流する編集者です。

あなたの仕事は、主軸ソースの行ごとのテキストに対して、他ソース（補完候補）を文脈として参照しながら以下のみを行うことです：
1. 主軸が拾い損ねた単語の補完
2. 主軸内で明らかに順序が崩れた語句の修正
3. 句読点とスペースの整形

絶対禁止事項：
- 行の追加・削除・統合・分割・並べ替え
- 候補テキストに存在しない語句の新規生成
- 主軸テキストの意味を変える書換え

判断に迷う場合は主軸テキストをそのまま返してください。誠実さが速度より大切です。
```

### 4.2 User Prompt（チャンク単位）
```
## 参加者
{participant_list}

## 補完優先度（同等候補から1つ選ぶ際の参考）
WebSpeech : Run1({engine1}) : Run2({engine2}) = {w_ws} : {w_run1} : {w_run2}

## 文脈（参照のみ・出力に含めない）
  [prev-1] [ts=...] [話者=...] 主軸: ...
  [prev-2] [ts=...] [話者=...] 主軸: ...

## 編集対象（必ず N 行を N 行で返す）
行001 [ts=00:00] [話者=参加者1]
    主軸(run1): おはようございます
    WS:   おはよございます
    Run1: おはようございます
    Run2: おはよう御座います

行002 [ts=00:09] [話者=未設定]
    主軸(run1): 脚本
    WS:   （なし）
    Run1: （なし）
    Run2: いです

...（N 行）

## 文脈（参照のみ・出力に含めない）
  [next-1] [ts=...] [話者=...] 主軸: ...
  [next-2] [ts=...] [話者=...] 主軸: ...

## 出力形式（JSONL、1行=1JSON、改行区切り、フェンス不要）
{"idx":1,"text":"...","note":null}
{"idx":2,"text":"...","note":"要確認の理由"}
...

※ 必ず N 行返してください。idx は 1〜N の連番厳守。
```

### 4.3 LLM 出力解析 (`_parse_jsonl`)
- JSONL を行単位パース。1行壊れていても他行は救う
- LLM が誤って JSON 配列で返した場合、`[ ... ]` 検出時はそちらでもパース試行
- パース成功 ≠ N 行 → そのチャンクは丸ごとフォールバック

```python
def _parse_jsonl(raw, expected_n):
    raw = strip_fence(raw)
    if raw.startswith('['):
        try:
            data = json.loads(raw)
            if isinstance(data, list) and len(data) == expected_n:
                return data
        except Exception: pass
    result = []
    for line in raw.split('\n'):
        line = line.strip().rstrip(',')
        if not line or line in ('[', ']'):
            continue
        try: result.append(json.loads(line))
        except Exception: continue
    return result if len(result) == expected_n else None
```

---

## 5. 設定パラメータ（DB `merge_settings` テーブル）

| キー | 型 | デフォルト | 範囲 | 説明 |
|---|---|---|---|---|
| `chunk_size` | int | 25 | 10–50 | LLM 1 リクエストあたりの行数 |
| `cluster_window_ms` | int | 2000 | 500–5000 | 主軸近傍判定の時間窓 |
| `orphan_sim_threshold` | float | 0.4 | 0.0–1.0 | オーファン除外の類似度閾値 |
| `backbone_algo` | enum | `fixed`（運用デフォルト） | `fixed`/`rows_x_log_chars`/`rows_only`/`chars_only` | 主軸選定方法。運用上は Run1 固定 |
| `backbone_fixed` | enum | `run1`（Whisper 固定・運用デフォルト） | `''`/`ws`/`run1`/`run2` | `backbone_algo=fixed` 時の主軸 |
| `default_model` | enum | `gemini-2.5-flash` | ALLOWED_MODELS | フロントエンドが model 指定省略時のデフォルト |
| `parallel` | int | 3 | 1–5 | LLM 並列呼出数 |
| `max_tokens_per_chunk` | int | 8192 | 4096–16384 | 各 LLM 呼出の max_tokens |
| `retry_per_chunk` | int | 1 | 0–3 | チャンク失敗時のリトライ回数 |

### 5.1 設定 API
- `GET /api/settings/merge` (Basic 認証) → 現在の設定をJSON返却
- `PUT /api/settings/merge` (Basic 認証) → 部分更新（送信したキーのみ変更）。範囲外は自動クランプ、enum 不一致はデフォルトへ。

### 5.2 管理画面 UI
歯車設定モーダルに「⚙ LLM統合詳細設定」セクションを新設:
- 主軸選定アルゴリズム（select）
- 固定主軸（select、algo=fixed 時のみ表示）
- デフォルトモデル（select）
- チャンクサイズ / 並列LLM呼出 / クラスタ統合窓 / オーファン類似度閾値 / max_tokens / リトライ回数（number input + min/max/step）
- 「デフォルト」「保存」ボタン
- 保存ステータス表示

---

## 6. 既存コード互換性

### 6.1 `merged_transcripts.result_json` 形状
- 不変: `[{ts, speaker, text, note, sources?}]`
- 追加フィールド無し
- 既存 Excel エクスポート / Q&A / 要約は無改修で動作

### 6.2 重みスライダー
- UI は残存。意味は「主軸選定の重み」→「LLM補完優先度ヒント」に変更
- localStorage キー（`merge_w_ws / merge_w_run1 / merge_w_run2`）はそのまま
- `merge_base` キーは廃止（保存はされなくなる）

### 6.3 マージ API
- `POST /api/projects/{id}/merge?model=&w_ws=&w_run1=&w_run2=` は維持
- `base` パラメータは後方互換のため受け取るが無視
- `model` 省略時は `merge_settings.default_model` を使用

### 6.4 後方互換
- 既存の merged_transcripts レコードはそのまま閲覧可能
- 再マージ実行で新ロジックの出力に置き換わる

---

## 7. 変更ファイル一覧

| ファイル | 変更内容 |
|---|---|
| `_db_remote.py`（新規・VPS db.py のローカル正典化） | `merge_settings` テーブル作成、初期値挿入 |
| `_main_remote.py` | `ALLOWED_MODELS` 維持、`MERGE_SETTINGS_*` 追加、ヘルパー関数群追加（`_get_merge_settings, _clamp_merge_settings, _normalize_for_compare, _text_sim, _select_backbone, _build_rows_with_orphans, _resolve_speaker_for_row, _chunk_rows, _detect_hallucination, _parse_jsonl, _call_gemini_chunk, _build_chunk_prompt, _make_segment`）、`merge_project` 全面書換え、`GET/PUT /api/settings/merge` 新設 |
| `_admin_remote.html` | 重みスライダー説明文変更、`merge-base` 関連 UI/JS 削除、`⚙ LLM統合詳細設定` セクション新設、`loadMergeAdvanced/saveMergeAdvanced/resetMergeAdvanced` 関数追加、`openApiSettings` から呼出 |

---

## 8. 検証手順

### 8.1 機能検証
1. **既存問題 MTG の再マージ**: `63fd7662-62c8-4253-9243-b693c368487a`
   - 旧出力: 15 行 / 末尾欠落 / 幻覚あり
   - 新出力: 141 行 / 末尾完結 / 幻覚 0
2. **行数とフィールド確認**:
   ```sql
   SELECT json_array_length(result_json), notes_json IS NULL,
          summary IS NULL, tasks_json IS NULL
   FROM merged_transcripts WHERE project_id='63fd7662-...';
   ```
3. **末尾欠落の解消**: result_json 末尾 5 行を grep で確認、`text` が空でないこと
4. **幻覚の有無**: 候補にない特定文字列（例:「9月10日午後9時頃」）が含まれないこと
5. **オーファン挿入の動作**: 主軸ソース行数 < 最終行数 になっていること
6. **管理画面 4 カラム比較ビュー**: 最終版カラムが全行埋まること
7. **Excel エクスポート**: 全行が時系列順、空セルなし

### 8.2 障害テスト
8. **LLM 障害シミュレーション**:
   - `.env` で GEMINI_API_KEY を一時的に無効値に
   - 再マージ
   - 期待: 全行 backbone 生テキスト、status=200、`fallback_chunks = チャンク総数`
9. **チャンク境界テスト**:
   - `PUT /api/settings/merge {"chunk_size":5}`
   - 再マージで境界での文脈断絶が起きないか目視
10. **設定範囲外**:
    - `PUT {"chunk_size":999}` → 50 に clamped されること
    - `PUT {"backbone_algo":"invalid"}` → `rows_x_log_chars` にフォールバック

### 8.3 長尺テスト
11. 30分以上の MTG（旧設計では truncation 必発）で末尾まで欠落なし

### 8.4 完了条件
- [ ] 問題 MTG で行数 ≥ 36（旧 15 から大幅増）
- [ ] 末尾 5 行の元発話がすべて新出力に含まれる
- [ ] 候補に無い 4 字以上連続文字列を含む行が 0
- [ ] LLM 無効化で結果が正常返却
- [ ] 設定画面の 9 項目すべて変更・保存可能
- [ ] Excel エクスポート / Q&A / 要約が無改修で動作
- [ ] CLAUDE.md / CONCEPT.md / system-overview.html / specs / plans が詳細更新済
- [ ] 本番 VPS にデプロイ済、本番検証完了

---

## 9. リスクと緩和策

| リスク | 影響 | 緩和策 |
|---|---|---|
| チャンク境界で語が途切れる | 文意が微妙に切れる | 前後 2 行を文脈として渡す。最悪でも欠落は発生しない |
| オーファン乱発で行数爆発 | UI 視認性低下 | 類似度閾値 0.4 + 広域 ±window×3 探索 + 隣接 500ms 連結で抑制 |
| 設定値が常識外 | 想定外動作 | バックエンドで範囲クランプ。フロントもバリデーション |
| 並列呼出で API rate limit | 一部チャンク失敗 | Semaphore で並列数制限。失敗は決定論フォールバックで補償 |
| 主軸選定の総合スコアが直感に合わない | 期待外の主軸 | `BACKBONE_ALGO=fixed` で明示指定可能 |
| LLM が JSONL ではなく JSON 配列を返す | パース失敗 | パーサーで両形式対応 |
| `merge_settings` テーブル未作成環境 | エラー | `_get_merge_settings` で空時はコードのデフォルト値返却 |
| 既存 result_json の sources 互換 | Excel UI 崩れ | `_normalize_for_compare` で同等なら省略し、相違ある場合のみ追加（既存仕様維持） |

---

## 10. 関連リソース

- 実装プラン: `docs/superpowers/plans/2026-06-05-llm-merge-redesign.md`
- 概念図: `CONCEPT.md` の「LLM統合」セクション
- システム全体: `docs/system-overview.html` のマージ処理フロー
- 実装の根本コミット予定: feature ブランチ `main` 上で直接コミット予定（既存運用に合わせる）
