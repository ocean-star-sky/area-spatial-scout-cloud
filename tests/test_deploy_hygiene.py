"""デプロイ資材の衛生テスト。

修正前は update_and_deploy.sh が「最新修正版」と表示しながら、埋め込みの base64 tar を
extractall('.') で展開して直近の修正を丸ごと巻き戻していた (実測: app.py 6,952B を
21,088B の現行版に上書き)。二度と同じ構造を作らないよう機械検出する。
"""

import re
import stat

import pytest
from conftest import REPO_ROOT

TEXT_SUFFIXES = {".py", ".sh", ".md", ".txt", ".html", ".yml", ".yaml", ""}

# tests/ 自身は除外する。この検査ファイルが 'extractall' という語を説明のために
# 含むため、含めると自分自身に反応して必ず赤くなる (実態の検出にならない)。
TRACKED_FILES = [
    p
    for p in REPO_ROOT.rglob("*")
    if p.is_file()
    and ".git" not in p.parts
    and "tests" not in p.parts
    and p.suffix in TEXT_SUFFIXES
]

# gzip+base64 の tar は 'H4sI' で始まる長い base64 リテラルになる
EMBEDDED_ARCHIVE_RE = re.compile(r"['\"]H4sI[A-Za-z0-9+/=]{200,}")


def test_repository_has_no_files():
    assert TRACKED_FILES, "検査対象ファイルを1件も拾えていない (検査が空振りしている)"


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
    assert not re.search(r"--set-env-vars\s+\"?GEMINI_API_KEY=", body), "APIキーを平文 env で渡している"
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
