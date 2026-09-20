#!/bin/bash
set -euo pipefail

# ==============================================================================
# Area Spatial Scout - Google Cloud Run 自動デプロイスクリプト
#
# 秘密情報 (Gemini APIキー / アクセスキー) は Secret Manager に置き、
# Cloud Run には --set-secrets で渡す。平文の環境変数には入れない。
# ==============================================================================

SERVICE_NAME="${SERVICE_NAME:-area-spatial-scout}"
REGION="${REGION:-asia-northeast1}"
SA_NAME="${SA_NAME:-area-spatial-scout}"
GEMINI_SECRET="${GEMINI_SECRET:-gemini-api-key}"
PASSWORD_SECRET="${PASSWORD_SECRET:-scout-password}"
TOKEN_SECRET="${TOKEN_SECRET:-scout-token-secret}"

echo "=========================================================="
echo "🚀 Area Spatial Scout: Cloud Run へのデプロイを開始します"
echo "=========================================================="

if ! command -v gcloud &> /dev/null; then
    echo "❌ gcloud コマンドが見つかりません。Google Cloud Shell で実行してください。"
    exit 1
fi

PROJECT_ID=$(gcloud config get-value project 2>/dev/null || true)
if [ -z "$PROJECT_ID" ]; then
    echo "❌ GCP プロジェクトが未選択です。gcloud config set project <PROJECT_ID> を実行してください。"
    exit 1
fi
if [[ "$PROJECT_ID" =~ ^[0-9]+$ ]]; then
    PROJECT_ID=$(gcloud projects describe "$PROJECT_ID" --format="value(projectId)")
    gcloud config set project "$PROJECT_ID"
fi
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

echo "✅ プロジェクト: $PROJECT_ID / リージョン: $REGION"

echo "📦 必要な API を有効化しています..."
gcloud services enable \
    run.googleapis.com cloudbuild.googleapis.com drive.googleapis.com \
    secretmanager.googleapis.com artifactregistry.googleapis.com \
    --project="$PROJECT_ID"

# ---------------------------------------------------------------- サービスアカウント
if ! gcloud iam service-accounts describe "$SA_EMAIL" --project="$PROJECT_ID" &>/dev/null; then
    echo "👤 サービスアカウントを作成します: $SA_EMAIL"
    gcloud iam service-accounts create "$SA_NAME" \
        --display-name="Area Spatial Scout" --project="$PROJECT_ID"
else
    echo "👤 既存のサービスアカウントを使用します: $SA_EMAIL"
fi

# ---------------------------------------------------------------- シークレット
ensure_secret() {
    local name="$1" value="$2"
    if ! gcloud secrets describe "$name" --project="$PROJECT_ID" &>/dev/null; then
        gcloud secrets create "$name" --replication-policy=automatic --project="$PROJECT_ID"
    fi
    if [ -n "$value" ]; then
        printf '%s' "$value" | gcloud secrets versions add "$name" --data-file=- --project="$PROJECT_ID" >/dev/null
        echo "🔐 $name を更新しました"
    fi
    if ! gcloud secrets versions describe latest --secret="$name" --project="$PROJECT_ID" &>/dev/null; then
        echo "❌ シークレット '$name' に値がありません。環境変数を設定して再実行してください。"
        exit 1
    fi
    gcloud secrets add-iam-policy-binding "$name" \
        --member="serviceAccount:${SA_EMAIL}" --role="roles/secretmanager.secretAccessor" \
        --project="$PROJECT_ID" >/dev/null
}

# Gemini APIキー: 環境変数があれば新バージョンを追加、無ければ既存を使う
if [ -z "${GEMINI_API_KEY:-}" ] && ! gcloud secrets describe "$GEMINI_SECRET" --project="$PROJECT_ID" &>/dev/null; then
    read -r -s -p "Gemini API キーを入力してください (https://aistudio.google.com/app/apikey): " GEMINI_API_KEY
    echo
fi
ensure_secret "$GEMINI_SECRET" "${GEMINI_API_KEY:-}"

# アクセスキー: 未設定かつ未作成なら自動生成して表示する (認証なし公開を避けるため必須)
if [ -z "${SCOUT_PASSWORD:-}" ] && ! gcloud secrets describe "$PASSWORD_SECRET" --project="$PROJECT_ID" &>/dev/null; then
    SCOUT_PASSWORD=$(python3 -c "import secrets; print(secrets.token_urlsafe(12))")
    echo "🔑 アクセスキーを自動生成しました: $SCOUT_PASSWORD"
    echo "   （スマホの「アクセスキー」欄に入力します。控えてください）"
fi
ensure_secret "$PASSWORD_SECRET" "${SCOUT_PASSWORD:-}"

# ダウンロードトークンの署名鍵: アクセスキーとは別の秘密を必ず持たせる
if [ -z "${SCOUT_TOKEN_SECRET:-}" ] && ! gcloud secrets describe "$TOKEN_SECRET" --project="$PROJECT_ID" &>/dev/null; then
    SCOUT_TOKEN_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
    echo "🔐 ダウンロードトークン署名鍵を自動生成しました（表示しません）"
fi
ensure_secret "$TOKEN_SECRET" "${SCOUT_TOKEN_SECRET:-}"

# ---------------------------------------------------------------- 納品先
DRIVE_PARENT_FOLDER_ID="${DRIVE_PARENT_FOLDER_ID:-}"
if [ -z "$DRIVE_PARENT_FOLDER_ID" ]; then
    EXISTING_FOLDER=$(gcloud run services describe "$SERVICE_NAME" --region="$REGION" --project="$PROJECT_ID" \
        --format='value(spec.template.spec.containers[0].env.filter("name:DRIVE_PARENT_FOLDER_ID").extract("value"))' 2>/dev/null || true)
    DRIVE_PARENT_FOLDER_ID="${EXISTING_FOLDER//[\[\]\']/}"
fi
if [ -z "$DRIVE_PARENT_FOLDER_ID" ]; then
    echo "ℹ️  DRIVE_PARENT_FOLDER_ID が未設定のため、Google ドライブ納品は無効で起動します"
    echo "   （成果物は ZIP / Word の直接ダウンロードで取得できます）"
fi

# ---------------------------------------------------------------- デプロイ
echo "🚀 ビルドおよびデプロイを実行中..."
gcloud run deploy "$SERVICE_NAME" \
    --source . \
    --region "$REGION" \
    --platform managed \
    --allow-unauthenticated \
    --service-account "$SA_EMAIL" \
    --memory 2Gi \
    --cpu 1 \
    --timeout 600 \
    --min-instances 0 \
    --max-instances 10 \
    --concurrency 20 \
    --set-secrets "GEMINI_API_KEY=${GEMINI_SECRET}:latest,SCOUT_PASSWORD=${PASSWORD_SECRET}:latest,SCOUT_TOKEN_SECRET=${TOKEN_SECRET}:latest" \
    --set-env-vars "DRIVE_PARENT_FOLDER_ID=${DRIVE_PARENT_FOLDER_ID}" \
    --project "$PROJECT_ID"

SERVICE_URL=$(gcloud run services describe "$SERVICE_NAME" --platform managed --region "$REGION" \
    --format 'value(status.url)' --project "$PROJECT_ID")

echo "=========================================================="
echo "🎉 デプロイ完了"
echo "🌐 URL: $SERVICE_URL"
echo "👤 サービスアカウント: $SA_EMAIL"
echo "----------------------------------------------------------"
echo "📂 Google ドライブ納品を使う場合（初回のみ）:"
echo "   1. 納品先フォルダを【共有ドライブ】内に作る"
echo "      （マイドライブ配下だとサービスアカウントは保存できません）"
echo "   2. そのフォルダに $SA_EMAIL を『コンテンツ管理者』として追加"
echo "   3. フォルダIDを指定して再実行:"
echo "      DRIVE_PARENT_FOLDER_ID=<フォルダID> ./deploy.sh"
echo "=========================================================="
