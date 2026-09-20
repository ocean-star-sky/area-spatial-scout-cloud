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
        "距離判定を素通しにする (別エリア混入の復活)",
        "research_engine.py",
        "        if distance <= radius_km:",
        "        if True:",
        "tests/test_area_match.py::test_far_away_spots_are_excluded",
    ),
    (
        "エリア判定の半径を実質無制限にする",
        "research_engine.py",
        'AREA_MATCH_RADIUS_KM = float(os.environ.get("AREA_MATCH_RADIUS_KM", "3.0"))',
        'AREA_MATCH_RADIUS_KM = float(os.environ.get("AREA_MATCH_RADIUS_KM", "9999.0"))',
        "tests/test_area_match.py::test_far_away_spots_are_excluded",
    ),
    (
        "住所不明のスポットを黙って落とす",
        "research_engine.py",
        '            s["area_match"] = "unverified"\n            kept.append(s)',
        '            s["area_match"] = "unverified"\n            dropped.append(s)',
        "tests/test_area_match.py::test_spots_without_address_are_kept_but_flagged",
    ),
    (
        "エリア名だけを基準座標に使う (全国の同名地点を掴む)",
        "research_engine.py",
        "    anchors: list[tuple[float, float]] = []",
        "    anchors: list[tuple[float, float]] = [c for c in [geocode_address(area)] if c]",
        "tests/test_area_match.py::test_area_name_alone_is_never_geocoded",
    ),
    (
        "住所補完で既存の住所を上書きする",
        "research_engine.py",
        'if not spot or not address or (spot.get("address") or "").strip():',
        "if not spot or not address:",
        "tests/test_address_backfill.py::test_duplicate_rows_do_not_overwrite_the_first_answer",
    ),
    (
        "住所補完の失敗を致命的にする (調査本体を巻き込む)",
        "research_engine.py",
        '            print(f"[Address Backfill] {model} で失敗: {type(e).__name__}")\n            continue',
        "            raise",
        "tests/test_address_backfill.py::test_api_failure_is_not_fatal",
    ),
    (
        "入力していない施設名の回答も取り込む",
        "research_engine.py",
        "    by_name = {_normalize_name(s[\"name\"]): s for s in targets}",
        '    by_name = {}\n    for _s in targets:\n        by_name[_normalize_name(_s["name"])] = _s\n    by_name = type("D", (dict,), {"get": lambda self, k, d=None: next(iter(targets), d)})(by_name)',
        "tests/test_address_backfill.py::test_unknown_names_in_response_are_ignored",
    ),
    (
        "住所補完をエリア判定の後ろへ動かす",
        "research_engine.py",
        "    backfilled = backfill_addresses(by_genre, area, theme, key, models)\n    in_area, out_of_area = verify_area_match(by_genre, area)",
        "    in_area, out_of_area = verify_area_match(by_genre, area)\n    backfilled = backfill_addresses(by_genre, area, theme, key, models)",
        "tests/test_address_backfill.py::test_backfill_runs_before_area_verification",
    ),
    (
        "アップロード0件でも空フォルダを残す (旧版の汚染が復活)",
        "drive_uploader.py",
        "        _delete_folder_quietly(service, folder_id)\n        raise DriveUploadError",
        "        raise DriveUploadError",
        "tests/test_drive_uploader.py::test_empty_directory_does_not_leave_a_folder",
    ),
    (
        "OAuth より サービスアカウントを優先する",
        "drive_uploader.py",
        'return "oauth_user" if os.environ.get("GDRIVE_OAUTH_JSON", "").strip() else "service_account"',
        'return "service_account"',
        "tests/test_drive_uploader.py::test_oauth_credentials_are_preferred_over_service_account",
    ),
    (
        "自己展開デプロイスクリプトを復活",
        "__NEW_FILE__:update_and_deploy.sh",
        "",
        # 検出対象の文字列を分割して組み立てる。このファイル自身も
        # tests/test_deploy_hygiene.py の走査対象なので、リテラルで書くと
        # 「ハーネスが自分の検査に引っかかる」だけで実態の検出にならない。
        "#!/bin/bash\nb64 = '" + "H4s" + "I" + "A" * 220 + "'\nt." + "extract" + "all('.')\n",
        "tests/test_deploy_hygiene.py",
    ),
    (
        "検索が走らなかった応答も即採用する (裏取り無しの素通り復活)",
        "research_engine.py",
        "        if not cand_queries:",
        "        if False:",
        "tests/test_research_engine.py::test_grounded_response_wins_over_an_earlier_ungrounded_one",
    ),
    (
        "未グラウンディングでもクチコミを載せる",
        "research_engine.py",
        "    if grounded:\n        reviews_added = backfill_reviews(data[\"spots\"], area, theme, key, models)",
        "    if True:\n        reviews_added = backfill_reviews(data[\"spots\"], area, theme, key, models)",
        "tests/test_no_fabrication.py::test_ungrounded_response_carries_no_reviews",
    ),
    (
        "出典の無いクチコミも採る (件数合わせの創作が通る)",
        "research_engine.py",
        '        if not body or not source.startswith("http"):',
        "        if not body:",
        "tests/test_no_fabrication.py::test_reviews_without_a_source_are_not_merged",
    ),
    (
        "クチコミ補完で既存の声を捨てて置き換える",
        "research_engine.py",
        "    normalize_spot_reviews(spot)\n    texts = spot[\"reviews\"]\n    sources = spot[\"review_sources\"]",
        "    normalize_spot_reviews(spot)\n    texts = spot[\"reviews\"]\n    del texts[:]\n    sources = spot[\"review_sources\"]\n    del sources[:]",
        "tests/test_review_backfill.py::test_existing_reviews_are_kept_and_duplicates_are_skipped",
    ),
    (
        "クチコミの重複判定を撤去 (同じ声で枠を埋める)",
        "research_engine.py",
        "        if key in seen:\n            continue",
        "        if False:\n            continue",
        "tests/test_review_backfill.py::test_existing_reviews_are_kept_and_duplicates_are_skipped",
    ),
    (
        "公式サイトの OGP 画像を使わない (写真ゼロの状態へ巻き戻し)",
        "research_engine.py",
        "            og_url = fetch_og_image_url(s.get(\"url\") or \"\")",
        "            og_url = None",
        "tests/test_research_engine.py::test_og_image_is_used_when_the_response_has_no_photo_urls",
    ),
    (
        "OGP 画像URLを検証せず取得する (外部ページ由来の SSRF)",
        "research_engine.py",
        "        candidate = urllib.parse.urljoin(final_url, html.unescape(found[key]))\n        if is_public_http_url(candidate):",
        "        candidate = urllib.parse.urljoin(final_url, html.unescape(found[key]))\n        if True:",
        "tests/test_research_engine.py::test_og_image_pointing_at_an_internal_address_is_rejected",
    ),
    (
        "OGP 取得前の URL 検証を撤去 (file:// や内部アドレスを開く)",
        "research_engine.py",
        "    if not page_url or not is_public_http_url(page_url):\n        return None",
        "    if not page_url:\n        return None",
        "tests/test_research_engine.py::test_og_fetch_is_skipped_for_urls_we_must_not_open",
    ),
    (
        "JSON を読めない応答で即座に落ちる (候補モデルを残したまま 502)",
        "research_engine.py",
        "            continue\n\n        if not isinstance(cand_data, dict) or not isinstance(cand_data.get(\"spots\"), list):",
        "            raise ResearchUnavailable(\"調査結果の解析に失敗しました\", reasons)\n\n        if not isinstance(cand_data, dict) or not isinstance(cand_data.get(\"spots\"), list):",
        "tests/test_research_engine.py::test_unparsable_response_falls_through_to_the_next_model",
    ),
    (
        "間欠的な不良応答の再試行を撤去",
        "research_engine.py",
        "            if retry_budget > 0 and model not in retried:",
        "            if False:",
        "tests/test_research_engine.py::test_a_model_that_returned_prose_is_retried_once",
    ),
    (
        "再試行の上限を外す (1ジョブで無料枠を食い潰す)",
        "research_engine.py",
        'PARSE_RETRY_BUDGET = int(os.environ.get("PARSE_RETRY_BUDGET", "1"))',
        'PARSE_RETRY_BUDGET = int(os.environ.get("PARSE_RETRY_BUDGET", "99"))',
        "tests/test_research_engine.py::test_parse_retries_are_capped",
    ),
    (
        "spots が無い応答も採用する",
        "research_engine.py",
        '        if not isinstance(cand_data, dict) or not isinstance(cand_data.get("spots"), list):',
        "        if False:",
        "tests/test_research_engine.py::test_response_without_a_spots_list_falls_through",
    ),
    (
        "パース失敗の理由から finishReason を落とす (次の切り分けができない)",
        "research_engine.py",
        'reasons.append(f"{model}: JSONパース失敗: {parse_err} (finishReason={finish})")',
        'reasons.append(f"{model}: JSONパース失敗")',
        "tests/test_research_engine.py::test_unparsable_failure_reasons_name_the_model_and_finish_reason",
    ),
    (
        "プロンプトから前置き禁止を外す",
        "research_engine.py",
        "   応答の最初の文字から ```json で始めること。調査の経過・前置き・要約文・謝辞を",
        "   なるべく簡潔に書くこと。",
        "tests/test_research_engine.py::test_prompt_forbids_a_prose_preamble",
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
