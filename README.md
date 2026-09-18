# 🗺️ Area Spatial Scout - Cloud Run (完全クラウド稼働版)

> **Macの電源が切れていても、スリープ（オフライン）でも大丈夫！**  
> スマートフォンの専用画面から「エリア」と「テーマ」を入力するだけで、Google Cloud 上の自律AIがリサーチ ➔ 国土地理院地図合成 ➔ デュアルWord（PC用A4 ＆ スマホ用横スクロールゼロ） ➔ CSV台帳を作成し、あなたの **Google ドライブへ全自動で直接納品** します。

---

## 🌟 特徴とメリット

1. **Mac不要・完全サーバーレス稼働**:
   - 自宅やオフィスのMacを開く必要は一切ありません。
   - 移動中の電車やカフェから、スマホ1台で指示が完了します。
2. **完全無料枠内で運用可能**:
   - Google Cloud Run は毎月200万リクエスト、CPU/メモリ無料枠が膨大に用意されているため、**月額費用は0円**で稼働できます。
3. **最新AI自律検索 (Gemini with Google Search Grounding)**:
   - 食べログ・Googleマップ・公式サイトをリアルタイムで横断照合し、「完全個室あり」「口コミ8件」「詳細料金体系」を自動抽出。
4. **黄金基準レポート ＆ スマート地図合成**:
   - 国土地理院公的タイル × スマート自動衝突回避アルゴリズムにより、密集地でも重なりゼロの広域地図を自動生成。
   - PC印刷用A4カルテ（全12pt以上）と、スマホ閲覧専用Word（横スクロール完全ゼロ）を両方同時生成。
5. **Google ドライブ直接保存**:
   - 指定のGoogle ドライブフォルダ（`マイドライブ/05_ワード出力/04_コンサル・企業調査/エリア空間調査/`）へ自動で新規フォルダを作成し、ファイル一式を直接アップロード。

---

## 🚀 初回デプロイ手順（所要時間: 約5分・ブラウザだけで完結）

Macに Docker や gcloud コマンドがインストールされていなくても、**Google公式のブラウザ上ターミナル（Google Cloud Shell）** を使うことで、誰でも一撃でデプロイできます。

### ステップ 1: Google Cloud Shell を開く
1. ブラウザで [Google Cloud コンソール](https://console.cloud.google.com/) にアクセスします。
2. 画面右上にある **「Cloud Shell をアクティブにする」アイコン（`>_` のマーク）** をクリックしてターミナルを起動します。

### ステップ 2: デプロイスクリプトの実行
Cloud Shell の画面に以下のコマンドを貼り付けて実行します：

```bash
# 本リポジトリの cloud_run_scout ディレクトリをアップロード、または直接デプロイ
cd cloud_run_scout
chmod +x deploy.sh
./deploy.sh
```

- スクリプトが自動で必要なAPIの有効化、コンテナビルド、Cloud Runへのデプロイを行います。
- 途中で **Gemini API キー** の入力を求められたら、[Google AI Studio](https://aistudio.google.com/app/apikey) で取得した無料キーを入力してください。

### ステップ 3: Google ドライブへの権限付与（超重要！）
Cloud Run からあなたのGoogleドライブに直接ファイルを書き込めるように、サービスアカウントを招待します：

1. デプロイ時に作成されるサービスアカウントのメールアドレス（例: `area-spatial-scout@<プロジェクトID>.iam.gserviceaccount.com`）をコピーします。
2. Google ドライブの納品先フォルダ（[エリア空間調査フォルダ](https://drive.google.com/drive/folders/1ox9MRqdGRbfOIeEZAMWpQ_RbIupcn9dH?usp=sharing)）をブラウザで開きます。
3. フォルダの「共有」ボタンを押し、上記サービスアカウントのメールアドレスを **「編集者」** として追加します。

---

## 📱 スマートフォンでの使い方

1. デプロイ完了時にターミナルに表示された **専用URL**（例: `https://area-spatial-scout-xxxxx-an.a.run.app`）をスマートフォンの Safari または Chrome で開きます。
2. ブラウザの共有メニューから **「ホーム画面に追加」** をタップすると、スマートフォンのホーム画面にアプリアイコンが登録されます。
3. **あとはエリアとテーマを入力するだけ！**
   - 例: エリア「銀座」、テーマ「個室接待鮨」
   - 「生成開始」をタップすると、約1〜2分で処理が完了し、**「Google ドライブで開く」** ボタンが表示されます。
   - タップすれば、スマホの Google ドキュメントアプリで出来立てのレポートをすぐに確認できます！

---

## 📂 ファイル構成

```
cloud_run_scout/
  ├── app.py                 # FastAPI メインアプリケーション (Web UI & API)
  ├── research_engine.py     # Gemini API 自律検索グラウンディングエンジン
  ├── report_engine.py       # Linux/コンテナ対応 地図合成 & デュアルWord生成
  ├── drive_uploader.py      # Google Drive API v3 直接アップローダー
  ├── templates/
  │   └── index.html         # スマホ専用レスポンシブ Web UI
  ├── Dockerfile             # 日本語フォント (Noto Sans CJK JP) 搭載コンテナ定義
  ├── requirements.txt       # 必要パッケージ定義
  ├── deploy.sh              # 一撃自動デプロイスクリプト
  └── README.md              # 本マニュアル
```
