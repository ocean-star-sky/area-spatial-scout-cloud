#!/usr/bin/env python3
"""
drive_uploader.py
Google Drive API v3 で成果物を Google ドライブへ納品するモジュール。

認証方式は2つあり、GDRIVE_OAUTH_JSON があればそちらを優先する。

  1. OAuth ユーザー資格情報 (GDRIVE_OAUTH_JSON) ... マイドライブに納品できる
  2. サービスアカウント                        ... 共有ドライブにしか納品できない

サービスアカウントは個人のマイドライブに保存容量を持たない。やっかいなのは
「フォルダ作成だけは成功してしまう」点で、フォルダは容量を消費しないため
files.create が通り、その中へのファイル投入だけが 403 storageQuotaExceeded で落ちる。
結果として**空のフォルダだけが延々と作られる**。実測でも、納品先に 11 個の
SA 所有フォルダが作られ、すべて 0 ファイルだった。
マイドライブへ納品するなら OAuth ユーザー資格情報を使うこと。
"""

import json
import os
from pathlib import Path

from google.oauth2 import service_account
from google.oauth2.credentials import Credentials as UserCredentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/drive"]
TOKEN_URI = "https://oauth2.googleapis.com/token"

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
            "DRIVE_PARENT_FOLDER_ID が未設定です（納品先フォルダIDを設定してください）"
        )
    return folder_id


def build_user_credentials(payload: dict) -> UserCredentials:
    """OAuth ユーザー資格情報 (リフレッシュトークン) から Credentials を組む"""
    missing = [k for k in ("client_id", "client_secret", "refresh_token") if not payload.get(k)]
    if missing:
        raise DriveUploadError(f"GDRIVE_OAUTH_JSON に必要な項目がありません: {', '.join(missing)}")
    return UserCredentials(
        token=payload.get("access_token"),
        refresh_token=payload["refresh_token"],
        client_id=payload["client_id"],
        client_secret=payload["client_secret"],
        token_uri=payload.get("token_uri", TOKEN_URI),
        scopes=SCOPES,
    )


def credential_kind() -> str:
    """どちらの資格情報で動いているかを返す (運用時の切り分け用)"""
    return "oauth_user" if os.environ.get("GDRIVE_OAUTH_JSON", "").strip() else "service_account"


def get_drive_service():
    """環境変数またはキーファイルから Drive API サービスを取得"""
    oauth_json = os.environ.get("GDRIVE_OAUTH_JSON", "").strip()
    creds_json = os.environ.get("GDRIVE_SERVICE_ACCOUNT_JSON")
    creds_file = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")

    try:
        if oauth_json:
            # マイドライブへ納品できるのはこちらだけ。SA より優先する。
            creds = build_user_credentials(json.loads(oauth_json))
        elif creds_json:
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
        # フォルダ作成だけでは足りない。SA はフォルダを作れてもファイルを入れられないため、
        # 実際に 1 ファイル投入するところまで確かめる。
        from googleapiclient.http import MediaInMemoryUpload

        probe_file = (
            service.files()
            .create(
                body={"name": "_write_probe.txt", "parents": [probe_id]},
                media_body=MediaInMemoryUpload(b"probe", mimetype="text/plain"),
                fields="id",
                supportsAllDrives=True,
            )
            .execute()
        )
        return {
            "ok": True,
            "credential_kind": credential_kind(),
            "parent_name": meta.get("name"),
            "drive_id": meta.get("driveId"),
            "is_shared_drive": bool(meta.get("driveId")),
            "probe_file_id": probe_file["id"],
        }
    except HttpError as e:
        raise DriveUploadError(f"納品先への書き込み検証に失敗しました: HTTP {e.status_code} {e.reason}") from e
    finally:
        if probe_id:
            try:
                service.files().delete(fileId=probe_id, supportsAllDrives=True).execute()
            except Exception as e:
                print(f"[Warning] 検証用フォルダの削除に失敗 ({probe_id}): {e}")


def _delete_folder_quietly(service, folder_id: str) -> None:
    """後片付け用。削除に失敗しても本来のエラーを覆い隠さない。"""
    try:
        service.files().delete(fileId=folder_id, supportsAllDrives=True).execute()
    except Exception as e:
        print(f"[Warning] 空フォルダの削除に失敗 ({folder_id}): {type(e).__name__}")


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
            detail += (
                "（サービスアカウントはマイドライブに保存容量を持ちません。"
                "GDRIVE_OAUTH_JSON で OAuth ユーザー資格情報を設定するか、納品先を共有ドライブにしてください）"
            )
        raise DriveUploadError(f"納品先フォルダを作成できません: {detail}") from e

    uploaded = 0
    for f in sorted(local_dir.glob("*")):
        if f.is_file() and not f.name.startswith("."):
            try:
                upload_file_to_drive(service, f, folder_id)
                uploaded += 1
            except HttpError as e:
                detail = f"HTTP {e.status_code} {e.reason}"
                if "storageQuotaExceeded" in str(e) or "storage quota" in str(e).lower():
                    detail += (
                        "（サービスアカウントはマイドライブに保存容量を持ちません。"
                        "GDRIVE_OAUTH_JSON で OAuth ユーザー資格情報を設定してください）"
                    )
                if uploaded == 0:
                    _delete_folder_quietly(service, folder_id)
                raise DriveUploadError(f"'{f.name}' のアップロードに失敗: {detail}") from e

    folder_url = f"https://drive.google.com/drive/folders/{folder_id}"
    if uploaded == 0:
        # 空フォルダを残して「成功」と言わない。作ったフォルダも片付ける。
        # (これを怠った旧版は、納品先に 0 ファイルのフォルダを 11 個積み上げていた)
        _delete_folder_quietly(service, folder_id)
        raise DriveUploadError("アップロードできたファイルが 0 件でした（作成したフォルダは削除しました）")
    print(f"[Drive] 納品完了: {uploaded}ファイル ({credential_kind()}) -> {folder_url}")
    return folder_url


if __name__ == "__main__":
    # 手元から納品先の書き込み可否を確認するための入口
    print(json.dumps(verify_access(), ensure_ascii=False, indent=2))
