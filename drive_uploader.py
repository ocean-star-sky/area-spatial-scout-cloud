#!/usr/bin/env python3
"""
drive_uploader.py
Google Drive API v3 で成果物を「共有ドライブ (Shared Drive)」へ納品するモジュール。

重要 (実運用でつまずく点):
  サービスアカウントは個人のマイドライブに保存容量を持たないため、マイドライブ配下の
  フォルダを共有しても files.create は 403 "Service Accounts do not have storage quota"
  で失敗する。納品先は必ず共有ドライブに置き、SA をそのメンバーにすること。
"""

import json
import os
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/drive"]

MIME_BY_SUFFIX = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".csv": "text/csv",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".json": "application/json",
    ".zip": "application/zip",
}


class DriveUploadError(RuntimeError):
    """ドライブ納品が成立しなかった。呼び出し元は理由を利用者に伝えること。"""


def get_parent_folder_id() -> str:
    folder_id = os.environ.get("DRIVE_PARENT_FOLDER_ID", "").strip()
    if not folder_id:
        raise DriveUploadError(
            "DRIVE_PARENT_FOLDER_ID が未設定です（共有ドライブ内の納品先フォルダIDを設定してください）"
        )
    return folder_id


def get_drive_service():
    """環境変数またはキーファイルから Drive API サービスを取得"""
    creds_json = os.environ.get("GDRIVE_SERVICE_ACCOUNT_JSON")
    creds_file = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")

    try:
        if creds_json:
            creds = service_account.Credentials.from_service_account_info(json.loads(creds_json), scopes=SCOPES)
        elif creds_file and os.path.exists(creds_file):
            creds = service_account.Credentials.from_service_account_file(creds_file, scopes=SCOPES)
        else:
            local_key = Path("service_account.json")
            if local_key.exists():
                creds = service_account.Credentials.from_service_account_file(str(local_key), scopes=SCOPES)
            else:
                import google.auth

                creds, _ = google.auth.default(scopes=SCOPES)
    except DriveUploadError:
        raise
    except Exception as e:
        raise DriveUploadError(f"Google Drive の認証情報を取得できません: {type(e).__name__}: {e}") from e

    return build("drive", "v3", credentials=creds, cache_discovery=False)


def create_drive_folder(service, folder_name: str, parent_id: str) -> str:
    """共有ドライブ上に新しいフォルダを作成してIDを返す"""
    metadata = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    folder = service.files().create(body=metadata, fields="id", supportsAllDrives=True).execute()
    return folder["id"]


def upload_file_to_drive(service, file_path: Path, parent_id: str) -> str:
    """ファイルを指定フォルダへアップロード"""
    from googleapiclient.http import MediaFileUpload

    mime_type = MIME_BY_SUFFIX.get(file_path.suffix.lower(), "application/octet-stream")
    media = MediaFileUpload(str(file_path), mimetype=mime_type, resumable=True)
    uploaded = (
        service.files()
        .create(
            body={"name": file_path.name, "parents": [parent_id]},
            media_body=media,
            fields="id",
            supportsAllDrives=True,
        )
        .execute()
    )
    return uploaded["id"]


def verify_access(parent_folder_id: str = None) -> dict:
    """
    納品先へ実際に書き込めるかを検証する。
    「親フォルダが読めた」だけでは SA の容量制限を検知できないため、
    テストフォルダを実際に作成してから削除する。
    """
    parent_id = parent_folder_id or get_parent_folder_id()
    service = get_drive_service()
    probe_id = None
    try:
        meta = (
            service.files()
            .get(fileId=parent_id, fields="id,name,driveId", supportsAllDrives=True)
            .execute()
        )
        probe_id = create_drive_folder(service, "_write_probe_delete_me", parent_id)
        return {
            "ok": True,
            "parent_name": meta.get("name"),
            "drive_id": meta.get("driveId"),
            "is_shared_drive": bool(meta.get("driveId")),
        }
    except HttpError as e:
        raise DriveUploadError(f"納品先への書き込み検証に失敗しました: HTTP {e.status_code} {e.reason}") from e
    finally:
        if probe_id:
            try:
                service.files().delete(fileId=probe_id, supportsAllDrives=True).execute()
            except Exception as e:
                print(f"[Warning] 検証用フォルダの削除に失敗 ({probe_id}): {e}")


def upload_report_directory(local_dir: Path, target_folder_name: str, parent_folder_id: str = None) -> str:
    """
    指定ディレクトリ配下の成果物を共有ドライブへアップロードし、フォルダURLを返す。
    失敗時は DriveUploadError を送出する (None を返して無言で諦めない)。
    """
    parent_id = parent_folder_id or get_parent_folder_id()
    service = get_drive_service()

    try:
        print(f"[Drive] フォルダ作成: {target_folder_name} (親: {parent_id})")
        folder_id = create_drive_folder(service, target_folder_name, parent_id)
    except HttpError as e:
        detail = f"HTTP {e.status_code} {e.reason}"
        if "storageQuotaExceeded" in str(e) or "storage quota" in str(e).lower():
            detail += "（サービスアカウントはマイドライブに保存できません。納品先を共有ドライブにしてください）"
        raise DriveUploadError(f"納品先フォルダを作成できません: {detail}") from e

    uploaded = 0
    for f in sorted(local_dir.glob("*")):
        if f.is_file() and not f.name.startswith("."):
            try:
                upload_file_to_drive(service, f, folder_id)
                uploaded += 1
            except HttpError as e:
                raise DriveUploadError(f"'{f.name}' のアップロードに失敗: HTTP {e.status_code} {e.reason}") from e

    folder_url = f"https://drive.google.com/drive/folders/{folder_id}"
    print(f"[Drive] 納品完了: {uploaded}ファイル -> {folder_url}")
    return folder_url


if __name__ == "__main__":
    # 手元から納品先の書き込み可否を確認するための入口
    print(json.dumps(verify_access(), ensure_ascii=False, indent=2))
