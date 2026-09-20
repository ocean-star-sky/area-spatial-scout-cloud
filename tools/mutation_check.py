#!/usr/bin/env python3
"""回帰テストが実際に効いているかを変異テストで実証する。

各ガードを1つずつ「修正前の壊れた状態」に戻し、対応するテストが RED になることを確認する。
RED にならない変異は『素通りテスト』なので失敗として報告する。
リポジトリ本体は触らず、毎回 mktemp のコピー上で実行する。
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# (名前, 対象ファイル, 置換前, 置換後, そのミューテーションを殺すはずのテスト)
MUTATIONS = [
    (
        "job_id の書式検証を撤去 (単独では 2段目のガードが残る=inert のはず)",
        "app.py",
        'JOB_ID_RE = re.compile(r"^[0-9]{8}_[0-9]{6}_[0-9a-f]{6}$")',
        'JOB_ID_RE = re.compile(r"^.*$")',
        "tests/test_security.py::test_encoded_parent_traversal_is_rejected",
        True,  # inert を許容 (多層防御)
    ),
    (
        "パスガードを両方撤去 (パストラバーサル完全復活)",
        "app.py",
        '    if not job_dir.is_relative_to(OUTPUTS_DIR.resolve()) or not job_dir.is_dir():',
        "    if not job_dir.is_dir():",
        "tests/test_security.py::test_encoded_parent_traversal_is_rejected",
    ),
    (
        "認証を無効化 (誰でも実行可)",
        "app.py",
        "    if not password or not secrets.compare_digest(password, SCOUT_PASSWORD):",
        "    if False:",
        "tests/test_security.py::test_jobs_listing_requires_password",
    ),
    (
        "ダウンロードトークン検証を撤去",
        "app.py",
        '    if not t or not secrets.compare_digest(t, download_token(job_id)):',
        "    if False:",
        "tests/test_security.py::test_download_requires_valid_token",
    ),
    (
        "SCOUT_PASSWORD 未設定でも通す (fail-open 化)",
        "app.py",
        '    if not SCOUT_PASSWORD:\n        raise HTTPException(\n            status_code=503,',
        '    if False:\n        raise HTTPException(\n            status_code=503,',
        "tests/test_security.py::test_fail_closed_when_password_unset",
    ),
    (
        "catch-all が API の 404 を隠す",
        "app.py",
        '    if full_path.startswith("api/"):',
        "    if False:",
        "tests/test_security.py::test_unknown_api_path_returns_404_not_html",
    ),
    (
        "調査失敗を 200 の偽成功にすり替える",
        "app.py",
        "        raise HTTPException(\n            status_code=502,",
        "        raise HTTPException(\n            status_code=200,",
        "tests/test_no_fabrication.py::test_research_failure_is_not_reported_as_success",
    ),
    (
        "reviews_count を素通し (書式指定でクラッシュ)",
        "report_engine.py",
        '    digits = re.sub(r"[^0-9]", "", str(val or ""))\n    return int(digits) if digits else default',
        "    return val",
        "tests/test_report_engine.py::test_string_reviews_count_does_not_crash",
    ),
    (
        "廃止済みモデルを候補に戻す",
        "research_engine.py",
        'DEFAULT_MODELS = ("gemini-flash-latest", "gemini-2.5-flash", "gemini-2.5-flash-lite")',
        'DEFAULT_MODELS = ("gemini-1.5-flash", "gemini-2.0-flash")',
        "tests/test_research_engine.py::test_models_are_distinct_so_per_model_quota_can_fall_through",
    ),
    (
        "Google 検索グラウンディングを外す",
        "research_engine.py",
        '        "tools": [{"google_search": {}}],',
        "",
        "tests/test_research_engine.py::test_payload_enables_google_search_grounding",
    ),
    (
        "APIキーを平文 env でデプロイ",
        "deploy.sh",
        '    --set-secrets "GEMINI_API_KEY=${GEMINI_SECRET}:latest',
        '    --set-env-vars "DRIVE_PARENT_FOLDER_ID=x,GEMINI_API_KEY=${GEMINI_API_KEY}" \\\n    --set-secrets "SCOUT_PASSWORD=${PASSWORD_SECRET}:latest',
        "tests/test_deploy_hygiene.py::test_deploy_script_uses_secret_manager",
    ),
    (
        "ジオコーディング失敗時も地図に載せる (架空座標の復活)",
        "report_engine.py",
        "            if not geo:",
        "            if False:",
        "tests/test_report_engine.py::test_unresolvable_addresses_are_not_given_invented_coordinates",
    ),
    (
        "ファイル名サニタイズを撤去 (書き込み側 traversal)",
        "report_engine.py",
        '    text = _UNSAFE_FILENAME_RE.sub("_", text)',
        "    return text or default",
        "tests/test_report_engine.py::test_area_theme_cannot_escape_output_dir",
    ),
    (
        "LLM の id をそのままパスに使う",
        "research_engine.py",
        '    slug = re.sub(r"[^A-Za-z0-9_-]", "", str(raw or ""))[:40]',
        '    slug = str(raw or "")',
        "tests/test_research_engine.py::test_llm_supplied_id_cannot_escape_output_dir",
    ),
    (
        "SSRF ガードを撤去 (内部アドレスへ取得しに行く)",
        "research_engine.py",
        "    if not is_public_http_url(url):",
        "    if False:",
        "tests/test_research_engine.py",
    ),
    (
        "トークン鍵をアクセスキーそのものに戻す",
        "app.py",
        '    return sha256(b"area-spatial-scout/download-token/v1|" + SCOUT_PASSWORD.encode("utf-8")).digest()',
        '    return SCOUT_PASSWORD.encode("utf-8")',
        "tests/test_security.py::test_download_token_is_not_derived_from_password_directly",
    ),
    (
        "存在オラクル復活 (トークン検証より先にディレクトリ解決)",
        "app.py",
        "    if not t or not secrets.compare_digest(t, download_token(job_id)):\n"
        '        raise HTTPException(status_code=403, detail="このファイルへのアクセス権がありません")\n'
        "    job_dir = resolve_job_dir(job_id)",
        "    job_dir = resolve_job_dir(job_id)\n"
        "    if not t or not secrets.compare_digest(t, download_token(job_id)):\n"
        '        raise HTTPException(status_code=403, detail="このファイルへのアクセス権がありません")',
        "tests/test_security.py::test_job_existence_is_not_leaked_without_token",
    ),
    (
        "管理APIでクエリ文字列の資格情報も受理",
        "app.py",
        'def require_auth_header(x_scout_key: str = Header("")) -> None:',
        'def require_auth_header(x_scout_key: str = Header(""), password: str = Query("")) -> None:\n'
        "    if password:\n"
        "        return require_auth(password)",
        "tests/test_security.py::test_admin_key_is_not_accepted_in_query_string",
    ),
    (
        "コンテナイメージから秘密を除外しない",
        "__DELETE_FILE__:.dockerignore",
        "",
        "",
        "tests/test_deploy_hygiene.py::test_secrets_are_excluded_from_container_image",
    ),
    (
        "自己展開デプロイスクリプトを復活",
        "__NEW_FILE__:update_and_deploy.sh",
        "",
        "#!/bin/bash\nb64 = 'H4sI" + "A" * 220 + "'\nt.extractall('.')\n",
        "tests/test_deploy_hygiene.py",
    ),
]


def run_pytest(cwd: Path, target: str) -> int:
    return subprocess.run(
        [sys.executable, "-m", "pytest", target, "-q", "--no-header", "-x"],
        cwd=cwd,
        capture_output=True,
        text=True,
    ).returncode


def main() -> int:
    # 1. ベースライン: 変異なしで全部 GREEN であること
    base = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--no-header"],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    if base.returncode != 0:
        print("❌ ベースラインが GREEN ではありません。変異テストの前に修正が必要です。")
        print(base.stdout[-2000:])
        return 1
    print(f"✅ ベースライン GREEN: {base.stdout.strip().splitlines()[-1]}\n")

    killed, survived = 0, []
    for i, mut in enumerate(MUTATIONS, start=1):
        name, rel, old, new, target = mut[:5]
        allow_inert = mut[5] if len(mut) > 5 else False
        work = Path(tempfile.mkdtemp(prefix="mut_"))
        dst = work / "repo"
        shutil.copytree(REPO, dst, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"))

        if name.startswith("パスガードを両方撤去"):
            # 書式検証だけ残っていると到達しないので、こちらも同時に緩める
            ap = dst / "app.py"
            ap.write_text(
                ap.read_text(encoding="utf-8").replace(
                    'JOB_ID_RE = re.compile(r"^[0-9]{8}_[0-9]{6}_[0-9a-f]{6}$")',
                    'JOB_ID_RE = re.compile(r"^.*$")',
                ),
                encoding="utf-8",
            )

        if rel.startswith("__DELETE_FILE__:"):
            (dst / rel.split(":", 1)[1]).unlink(missing_ok=True)
        elif rel.startswith("__NEW_FILE__:"):
            (dst / rel.split(":", 1)[1]).write_text(new, encoding="utf-8")
        else:
            path = dst / rel
            body = path.read_text(encoding="utf-8")
            if old not in body:
                print(f"[{i:2d}] ⚠️  変異を適用できません（対象文字列が見つからない）: {name}")
                survived.append(f"{name} (適用不能)")
                shutil.rmtree(work, ignore_errors=True)
                continue
            path.write_text(body.replace(old, new, 1), encoding="utf-8")

        rc = run_pytest(dst, target)
        if rc != 0:
            print(f"[{i:2d}] ✅ KILLED   {name}")
            killed += 1
        else:
            if allow_inert:
                print(f"[{i:2d}] ➖ INERT    {name} (多層防御のため単独では再現しない・確認済み)")
            else:
                print(f"[{i:2d}] ❌ SURVIVED {name}  -> {target} が素通りしている")
                survived.append(name)
        shutil.rmtree(work, ignore_errors=True)

    print(f"\n検証した変異: {len(MUTATIONS)} 件 / KILLED {killed} 件 / SURVIVED {len(survived)} 件")
    for s in survived:
        print(f"  - SURVIVED: {s}")
    return 0 if not survived else 1


if __name__ == "__main__":
    sys.exit(main())
