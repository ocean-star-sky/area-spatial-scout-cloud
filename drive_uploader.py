#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
drive_uploader.py
Google Drive API v3 を使用して、生成されたレポート・地図・CSV・写真を
Google ドライブの指定フォルダへ直接アップロード・保存するモジュール。
"""

import os
import io
import json
from pathlib import Path
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

DEFAULT_PARENT_FOLDER_ID = "1ox9MRqdGRbfOIeEZAMWpQ_RbIupcn9dH"
SCOPES = ["https://www.googleapis.com/auth/drive"]


def get_drive_service():
    """環境変数またはJSONキーファイルからGoogle Drive APIサービスを取得"""
    creds_json = os.environ.get("GDRIVE_SERVICE_ACCOUNT_JSON")
    creds_file = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    
    if creds_json:
        # 環境変数に直接JSON文字列が格納されている場合
        creds_info = json.loads(creds_json)
        creds = service_account.Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    elif creds_file and os.path.exists(creds_file):
        # ファイルパスが指定されている場合
        creds = service_account.Credentials.from_service_account_file(creds_file, scopes=SCOPES)
    else:
        # ローカル開発用: 直下の service_account.json を探す
        local_key = Path("service_account.json")
        if local_key.exists():
            creds = service_account.Credentials.from_service_account_file(str(local_key), scopes=SCOPES)
        else:
            try:
                import google.auth
                creds, _ = google.auth.default(scopes=SCOPES)
            except Exception as e:
                raise RuntimeError(
                    f"Google Drive APIの認証情報が見つかりません ({e})。環境変数 GDRIVE_SERVICE_ACCOUNT_JSON または "
                    "GOOGLE_APPLICATION_CREDENTIALS を設定するか、service_account.json を配置してください。"
                )

    return build("drive", "v3", credentials=creds)


def create_drive_folder(service, folder_name: str, parent_id: str) -> str:
    """Google ドライブ上に新しいフォルダを作成してIDを返す"""
    file_metadata = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id]
    }
    folder = service.files().create(body=file_metadata, fields="id", supportsAllDrives=True).execute()
    return folder.get("id")


def upload_file_to_drive(service, file_path: Path, parent_id: str) -> str:
    """ファイルをGoogle ドライブの指定フォルダへアップロード"""
    file_name = file_path.name
    
    # 拡張子からMIMEタイプを判定
    suffix = file_path.suffix.lower()
    if suffix == ".docx":
        mime_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    elif suffix == ".csv":
        mime_type = "text/csv"
    elif suffix in (".jpg", ".jpeg"):
        mime_type = "image/jpeg"
    elif suffix == ".png":
        mime_type = "image/png"
    elif suffix == ".json":
        mime_type = "application/json"
    else:
        mime_type = "application/octet-stream"

    file_metadata = {
        "name": file_name,
        "parents": [parent_id]
    }
    media = MediaFileUpload(str(file_path), mimetype=mime_type, resumable=True)
    uploaded_file = service.files().create(body=file_metadata, media_body=media, fields="id", supportsAllDrives=True).execute()
    return uploaded_file.get("id")


def upload_report_directory(local_dir: Path, target_folder_name: str, parent_folder_id: str = DEFAULT_PARENT_FOLDER_ID) -> str | None:
    """
    指定ディレクトリ配下の成果物ファイルを Google ドライブへアップロード。
    個人アカウントの容量制限（storageQuotaExceeded）等の場合はエラーをスローせず、
    None を返してスマホ直接ダウンロードへ安全にフォールバックする。
    """
    try:
        service = get_drive_service()
        print(f"Creating folder on Google Drive: {target_folder_name} (Parent: {parent_folder_id})")
        created_folder_id = create_drive_folder(service, target_folder_name, parent_folder_id)
        
        # ディレクトリ内のファイルを走査してアップロード
        for f in local_dir.glob("*"):
            if f.is_file() and not f.name.startswith(".") and not f.name.endswith(".zip"):
                print(f"  -> Uploading: {f.name}...")
                upload_file_to_drive(service, f, created_folder_id)

        folder_url = f"https://drive.google.com/drive/folders/{created_folder_id}"
        print(f"[OK] Google ドライブへ完全同期しました: {folder_url}")
        return folder_url
    except Exception as e:
        print(f"[Notice] Google ドライブへの同期を安全にスキップしました (個人アカウント等の制限): {e}")
        return None
