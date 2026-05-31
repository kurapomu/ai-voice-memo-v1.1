# AIボイスメモ PWA — CLAUDE.md

## プロジェクト概要

対面録音特化のAIボイスメモPWA。専用ハードウェア不要で、iPhoneのSafariからホーム画面に追加して利用する。
Web Speech APIによるリアルタイム文字起こしをコアとし、将来的にAssemblyAI等の高精度AIと二段階で突合する設計。

---

## 技術スタック

| 層 | 技術 | 備考 |
|---|---|---|
| フロントエンド | HTML / CSS / JavaScript（PWA） | React不使用。VanillaJS徹底 |
| STT（現在） | Web Speech API（ブラウザ標準） | 無料・追加サーバー不要 |
| STT（将来） | AssemblyAI API | 話者分離・正確なタイムスタンプ対応 |
| 要約・Q&A（将来） | Claude API（Haiku優先） | プロンプトキャッシュ設計を組み込む |
| 音声保存 | MediaRecorder API → IndexedDB | 32kbps webm形式 |
| バックエンド（将来） | Python / FastAPI | PII保護（Presidio + GiNZA）と連携 |
| デプロイ | GitHub Pages（現在）→ レンタルサーバー/VPS（将来） | HTTPS必須 |

---

## バージョン履歴

### v1（完成・凍結）
- 対面録音（MediaRecorder API）
- リアルタイム文字起こし（Web Speech API）
- ハイライト★ボタン（タイムスタンプ記録）
- 全文コピー
- PWAマニフェスト・Service Worker

### v1.1（現行開発版）
- 参加者名事前入力（最大5人）
- 話者切替ボタン（録音中にタップ、カラー表示）
- interimセグメントへのリアルタイム話者反映
- 名乗り検出：「鈴木です」→ `[鈴木]` に自動変換（登録名に限定・セグメント全体を対象）
- 録音音声の保存（32kbps webm・MediaRecorder）
- 結果画面：セグメントごとのプルダウン話者変更・テキスト直接編集
- 音声プレイヤー（聴き直し）
- IndexedDB保存・履歴一覧・削除・再読込

---

## ファイル構成

```
ai-voice-memo/        ← v1（GitHub Pages公開済み・凍結）
├── index.html
├── manifest.json
└── sw.js

ai-voice-memo-v1.1/   ← v1.1（別リポジトリ）
├── index.html
├── manifest.json
└── sw.js
```

---

## 設計方針

### STT差し替えポイント
`createSTT()` 関数のみを差し替えることで、Web Speech API → Whisper / AssemblyAI への移行が可能。
UIロジック・録音処理・IndexedDB保存には一切手を入れない。

### 二段階方式（話者分離）
```
【リアルタイム層：Web Speech（参考データ）】
  録音中に文字起こし＋話者ボタン操作で話者ラベルを付与
  ↓
【後処理層：高精度AI（本番データ）】
  保存した録音音声を AssemblyAI 等に送信
  Web Speechの結果を補助データとして突合し、話者分離と精度を向上
```

### IndexedDB 保存スキーマ
```javascript
{
  id,            // 自動採番
  createdAt,     // ISO8601
  duration,      // "MM:SS"
  participants,  // ['佐藤', '鈴木', ...]
  segments: [{
    text,        // 文字起こしテキスト
    speakerIdx,  // 話者インデックス（null=未設定）
    ts,          // タイムスタンプ "MM:SS"
    highlight,   // ハイライトフラグ
    isInterim,   // 確定フラグ
  }],
  audioMime,     // "audio/webm;codecs=opus"
  audioData,     // ArrayBuffer（録音音声）
}
```

### 話者ボタン押下時の挙動
- 押下瞬間：現在生成中の interim セグメントの `speakerIdx` を即時更新
- 録音終了後：セグメント冒頭に話者バッジとして補正表示
- 参考データのため、若干のズレは許容する

### 名乗り検出ルール
- 対象：登録した参加者名に限定（誤検出防止）
- 検出範囲：セグメント全体（発言のどこにあっても検出）
- パターン：`{名前}です` → `[{名前}]` に変換
- 実行タイミング：録音終了後の後処理（`applyNameDeclaration()`）

---

## コスト設計

| サービス | 単価 | 月100分想定 |
|---|---|---|
| Web Speech API | 無料 | 0円 |
| AssemblyAI（文字起こし＋話者分離） | $0.00283/分 | 約42円 |
| Claude API Haiku（要約・Q&A） | 従量 | 月1ドル未満 |
| 合計（将来構成） | — | 約150〜300円 |

- AssemblyAIの無料クレジット：$50（約185時間分）→PoC期間は実質無料
- プロンプトキャッシュで要約コストを最大90%削減する設計を将来フェーズで組み込む

---

## 将来フェーズ

### フェーズ2（次回）
- AssemblyAI連携（録音→自動話者分離）
- FastAPIバックエンド構築
- 音声の一時保管（S3等）→ AssemblyAIへURL渡し
- Web Speech結果との突合処理

### フェーズ3
- Claude API連携（要約・ToDo抽出・Q&A）
- 用途別テンプレート（会議・インタビュー・講義）
- Presidio + GiNZA によるPII保護

### フェーズ4
- VPS/レンタルサーバーへの本番移設
- プライバシーポリシー整備・公開

---

## 制約・注意事項

- **HTTPS必須**：Web Speech API・マイクアクセスともにHTTPSでのみ動作（localhostは例外）
- **iOS Safari固有**：バックグラウンド録音は停止しやすい。STT自動再起動ロジックで対処（`onEnd`時に300ms後に再起動）
- **iOS 7日間削除**：PWAとしてホーム画面に追加することで緩和。長期保存は将来のバックエンドで対応
- **通話録音は対象外**：iOSはウェブ・アプリを問わず通話音声へのアクセス不可
- **フォントサイズ**：16px基準・4の倍数のみ使用（16/20/24/28/32px）。16px未満は全禁止
- **成果物に個人名・法人名を含めない**

---

## 開発・テスト環境

- MacBook + VS Code
- ローカルテスト：`python3 -m http.server 5500` → `http://localhost:5500`
- 公開：GitHub Pages（HTTPS自動・無料）
- Android Chrome でも追加作業なしで動作確認済み
