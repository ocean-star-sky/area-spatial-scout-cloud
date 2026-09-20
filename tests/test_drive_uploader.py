"""Google ドライブ納品の回帰テスト。

実運用で踏んだ問題:
  サービスアカウントは個人のマイドライブに保存容量を持たない。ところが
  フォルダ作成だけは成功してしまうため、旧版は納品先に 0 ファイルのフォルダを
  11 個積み上げたうえで、例外を握り潰して「成功」扱いにしていた。

対策は 2 つ:
  - マイドライブへ納品するときは OAuth ユーザー資格情報を使う
  - 1 件もアップロードできなかったら、作ったフォルダを消して失敗を返す
"""

import json

import pytest
from googleapiclient.errors import HttpError

import drive_uploader
from drive_uploader import DriveUploadError, build_user_credentials, credential_kind

OAUTH_PAYLOAD = {
    "client_id": "cid.apps.googleusercontent.com",
    "client_secret": "csecret",
    "refresh_token": "rtoken",
}


class _FakeResp:
    def __init__(self, status=403, reason="Forbidden"):
        self.status = status
        self.reason = reason


def _quota_error():
    return HttpError(
        _FakeResp(),
        b'{"error": {"errors": [{"reason": "storageQuotaExceeded"}], "message": "Service Accounts do not have storage quota."}}',
    )


class FakeDrive:
    """files().create / delete だけを持つ最小の Drive API スタブ"""

    def __init__(self, fail_uploads=False):
        self.fail_uploads = fail_uploads
        self.created_folders = []
        self.uploaded = []
        self.deleted = []

    def files(self):
        return self

    def create(self, body=None, media_body=None, fields=None, supportsAllDrives=None):
        self._pending = (body, media_body)
        return self

    def delete(self, fileId=None, supportsAllDrives=None):
        self._pending = ("delete", fileId)
        return self

    def execute(self):
        kind, payload = self._pending
        if kind == "delete":
            self.deleted.append(payload)
            return {}
        body = kind
        if body.get("mimeType") == "application/vnd.google-apps.folder":
            self.created_folders.append(body["name"])
            return {"id": f"folder_{len(self.created_folders)}"}
        if self.fail_uploads:
            raise _quota_error()
        self.uploaded.append(body["name"])
        return {"id": f"file_{len(self.uploaded)}"}


@pytest.fixture
def drive(monkeypatch):
    fake = FakeDrive()
    monkeypatch.setattr(drive_uploader, "get_drive_service", lambda: fake)
    monkeypatch.setenv("DRIVE_PARENT_FOLDER_ID", "parent123")
    return fake


def _make_artifacts(tmp_path):
    (tmp_path / "report.docx").write_bytes(b"x" * 10)
    (tmp_path / "ledger.csv").write_text("a,b\n", encoding="utf-8")
    return tmp_path


def test_oauth_credentials_are_preferred_over_service_account(monkeypatch):
    monkeypatch.setenv("GDRIVE_OAUTH_JSON", json.dumps(OAUTH_PAYLOAD))
    monkeypatch.setenv("GDRIVE_SERVICE_ACCOUNT_JSON", json.dumps({"type": "service_account"}))
    assert credential_kind() == "oauth_user"


def test_service_account_is_used_when_no_oauth(monkeypatch):
    monkeypatch.delenv("GDRIVE_OAUTH_JSON", raising=False)
    assert credential_kind() == "service_account"


def test_user_credentials_are_built_from_refresh_token():
    creds = build_user_credentials(OAUTH_PAYLOAD)
    assert creds.refresh_token == "rtoken"
    assert creds.client_id == OAUTH_PAYLOAD["client_id"]
    assert "https://www.googleapis.com/auth/drive" in creds.scopes


@pytest.mark.parametrize("missing", ["client_id", "client_secret", "refresh_token"])
def test_incomplete_oauth_payload_is_reported(missing):
    payload = {k: v for k, v in OAUTH_PAYLOAD.items() if k != missing}
    with pytest.raises(DriveUploadError) as exc:
        build_user_credentials(payload)
    assert missing in str(exc.value)


def test_successful_upload_returns_folder_url(drive, tmp_path):
    url = drive_uploader.upload_report_directory(_make_artifacts(tmp_path), "20260920_テスト")
    assert url.startswith("https://drive.google.com/drive/folders/")
    assert sorted(drive.uploaded) == ["ledger.csv", "report.docx"]
    assert drive.deleted == [], "成功したのにフォルダを消している"


def test_quota_failure_removes_the_empty_folder(monkeypatch, tmp_path):
    """0 件しか入らなかったら空フォルダを残さないこと (旧版はこれで 11 個溜めた)"""
    fake = FakeDrive(fail_uploads=True)
    monkeypatch.setattr(drive_uploader, "get_drive_service", lambda: fake)
    monkeypatch.setenv("DRIVE_PARENT_FOLDER_ID", "parent123")

    with pytest.raises(DriveUploadError) as exc:
        drive_uploader.upload_report_directory(_make_artifacts(tmp_path), "20260920_テスト")

    assert fake.created_folders == ["20260920_テスト"]
    assert fake.deleted == ["folder_1"], "空フォルダを消していない"
    assert "マイドライブに保存容量を持ちません" in str(exc.value)


def test_upload_failure_is_never_silently_swallowed(monkeypatch, tmp_path):
    """None を返して「納品できなかったこと」を隠さない"""
    fake = FakeDrive(fail_uploads=True)
    monkeypatch.setattr(drive_uploader, "get_drive_service", lambda: fake)
    monkeypatch.setenv("DRIVE_PARENT_FOLDER_ID", "parent123")
    with pytest.raises(DriveUploadError):
        drive_uploader.upload_report_directory(_make_artifacts(tmp_path), "x")


def test_missing_parent_folder_id_is_reported(monkeypatch):
    monkeypatch.delenv("DRIVE_PARENT_FOLDER_ID", raising=False)
    with pytest.raises(DriveUploadError) as exc:
        drive_uploader.get_parent_folder_id()
    assert "DRIVE_PARENT_FOLDER_ID" in str(exc.value)


def test_empty_directory_does_not_leave_a_folder(drive, tmp_path):
    with pytest.raises(DriveUploadError) as exc:
        drive_uploader.upload_report_directory(tmp_path, "空の納品")
    assert drive.deleted == ["folder_1"]
    assert "0 件" in str(exc.value)
