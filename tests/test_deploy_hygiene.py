"""デプロイ資材の衛生テスト。

修正前は update_and_deploy.sh が「最新修正版」と表示しながら、埋め込みの base64 tar を
extractall('.') で展開して直近の修正を丸ごと巻き戻していた (実測: app.py 6,952B を
21,088B の現行版に上書き)。二度と同じ構造を作らないよう機械検出する。
"""

import pathlib
import re
import stat
import subprocess

import pytest
from conftest import REPO_ROOT

TEXT_SUFFIXES = {".py", ".sh", ".md", ".txt", ".html", ".yml", ".yaml", ""}


EXCLUDED_DIRS = {".git", ".ruff_cache", ".pytest_cache", "__pycache__", "tests", ".venv"}


def _tracked_text_files():
    """検査対象ファイルの一覧。

    rglob をそのまま使うと .ruff_cache / .pytest_cache の残骸を拾い、テスト件数が
    ローカル環境に依存して CI と一致しなくなるため、git 管理下のファイルを優先する。
    git が使えない環境 (変異テストのコピー等) では明示的な除外リストで走査する。
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-files"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        names = [f for f in out if f]
    except (subprocess.CalledProcessError, FileNotFoundError):
        names = [
            str(p.relative_to(REPO_ROOT))
            for p in REPO_ROOT.rglob("*")
            if p.is_file() and not EXCLUDED_DIRS & set(p.relative_to(REPO_ROOT).parts)
        ]
    return [
        REPO_ROOT / f
        for f in names
        if not f.startswith("tests/")
        and pathlib.Path(f).suffix in TEXT_SUFFIXES
        and not EXCLUDED_DIRS & set(pathlib.Path(f).parts)
        and (REPO_ROOT / f).is_file()
    ]


# tests/ 自身は除外する。この検査ファイルが 'extractall' という語を説明のために
# 含むため、含めると自分自身に反応して必ず赤くなる (実態の検出にならない)。
TRACKED_FILES = _tracked_text_files()

# gzip+base64 の tar は 'H4sI' で始まる長い base64 リテラルになる
EMBEDDED_ARCHIVE_RE = re.compile(r"['\"]H4sI[A-Za-z0-9+/=]{200,}")


def test_scan_target_is_non_empty_and_reproducible():
    """0件PASS を防ぎ、拾う対象が作業ツリーの残骸に依存しないことを確認する"""
    assert TRACKED_FILES, "検査対象ファイルを1件も拾えていない (検査が空振りしている)"
    assert all(p.exists() for p in TRACKED_FILES)
    assert not any(".ruff_cache" in str(p) or ".pytest_cache" in str(p) for p in TRACKED_FILES)


@pytest.mark.parametrize("path", TRACKED_FILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_no_embedded_source_archive(path):
    body = path.read_text(encoding="utf-8", errors="ignore")
    assert not EMBEDDED_ARCHIVE_RE.search(body), f"{path.name} にソース埋め込みアーカイブがある"
    assert "extractall" not in body, f"{path.name} がアーカイブを展開しようとしている"


def test_stale_self_extracting_deployer_is_gone():
    assert not (REPO_ROOT / "update_and_deploy.sh").exists()


def test_deploy_script_uses_secret_manager():
    body = (REPO_ROOT / "deploy.sh").read_text(encoding="utf-8")
    assert "--set-secrets" in body
    assert "--service-account" in body, "専用サービスアカウントを指定していない"
    # カンマ区切りの2番目以降に足された場合も捕まえる (先頭一致だけでは素通りする)
    for m in re.finditer(r"--set-env-vars\s+\"([^\"]*)\"", body):
        assert "GEMINI_API_KEY=" not in m.group(1), "APIキーを平文 env で渡している"
        assert "SCOUT_PASSWORD=" not in m.group(1), "アクセスキーを平文 env で渡している"
        assert "SCOUT_TOKEN_SECRET=" not in m.group(1), "トークン鍵を平文 env で渡している"
    assert not re.search(r"env\[0\]", body), "env の添字参照は順序依存で別の値を掴む"


def test_deploy_script_is_executable():
    assert (REPO_ROOT / "deploy.sh").stat().st_mode & stat.S_IXUSR


def test_readme_does_not_document_removed_paths():
    body = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "cd cloud_run_scout" not in body, "存在しないディレクトリへの cd を案内している"
    assert "update_and_deploy.sh" not in body


def test_no_hardcoded_drive_folder_id():
    """納品先はデプロイ環境ごとに変わる。ソースに固定IDを埋めない。"""
    body = (REPO_ROOT / "drive_uploader.py").read_text(encoding="utf-8")
    assert "DRIVE_PARENT_FOLDER_ID" in body
    assert not re.search(r"=\s*['\"][A-Za-z0-9_-]{28,}['\"]", body), "フォルダIDらしき定数が残っている"


def test_secrets_are_excluded_from_container_image():
    """gcloud run deploy --source . で SA 鍵や .git がイメージへ混入しないこと"""
    for name in (".dockerignore", ".gcloudignore"):
        body = (REPO_ROOT / name).read_text(encoding="utf-8")
        assert "service_account.json" in body, f"{name} が SA 鍵を除外していない"
        assert ".git" in body, f"{name} が .git を除外していない"
    assert "service_account.json" in (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")


def test_token_secret_is_provisioned_separately():
    """ダウンロードトークンの署名鍵をアクセスキーと別に払い出していること"""
    body = (REPO_ROOT / "deploy.sh").read_text(encoding="utf-8")
    assert "SCOUT_TOKEN_SECRET=" in body
    assert "token_urlsafe(32)" in body
