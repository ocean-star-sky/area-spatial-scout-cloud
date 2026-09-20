# 🗺️ Area Spatial Scout - Cloud Run

スマートフォンから「エリア」と「テーマ」を入力すると、Google Cloud 上のサービスが
**Gemini + Google 検索グラウンディング**で実在施設を調査し、国土地理院タイルのプロット地図・
PC 印刷用 Word・スマホ閲覧用 Word・CSV 台帳・一括 ZIP を生成します。
共有ドライブを設定していれば、成果物一式をそこへ納品します。

---

## 🌟 できること

1. **Mac 不要・サーバーレス稼働** — スマホのブラウザだけで指示から受け取りまで完結します。
2. **Google 検索グラウンディング付き調査** — Gemini が実際に Web 検索した結果に基づいて
   施設名・住所・料金・クチコミを集めます。**実行した検索クエリと、API が返した出典 URL は
   レポート巻末に列挙**されます（出典 URL は応答に含まれないことがあり、その場合は
   検索クエリのみが記載されます）。
3. **調査できなかったことは、できなかったと返す** — API のクォータ切れや該当なしのときに、
   それらしい架空のスポットや創作クチコミでレポートを埋めることはしません。失敗として返します。
   検索で確認できなかった個別項目も、推測で埋めずに空欄にします。
4. **デュアル Word** — PC 印刷用 A4（全 12pt 以上）と、スマホ用の横スクロールゼロ 1 カラム版を
   同時に生成します。
5. **重なりを避ける広域地図** — 国土地理院標準地図タイル上にピンとラベルを自動配置します。

### 制限事項

- 調査品質は Gemini の検索結果に依存します。掲載内容（特に料金・営業情報）は
  **一次情報での確認を前提**としてください。
- Gemini API の無料枠は**モデル単位の 1 日あたりリクエスト数**で制限されます。
  枯渇すると別モデルへ自動で切り替えますが、すべて枯渇した場合は調査が失敗します。
- 施設写真は、調査結果が実在する画像 URL を返し、かつ取得に成功した場合のみ掲載します。
  取得できなければ写真欄ごと省略します（無関係なストック写真は使いません）。

---

## 🚀 デプロイ手順（Google Cloud Shell だけで完結）

### ステップ 1: Cloud Shell を開く
[Google Cloud コンソール](https://console.cloud.google.com/) 右上の
**「Cloud Shell をアクティブにする」（`>_`）** をクリックします。

### ステップ 2: リポジトリを取得してデプロイ

```bash
git clone https://github.com/ocean-star-sky/area-spatial-scout-cloud.git
cd area-spatial-scout-cloud
chmod +x deploy.sh
./deploy.sh
```

スクリプトが以下を自動で行います。

- 必要な API の有効化（Cloud Run / Cloud Build / Drive / Secret Manager / Artifact Registry）
- 専用サービスアカウント `area-spatial-scout@<プロジェクトID>.iam.gserviceaccount.com` の作成
- **Secret Manager** への Gemini API キーとアクセスキーの登録（平文の環境変数には置きません）
- コンテナのビルドと Cloud Run へのデプロイ

Gemini API キーは [Google AI Studio](https://aistudio.google.com/app/apikey) で取得できます。
アクセスキーを指定しなかった場合は自動生成され、実行ログに 1 度だけ表示されます。控えてください。

> **アクセスキーは必須です。** 未設定のままではサービスはすべてのリクエストを拒否します
> （インターネットに公開された状態で、誰でも API クォータを消費できてしまうのを防ぐため）。

再デプロイ時にキーを更新したい場合:

```bash
GEMINI_API_KEY=xxxx SCOUT_PASSWORD=yyyy ./deploy.sh
```

### ステップ 3: Google ドライブ納品を使う場合（任意）

**納品先フォルダは必ず「共有ドライブ」に作ってください。**
サービスアカウントはマイドライブに保存容量を持たないため、マイドライブ配下のフォルダを
共有しても `storageQuotaExceeded` で失敗します。

1. 共有ドライブ内に納品先フォルダを作成する
2. そのフォルダに `area-spatial-scout@<プロジェクトID>.iam.gserviceaccount.com` を
   **「コンテンツ管理者」** として追加する
3. フォルダ ID を指定して再デプロイする

```bash
DRIVE_PARENT_FOLDER_ID=<フォルダID> ./deploy.sh
```

書き込めるかを事前に確認するには（ローカル、要認証情報）:

```bash
DRIVE_PARENT_FOLDER_ID=<フォルダID> python3 drive_uploader.py
```

この確認は**実際にテストフォルダを作成して削除**します。「親フォルダが読める」だけでは
サービスアカウントの容量制限を検知できないためです。

`DRIVE_PARENT_FOLDER_ID` を設定しない場合、ドライブ納品は無効のまま起動し、
成果物は ZIP / Word の直接ダウンロードで取得できます。

---

## 📱 スマートフォンでの使い方

1. デプロイ完了時に表示される URL を Safari / Chrome で開きます。
2. 共有メニューの **「ホーム画面に追加」** でアプリのように起動できます。
3. エリア・テーマ・アクセスキーを入力して「生成開始」をタップします。
   - 例: エリア「銀座」、テーマ「個室接待鮨」
   - Web 検索を伴うため **30 秒〜2 分** かかります。**この画面を閉じると処理は中断されます。**
4. 完了すると、Word / ZIP / CSV / 地図のダウンロードボタンが表示されます。
   共有ドライブ納品に成功していれば「Google ドライブで開く」も表示されます。

---

## ⚙️ 環境変数

| 変数 | 必須 | 内容 |
|---|---|---|
| `GEMINI_API_KEY` | ✅ | Gemini API キー（Secret Manager 経由で注入） |
| `SCOUT_PASSWORD` | ✅ | アクセスキー。未設定時は全 API を 503 で拒否 |
| `DRIVE_PARENT_FOLDER_ID` | － | 共有ドライブ内の納品先フォルダ ID。未設定ならドライブ納品を行わない |
| `SCOUT_DEBUG_ENABLED` | － | `1` のとき診断 API `/api/scout/debug` を有効化（Gemini を消費します） |

## 🔌 API

| メソッド | パス | 認証 | 内容 |
|---|---|---|---|
| `GET` | `/health` | 不要 | ヘルスチェック |
| `POST` | `/api/scout/instant` | アクセスキー | 調査 → 成果物生成 → ドライブ納品 |
| `GET` | `/api/scout/jobs` | アクセスキー | 直近ジョブ一覧 |
| `GET` | `/api/download/{job_id}/{type}` | ジョブ別トークン | 成果物の配信 |

`type` は `map` / `mobile_docx` / `pc_docx` / `csv` / `zip`。
ダウンロード URL は `/api/scout/instant` のレスポンスに署名付きで含まれます。

---

## 🧪 開発

```bash
pip install -r requirements-dev.txt
ruff check .
pytest -q
```

テストはネットワークにも Gemini API にもアクセスしません。
`tests/test_security.py` と `tests/test_deploy_hygiene.py` は、過去に実機で再現した
不具合（パストラバーサル・認証欠落・古いソースを埋め込んだデプロイスクリプト）を
そのまま固定しています。

ローカル起動:

```bash
SCOUT_PASSWORD=local-key GEMINI_API_KEY=xxxx uvicorn app:app --port 8080
```

## 📂 ファイル構成

```
area-spatial-scout-cloud/
  ├── app.py                 # FastAPI アプリ (Web UI & API)
  ├── research_engine.py     # Gemini + Google 検索グラウンディング調査
  ├── report_engine.py       # 地図合成 & デュアル Word / CSV 生成
  ├── drive_uploader.py      # 共有ドライブへの納品
  ├── templates/index.html   # スマホ向け Web UI
  ├── tests/                 # 回帰テスト
  ├── Dockerfile             # 日本語フォント入りコンテナ定義
  ├── deploy.sh              # Cloud Run デプロイスクリプト
  └── requirements.txt
```
