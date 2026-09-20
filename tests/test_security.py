"""公開エンドポイントの認証とパス安全性の回帰テスト。

いずれも修正前の実機で「通ってしまう」ことを確認済みの経路を固定する:
  - GET /api/download/%2e%2e/zip が OUTPUTS_DIR の親の zip を配信していた
  - SCOUT_PASSWORD を設定しても /api/scout/jobs と /api/download/* は無認証で 200 だった
  - 未知の API パスが catch-all により 200 + HTML を返していた
"""

import zipfile
from urllib.parse import unquote

from conftest import TEST_PASSWORD


def _make_bait_zip(path):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("secret.txt", "TOP SECRET")


# パスガード (resolve_job_dir) まで到達し、そこで弾かれる入力。
# app.py の detail 文言で「どのガードが弾いたか」を区別する。
REACHES_PATH_GUARD = ["%2e%2e", "%2E%2E", "spot_1", "debug_1", "%00", "20200101_000000_ZZZZZZ"]

# "/" を含むためルーティング段階で download ハンドラに到達しない入力。
# こちらも 404 になるが、根拠は catch-all であってパスガードではない。
STOPPED_BY_ROUTING = ["..", "%2e%2e%2f%2e%2e", "%2ftmp", "..%2f.."]


def test_encoded_parent_traversal_is_rejected(client, app_module):
    """%2e%2e で OUTPUTS_DIR の外に出られないこと (修正前は 200 で zip が漏洩した)。

    トークン検証に助けられて弾かれたのでは、パスガード自体を検証したことにならないため、
    各 job_id に対する正当なトークンを付けたうえで 404 になることを確かめる。
    さらに catch-all の 404 と取り違えないよう detail 文言まで照合する。
    """
    _make_bait_zip(app_module.OUTPUTS_DIR.parent / "bait.zip")
    for job_id in REACHES_PATH_GUARD:
        decoded = unquote(job_id)
        res = client.get(f"/api/download/{job_id}/zip", params={"t": app_module.download_token(decoded)})
        assert res.status_code == 404, f"{job_id} が 404 で弾かれていない (実際は {res.status_code})"
        assert res.json()["detail"] == "ファイルが見つかりません", (
            f"{job_id} はパスガードではなく別経路 ({res.json()}) で弾かれている"
        )
        assert "application/zip" not in res.headers.get("content-type", "")
        assert "SECRET" not in res.text


def test_slash_bearing_job_ids_never_reach_the_handler(client, app_module):
    """'/' を含む job_id はルーティングで落ちること (漏洩しないことも併せて確認)"""
    _make_bait_zip(app_module.OUTPUTS_DIR.parent / "bait.zip")
    for job_id in STOPPED_BY_ROUTING:
        res = client.get(f"/api/download/{job_id}/zip", params={"t": app_module.download_token(unquote(job_id))})
        assert res.status_code == 404
        assert "application/zip" not in res.headers.get("content-type", "")
        assert "SECRET" not in res.text


def test_jobs_listing_requires_password(client):
    assert client.get("/api/scout/jobs").status_code == 401
    assert client.get("/api/scout/jobs", headers={"X-Scout-Key": "wrong"}).status_code == 401
    assert client.get("/api/scout/jobs", headers={"X-Scout-Key": TEST_PASSWORD}).status_code == 200


def test_admin_key_is_not_accepted_in_query_string(client):
    """クエリ文字列の資格情報はアクセスログに平文で残るため受け付けない"""
    assert client.get("/api/scout/jobs", params={"password": TEST_PASSWORD}).status_code == 401


def test_job_existence_is_not_leaked_without_token(client, stub_research):
    """トークン無しでは『そのジョブが在るか』を 404/403 の差で言い当てられないこと"""
    res = client.post(
        "/api/scout/instant",
        json={"area": "銀座", "theme": "個室鮨", "count": 3, "password": TEST_PASSWORD},
    )
    existing = res.json()["job_id"]
    missing = "20200101_000000_abcdef"
    assert client.get(f"/api/download/{existing}/zip").status_code == 403
    assert client.get(f"/api/download/{missing}/zip").status_code == 403


def test_download_token_is_not_derived_from_password_directly(app_module):
    """トークンがアクセスキーそのものを鍵にしていないこと (漏洩時の総当たりオラクル化を防ぐ)"""
    import hashlib
    import hmac as _hmac

    job_id = "20260920_120000_abcdef"
    naive = _hmac.new(
        app_module.SCOUT_PASSWORD.encode(), job_id.encode(), hashlib.sha256
    ).hexdigest()[:32]
    assert app_module.download_token(job_id) != naive


def test_instant_requires_password(client):
    body = {"area": "銀座", "theme": "鮨", "count": 3}
    assert client.post("/api/scout/instant", json=body).status_code == 401
    assert client.post("/api/scout/instant", json={**body, "password": "wrong"}).status_code == 401


def test_download_requires_valid_token(client, stub_research):
    """job_id を知っているだけでは成果物を取得できないこと"""
    res = client.post(
        "/api/scout/instant",
        json={"area": "銀座", "theme": "個室鮨", "count": 3, "password": TEST_PASSWORD},
    )
    assert res.status_code == 200, res.text
    payload = res.json()
    job_id = payload["job_id"]

    assert client.get(f"/api/download/{job_id}/zip").status_code == 403
    assert client.get(f"/api/download/{job_id}/zip", params={"t": "0" * 32}).status_code == 403
    assert client.get(payload["zip_url"]).status_code == 200


def test_debug_endpoint_is_absent_by_default(client):
    """Gemini 課金を焼く診断 API は既定で公開されないこと"""
    assert client.get("/api/scout/debug").status_code == 404


def test_unknown_api_path_returns_404_not_html(client):
    res = client.get("/api/nope")
    assert res.status_code == 404
    assert "text/html" not in res.headers.get("content-type", "")


def test_non_api_path_still_serves_spa(client):
    res = client.get("/anything-else")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]


def test_fail_closed_when_password_unset(unconfigured_client):
    """SCOUT_PASSWORD 未設定なら誰にもサービスしないこと"""
    assert unconfigured_client.post(
        "/api/scout/instant", json={"area": "銀座", "theme": "鮨", "count": 3}
    ).status_code == 503
    assert unconfigured_client.get("/api/scout/jobs").status_code == 503
    assert unconfigured_client.get("/api/download/20260920_120000_abcdef/zip").status_code == 503
    # ヘルスチェックだけは常に応答する
    assert unconfigured_client.get("/health").status_code == 200


def test_cors_does_not_combine_wildcard_with_credentials(app_module):
    """allow_origins=* と allow_credentials=True の同時指定は無効な組合せ"""
    cors = [m for m in app_module.app.user_middleware if "CORS" in str(m)]
    assert cors, "CORS ミドルウェアが登録されていない"
    kwargs = cors[0].kwargs
    assert kwargs["allow_credentials"] is False
