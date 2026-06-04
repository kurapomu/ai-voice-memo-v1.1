# Zoom Marketplace 設定手順書（v1）

> Zoom 連携の運用開始までに必要な「ユーザー手作業」をまとめた手順書。
> 実装（コード）側は完了している前提。

最終更新: 2026-06-04

---

## 全体の流れ

```
[1] Zoom Pro 契約          ← クレカ登録
[2] Cloud Recording 設定    ← アカウント設定で 2 つチェック
[3] Marketplace でアプリ作成 ← Server-to-Server OAuth
[4] credentials を VPS の .env に貼る  ← Claude が代行可
[5] 動作確認テストMTG
```

---

## [1] Zoom Pro 以上の契約

| 項目 | 内容 |
|---|---|
| 対象プラン | **Pro 以上**（Free プランでは Cloud Recording 機能が使えない） |
| 料金（2026年時点） | 月額 ¥2,125 〜（年払い・税抜） |
| URL | https://zoom.us/jp-jp/pricing |

確認ポイント：
- 「**クラウドレコーディング**」が機能一覧に含まれていること

---

## [2] Cloud Recording 設定

Zoom 管理画面 → 「設定（管理）」 → 「レコーディング」タブ。

| 設定項目 | あるべき値 | 重要度 |
|---|---|---|
| クラウドレコーディング | **オン** | 必須 |
| **各参加者の音声ファイルを別々に記録**（Record a separate audio file for each participant） | **オン** | **必須**（これが OFF だと話者分離が動かない） |
| 自動レコーディング | お好みで（オンにすると録画忘れ防止） | 任意 |

⚠️ 「**各参加者の音声ファイルを別々に記録**」をONにしないと、話者別の `participant_audio_files` が生成されず、本連携の最大の旨味（完璧な話者分離）が失われます。

---

## [3] Marketplace で Server-to-Server OAuth アプリを作成

### 3-1. アクセス

https://marketplace.zoom.us/develop/create

- 右上から自分の Zoom アカウントでサインイン
- 「**Develop**」→「**Build App**」

### 3-2. アプリ種別の選択

「**Server-to-Server OAuth**」を選択。

| ⚠️ 注意 | 「OAuth」「Webhook Only」「Chatbot」など他の種別を選ばないこと |

### 3-3. 基本情報

| 項目 | 値 |
|---|---|
| App Name | `AI Voice Memo - Jizo` |
| Short Description | 対面録音 + Zoom連携用 |
| Long Description | （任意） |
| Company Name | 自社名 |
| Developer Contact Name | 自分の名前 |
| Developer Contact Email | 自分のメール |

### 3-4. App Credentials の取得（重要）

「**App Credentials**」タブで以下 3 つを取得してメモ：

| 項目 | VPS 環境変数名 |
|---|---|
| Account ID | `ZOOM_ACCOUNT_ID` |
| Client ID | `ZOOM_CLIENT_ID` |
| Client Secret | `ZOOM_CLIENT_SECRET` |

⚠️ Client Secret は表示が一度きりの場合があるので、必ずメモを取ること。

### 3-5. Scopes（権限）の設定

「**Scopes**」タブで以下 3 つを追加：

| Scope | 用途 |
|---|---|
| `recording:read:admin` | 録画ファイルの取得 |
| `meeting:read:admin` | MTG メタ情報の取得 |
| `user:read:admin` | 参加者情報の取得 |

⚠️ 多すぎず少なすぎず。**この3つだけ**。

### 3-6. Feature（Event Subscription）

「**Feature**」タブで Event Subscriptions を有効化。

#### Webhook URL の登録

| 項目 | 値 |
|---|---|
| Endpoint URL | `https://jizo-dev.com/api/zoom/webhook` |
| Secret Token | （自動生成または自分で設定） |

⚠️ **重要**：Endpoint URL の検証は **VPS 側に Webhook 受信エンドポイントがデプロイ済みでないと失敗**します。事前に Task 15（デプロイ）を完了させておくこと。

Secret Token は VPS の環境変数名で `ZOOM_WEBHOOK_SECRET` として保存。

#### Event types

「**Add Events**」→「**Recording**」配下から：

- ✅ `Recording is completed`（recording.completed）

これだけ追加。**他のイベントは追加しない**（不要な通知でログが汚れる）。

### 3-7. Activation

「**Activation**」タブで「**Activate your app**」をクリック。

⚠️ アカウント管理者でない場合、別途承認フローが発生します。

---

## [4] credentials を VPS の `.env` に貼る

取得した 4 つを **VPS `/opt/jizo-api/.env`** に追記：

```bash
ZOOM_CLIENT_ID=（3-4 で取得）
ZOOM_CLIENT_SECRET=（3-4 で取得）
ZOOM_ACCOUNT_ID=（3-4 で取得）
ZOOM_WEBHOOK_SECRET=（3-6 の Secret Token）
```

その後：
```bash
ssh root@162.43.14.31 "systemctl restart jizo-api"
```

⚠️ **この `.env` 編集はセキュリティ的に Claude に任せて OK**（credentials は会話に貼り付けてもらえれば私が SSH で書き込みます）。

---

## [5] 動作確認

### 5-1. Webhook URL Validation の確認

Marketplace のアプリ画面で「**Verify**」ボタンを押す（Endpoint URL 入力欄の隣）。
→ 緑色の `Verified` が出れば成功。

失敗する場合：
- VPS の `jizo-api` サービスが起動しているか確認
- `curl -s https://jizo-dev.com/api/health` で疎通確認
- `ssh root@162.43.14.31 "journalctl -u jizo-api -n 50 --no-pager"` でログ確認

### 5-2. テスト MTG（自分 1 人）

1. Zoom デスクトップから新規 MTG 開始（自分のみ）
2. 「クラウドに録画」を押す
3. 30 秒ほど話して MTG 終了
4. 数分〜数十分待つ（Zoom 側で録画ファイル処理）
5. PC 管理画面に **「Zoom 取り込み待ち 1 件」** が出る
6. 「取り込む」を押す
7. 既存の解析ボタン（Run1）で文字起こしを実行
8. 自分の発言が話者ラベル付きで表示される

### 5-3. 2 人 MTG での話者分離確認

別アカウントの人を招待して 2 〜 3 人 MTG を実施 → 取り込み → 解析。
**各人の発言が個別の話者ラベルで分かれていれば成功**。

---

## トラブルシューティング

| 症状 | 原因 | 対応 |
|---|---|---|
| Webhook URL Verification 失敗 | VPS 未デプロイ / 鍵未設定 | Task 15 完了 + `.env` 確認 |
| Webhook 着弾しない | Scope/Event 設定漏れ | 3-5・3-6 を再確認 |
| `participant_audio_files` が空 | アカウント設定 [2] が OFF | [2] を ON にして再録画 |
| `invalid signature` ログ多発 | `ZOOM_WEBHOOK_SECRET` 不一致 | Marketplace の Secret Token と `.env` を再同期 |
| 取り込みは成功するが話者分離されない | 同上 + 設定後の MTG で再テスト | 設定変更前の MTG は仕様上分離不可 |

---

## 既知の制約

- 録画ボタンを押し忘れた MTG は検知不可
- Zoom 無料プランでは利用不可
- DL 中は VPS に一時的にディスク数 GB 消費
- Whisper コストが**参加者数に比例**（10 人 MTG = 10 倍の API 呼び出し）
- 他組織主催 MTG は Webhook 自動取り込みの対象外 → 録画ファイルをダウンロードして「他組織MTGをファイルアップロード」で取り込む
