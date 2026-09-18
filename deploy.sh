#!/bin/bash
set -e

# ==============================================================================
# Area Spatial Scout - Google Cloud Run 自動デプロイスクリプト
# ==============================================================================

SERVICE_NAME="area-spatial-scout"
REGION="asia-northeast1" # 東京リージョン

echo "=========================================================="
echo "🚀 Area Spatial Scout: Cloud Run への自動デプロイを開始します"
echo "=========================================================="

# 1. gcloud コマンドの確認
if ! command -v gcloud &> /dev/null; then
    echo "❌ エラー: gcloud コマンドが見つかりません。"
    echo "Google Cloud Shell（ブラウザ上の無料ターミナル）で実行するか、gcloud CLI をインストールしてください。"
    exit 1
fi

PROJECT_ID=$(gcloud config get-value project 2>/dev/null)
if [ -z "$PROJECT_ID" ]; then
    echo "❌ エラー: GCP プロジェクトが選択されていません。"
    echo "gcloud config set project <YOUR_PROJECT_ID> を実行してください。"
    exit 1
fi

# もしプロジェクト番号（数字のみ）だった場合、正式な projectId に自動変換
if [[ "$PROJECT_ID" =~ ^[0-9]+$ ]]; then
    echo "ℹ️ プロジェクト番号 ($PROJECT_ID) からプロジェクトIDを照会しています..."
    PROJECT_ID=$(gcloud projects describe "$PROJECT_ID" --format="value(projectId)")
    gcloud config set project "$PROJECT_ID"
fi

echo "✅ 対象プロジェクト: $PROJECT_ID"
echo "✅ リージョン: $REGION"

# 2. 必要な API の有効化
echo "📦 必要な API を有効化しています (Cloud Run, Cloud Build, Drive API)..."
gcloud services enable run.googleapis.com cloudbuild.googleapis.com drive.googleapis.com --project="$PROJECT_ID"

# 3. 環境変数の設定確認
if [ -z "$GEMINI_API_KEY" ]; then
    # 既存のCloud RunサービスからGEMINI_API_KEYを照会
    EXISTING_KEY=$(gcloud run services describe "$SERVICE_NAME" --region="$REGION" --project="$PROJECT_ID" --format='value(spec.template.spec.containers[0].env[0].value)' 2>/dev/null || true)
    if [ -n "$EXISTING_KEY" ]; then
        echo "✅ 既存の Cloud Run から Gemini API キーを自動取得しました"
        GEMINI_API_KEY="$EXISTING_KEY"
    else
        echo "----------------------------------------------------------"
        echo "⚠️  GEMINI_API_KEY が環境変数に設定されていません。"
        read -p "Google AI Studio の Gemini API キーを入力してください: " INPUT_GEMINI_KEY
        GEMINI_API_KEY=$INPUT_GEMINI_KEY
    fi
fi

# パスワード（任意）
if [ -z "$SCOUT_PASSWORD" ]; then
    SCOUT_PASSWORD=""
fi

# 4. Cloud Run へのソースコード直接ビルド＆デプロイ
echo "🚀 コンテナのビルドおよび Cloud Run へのデプロイを実行中..."
gcloud run deploy "$SERVICE_NAME" \
    --source . \
    --region "$REGION" \
    --platform managed \
    --allow-unauthenticated \
    --memory 1Gi \
    --cpu 1 \
    --timeout 300 \
    --min-instances 0 \
    --max-instances 3 \
    --set-env-vars "GEMINI_API_KEY=$GEMINI_API_KEY,SCOUT_PASSWORD=$SCOUT_PASSWORD" \
    --project "$PROJECT_ID"

# 5. デプロイ完了URLの取得
SERVICE_URL=$(gcloud run services describe "$SERVICE_NAME" --platform managed --region "$REGION" --format 'value(status.url)' --project "$PROJECT_ID")

echo "=========================================================="
echo "🎉 デプロイが完了しました！"
echo "🌐 あなたの専用スマホURL: $SERVICE_URL"
echo "=========================================================="
echo "📱 【スマホでの使い方】"
echo "1. 上記 URL をスマートフォンのブラウザ（Safari / Chrome）で開く"
echo "2. 「ホーム画面に追加」をタップすると、アプリアイコンとして登録されます"
echo "3. あとは「銀座 個室接待鮨」等と入力して送信するだけで、Macがオフラインでも"
echo "   Google ドライブへ自動でレポートが納品されます！"
echo "=========================================================="
