"""Gemini 呼び出しの回帰テスト。

修正前の事故:
  - モデル名 gemini-1.5-flash / gemini-2.0-flash が実 API で 404 になり調査が一度も動かなかった
  - README が謳う Google Search グラウンディングが payload に入っていなかった
"""

import io
import json
import pathlib
import urllib.error

import pytest

import research_engine
from research_engine import ResearchUnavailable, classify_genre, extract_json_from_text, filter_by_genre


@pytest.fixture(autouse=True)
def _no_address_backfill(monkeypatch):
    """既定では住所の追加取得を止める。

    有効なままだと「モデルのカスケードで何回呼んだか」を数えるテストに
    追加リクエストが混ざり、検証したい対象がぼやける。
    backfill 自体は test_address_backfill.py / test_review_backfill.py で個別に検証する。
    """
    monkeypatch.setattr(research_engine, "ADDRESS_BACKFILL_ENABLED", False)
    monkeypatch.setattr(research_engine, "REVIEW_BACKFILL_ENABLED", False)


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _gemini_body(text, sources=None, queries=None):
    candidate = {"content": {"parts": [{"text": text}]}}
    if sources is not None or queries is not None:
        candidate["groundingMetadata"] = {
            "groundingChunks": [{"web": s} for s in (sources or [])],
            "webSearchQueries": queries or [],
        }
    return json.dumps({"candidates": [candidate]}).encode("utf-8")


@pytest.fixture
def captured_requests(monkeypatch):
    """urlopen を差し替えて、送信した payload を記録する"""
    sent = []

    def _fake_urlopen(req, timeout=None):
        sent.append({"url": req.full_url, "payload": json.loads(req.data.decode("utf-8"))})
        return _FakeResponse(_gemini_body('```json {"spots": [{"name": "テスト鮨"}]} ```', [], ["銀座 鮨"]))

    monkeypatch.setattr(research_engine.urllib.request, "urlopen", _fake_urlopen)
    return sent


def test_payload_enables_google_search_grounding(captured_requests):
    research_engine.run_autonomous_research(area="銀座", theme="鮨", count=3, api_key="dummy-key")
    payload = captured_requests[0]["payload"]
    assert payload["tools"] == [{"google_search": {}}], "Google 検索グラウンディングが有効になっていない"
    assert payload["generationConfig"]["thinkingConfig"]["thinkingBudget"] == 0
    assert payload["generationConfig"]["maxOutputTokens"] >= 8192


def test_models_are_distinct_so_per_model_quota_can_fall_through():
    """無料枠はモデル単位の日次上限。候補が同一モデルだと枯渇時に逃げ場が無くなる。"""
    models = research_engine.DEFAULT_MODELS
    assert len(models) >= 2
    assert len(set(models)) == len(models)
    # 実 API で 404 が確認済みの廃止モデルを使わないこと
    assert not {"gemini-1.5-flash", "gemini-2.0-flash"} & set(models)


def test_falls_through_to_next_model_on_http_error(monkeypatch):
    calls = []

    def _fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        if len(calls) == 1:
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, io.BytesIO(b"quota"))
        return _FakeResponse(_gemini_body('```json {"spots": [{"name": "テスト鮨"}]} ```', [], ["銀座 鮨"]))

    monkeypatch.setattr(research_engine.urllib.request, "urlopen", _fake_urlopen)
    data = research_engine.run_autonomous_research(area="銀座", theme="鮨", count=3, api_key="k")
    assert len(calls) == 2
    assert data["spots"][0]["name"] == "テスト鮨"


def test_all_models_failing_raises_with_reasons(monkeypatch):
    def _always_429(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, io.BytesIO(b"quota"))

    monkeypatch.setattr(research_engine.urllib.request, "urlopen", _always_429)
    with pytest.raises(ResearchUnavailable) as exc:
        research_engine.run_autonomous_research(area="銀座", theme="鮨", count=3, api_key="k")
    assert len(exc.value.reasons) == len(research_engine.DEFAULT_MODELS)
    assert all("429" in r for r in exc.value.reasons)


def test_grounding_sources_are_recorded(monkeypatch):
    def _fake_urlopen(req, timeout=None):
        return _FakeResponse(
            _gemini_body(
                '```json {"spots": [{"name": "テスト鮨"}]} ```',
                sources=[{"title": "食べログ", "uri": "https://tabelog.com/x"}],
                queries=["銀座 個室 鮨"],
            )
        )

    monkeypatch.setattr(research_engine.urllib.request, "urlopen", _fake_urlopen)
    data = research_engine.run_autonomous_research(area="銀座", theme="鮨", count=3, api_key="k")
    assert data["meta"]["sources"][0]["uri"] == "https://tabelog.com/x"
    assert data["meta"]["search_queries"] == ["銀座 個室 鮨"]
    assert data["meta"]["requested_count"] == 3


def test_api_key_is_not_logged_verbatim(capsys, captured_requests):
    research_engine.run_autonomous_research(area="銀座", theme="鮨", count=3, api_key="SUPER_SECRET_KEY")
    assert "SUPER_SECRET_KEY" not in capsys.readouterr().out


@pytest.mark.parametrize(
    ("text", "genre"),
    [("個室接待鮨", "鮨"), ("サウナ", "サウナ"), ("コワーキングスペース", "コワーキング"), ("居酒屋", "その他")],
)
def test_classify_genre(text, genre):
    assert classify_genre(text) == genre


def test_filter_removes_cross_genre_spots():
    spots = [
        {"name": "鮨 一番", "category": "江戸前鮨"},
        {"name": "サウナ道場", "category": "サウナ"},
        {"name": "謎の店", "category": ""},
    ]
    kept = [s["name"] for s in filter_by_genre(spots, "個室鮨")]
    assert kept == ["鮨 一番", "謎の店"]


def test_extract_json_repairs_truncated_output():
    assert extract_json_from_text('```json {"spots": [{"name": "a"},') ["spots"][0]["name"] == "a"


def test_extract_json_raises_on_garbage():
    with pytest.raises(ValueError):
        extract_json_from_text("これはJSONではありません")


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/computeMetadata/v1/",  # クラウドのメタデータサーバ
        "http://127.0.0.1:8080/admin",
        "http://localhost/secret.jpg",
        "http://10.0.0.5/a.jpg",
        "file:///etc/passwd",
        "ftp://example.com/a.jpg",
        "http://[::1]/a.jpg",
    ],
)
def test_internal_and_non_http_urls_are_refused(url, tmp_path, monkeypatch):
    """LLM が返した URL をそのまま取りに行かないこと (SSRF)。

    戻り値が False であることだけを見ても検証にならない。ガードを外しても
    「接続できずに False」で同じ結果になるため、*要求を出していないこと* を確かめる。
    """
    attempted = []

    def _tripwire(*_args, **_kwargs):
        attempted.append(_args)
        raise AssertionError(f"ブロックすべき URL に要求を出した: {url}")

    monkeypatch.setattr(research_engine.urllib.request, "build_opener", _tripwire)

    assert research_engine.is_public_http_url(url) is False
    assert research_engine.download_and_crop_image(url, tmp_path / "x.jpg") is False
    assert attempted == [], f"ブロックすべき URL で opener を組み立てた: {url}"
    assert not (tmp_path / "x.jpg").exists()


def test_public_url_passes_the_filter():
    assert research_engine.is_public_http_url("https://example.com/a.jpg") is True


def test_redirects_are_not_followed():
    """リダイレクトで内部アドレスへ飛ばされる経路を塞いでいること"""
    assert research_engine._NoRedirect().redirect_request(None, None, 302, "", {}, "http://127.0.0.1/") is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("spot_1", "spot_1"), ("../../evil", "evil"), ("a/b", "ab"), ("", "fallback"), (None, "fallback")],
)
def test_spot_id_is_sanitised_for_filenames(raw, expected):
    assert research_engine.safe_spot_slug(raw, "fallback") == expected


def test_llm_supplied_id_cannot_escape_output_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(
        research_engine, "download_and_crop_image", lambda url, path, **k: path.write_bytes(b"x") or True
    )
    job = tmp_path / "job"
    job.mkdir()
    data = {"spots": [{"id": "../../pwned", "name": "n", "photo_urls": ["https://example.com/a.jpg"]}]}
    research_engine.attach_photos(data, job)
    written = pathlib.Path(data["spots"][0]["photos"][0]["path"]).resolve()
    assert written.parent == job.resolve(), "LLM の id でディレクトリを脱出した"


# --------------------------------------------------------------------------- グラウンディング
def test_grounded_response_wins_over_an_earlier_ungrounded_one(monkeypatch):
    """検索を使わなかった応答があっても、検索付きの応答が取れるならそちらを採用する。

    実測 (9/20 五反田): flash-latest が 429 → 2.5-flash がタイムアウト → lite が
    「検索クエリ 0件」で応答し、裏取りの無い生成物がそのまま成果物になっていた。
    """
    calls = []

    def _fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        if len(calls) == 1:
            return _FakeResponse(_gemini_body('```json {"spots": [{"name": "裏取り無し鮨"}]} ```'))
        return _FakeResponse(_gemini_body('```json {"spots": [{"name": "検索済み鮨"}]} ```', [], ["銀座 鮨"]))

    monkeypatch.setattr(research_engine.urllib.request, "urlopen", _fake_urlopen)
    data = research_engine.run_autonomous_research(area="銀座", theme="鮨", count=3, api_key="k")

    assert data["spots"][0]["name"] == "検索済み鮨"
    assert data["meta"]["grounding_status"] == "grounded"


def test_all_models_ungrounded_is_marked_and_reviews_are_dropped(monkeypatch):
    """全モデルが検索を使えなかった回は、結果は返すがクチコミは載せない。"""
    body = '```json {"spots": [{"name": "裏取り無し鮨", "reviews": ["よかった", "また行きたい"]}]} ```'

    monkeypatch.setattr(
        research_engine.urllib.request,
        "urlopen",
        lambda req, timeout=None: _FakeResponse(_gemini_body(body)),
    )
    data = research_engine.run_autonomous_research(area="銀座", theme="鮨", count=3, api_key="k")

    assert data["meta"]["grounding_status"] == "ungrounded"
    assert data["spots"][0]["reviews"] == []
    assert data["meta"]["reviews_total"] == 0


# --------------------------------------------------------------------------- OGP 画像
def test_og_image_is_used_when_the_response_has_no_photo_urls(tmp_path, monkeypatch):
    """実測で photo_urls は空で返る。公式サイトの og:image を写真候補にする。"""
    monkeypatch.setattr(research_engine, "fetch_og_image_url", lambda url: "https://shop.example/hero.jpg")
    monkeypatch.setattr(
        research_engine,
        "download_and_crop_image",
        lambda url, path, **k: path.write_bytes(b"x") or True,
    )
    data = {"spots": [{"id": "spot_1", "name": "テスト店", "url": "https://shop.example/", "photo_urls": []}]}

    assert research_engine.attach_photos(data, tmp_path) == 1
    photo = data["spots"][0]["photos"][0]
    assert photo["photo_source"] == "ogp"
    assert "shop.example" in photo["caption"]


def test_og_image_is_not_fetched_when_the_response_gave_photo_urls(tmp_path, monkeypatch):
    """調査応答が画像URLを返したときは、公式サイトを叩かない (余計な外部アクセスをしない)"""

    def _must_not_be_called(url):
        raise AssertionError("photo_urls があるのに OGP 取得が呼ばれた")

    monkeypatch.setattr(research_engine, "fetch_og_image_url", _must_not_be_called)
    monkeypatch.setattr(
        research_engine,
        "download_and_crop_image",
        lambda url, path, **k: path.write_bytes(b"x") or True,
    )
    data = {
        "spots": [
            {
                "id": "spot_1",
                "name": "テスト店",
                "url": "https://shop.example/",
                "photo_urls": ["https://cdn.example/a.jpg"],
            }
        ]
    }

    assert research_engine.attach_photos(data, tmp_path) == 1
    assert data["spots"][0]["photos"][0]["photo_source"] == "llm"


@pytest.mark.parametrize(
    "html_text,expected",
    [
        ('<meta property="og:image" content="/img/hero.jpg">', "https://shop.example/img/hero.jpg"),
        ('<meta name="twitter:image" content="https://cdn.example/t.png">', "https://cdn.example/t.png"),
        # og:image を twitter:image より優先する
        (
            '<meta name="twitter:image" content="https://cdn.example/t.png">'
            '<meta property="og:image" content="https://cdn.example/og.png">',
            "https://cdn.example/og.png",
        ),
        # 属性の順序が逆でも、HTMLエンティティが入っていても読む
        ('<meta content="https://cdn.example/i.jpg?a=1&amp;b=2" property="og:image">', "https://cdn.example/i.jpg?a=1&b=2"),
        ('<meta name="description" content="画像なし">', None),
    ],
)
def test_og_image_extraction(monkeypatch, html_text, expected):
    monkeypatch.setattr(research_engine, "is_public_http_url", lambda url: url.startswith("http"))
    monkeypatch.setattr(research_engine, "_fetch_html_head", lambda url: ("https://shop.example/", html_text))

    assert research_engine.fetch_og_image_url("https://shop.example/") == expected


def test_og_image_pointing_at_an_internal_address_is_rejected(monkeypatch):
    """LLM 応答に限らず、外部ページが宣言した画像URLも untrusted として検証する

    ページ自体の URL 検証は通し、og:image 側の検証だけを見る。
    入口で弾かれて通ってしまう (= 検証に到達しない) と偽の緑になるため、
    is_public_http_url は「内部アドレスだけ拒否する」stub に差し替える。
    """
    monkeypatch.setattr(
        research_engine,
        "is_public_http_url",
        lambda url: url.startswith("http") and "169.254.169.254" not in url,
    )
    monkeypatch.setattr(
        research_engine,
        "_fetch_html_head",
        lambda url: ("https://shop.example/", '<meta property="og:image" content="http://169.254.169.254/latest/">'),
    )
    assert research_engine.fetch_og_image_url("https://shop.example/") is None


def test_og_fetch_is_skipped_for_urls_we_must_not_open(monkeypatch):
    def _must_not_be_called(*a, **k):
        raise AssertionError("許可しないURLなのにページ取得が走った")

    monkeypatch.setattr(research_engine, "_fetch_html_head", _must_not_be_called)

    assert research_engine.fetch_og_image_url("") is None
    assert research_engine.fetch_og_image_url("file:///etc/passwd") is None
    assert research_engine.fetch_og_image_url("http://127.0.0.1/") is None
