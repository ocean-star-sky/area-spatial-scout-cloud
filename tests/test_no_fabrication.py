"""「調査できなかったのに、それらしい成果物を返す」経路が無いことの回帰テスト。

修正前は Gemini が 404/429 でも build_intelligent_fallback_data() の架空スポットと
創作クチコミで 200 success を返していた。
"""

import re

import pytest
from conftest import REPO_ROOT, TEST_PASSWORD

import research_engine
from research_engine import ResearchUnavailable

# 架空データ生成に使われていた識別子。復活したら気付けるようにする。
FABRICATION_SYMBOLS = [
    "build_intelligent_fallback_data",
    "_generate_unique_reviews",
    "_generate_key_topics",
    "_generate_popular_times",
    "landmark_names",
    "sauna_pool",
    "sushi_pool",
    "coworking_pool",
    "general_pool",
]

SOURCE_FILES = [p for p in REPO_ROOT.glob("*.py")]


def test_research_failure_is_not_reported_as_success(client, app_module, monkeypatch):
    def _boom(*args, **kwargs):
        raise ResearchUnavailable("Gemini から調査結果を取得できませんでした", ["model-x: HTTP 429"])

    monkeypatch.setattr(app_module, "run_autonomous_research", _boom)

    res = client.post(
        "/api/scout/instant",
        json={"area": "どこか", "theme": "鮨", "count": 3, "password": TEST_PASSWORD},
    )
    assert res.status_code == 502
    detail = res.json()["detail"]
    assert "取得できませんでした" in detail["message"]
    assert any("429" in r for r in detail["reasons"])


def test_failed_job_leaves_no_partial_artifacts(client, app_module, monkeypatch):
    monkeypatch.setattr(
        app_module,
        "run_autonomous_research",
        lambda *a, **k: (_ for _ in ()).throw(ResearchUnavailable("失敗")),
    )
    client.post(
        "/api/scout/instant",
        json={"area": "どこか", "theme": "鮨", "count": 3, "password": TEST_PASSWORD},
    )
    assert list(app_module.OUTPUTS_DIR.glob("*")) == []


def test_missing_api_key_raises_instead_of_fabricating(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(ResearchUnavailable):
        research_engine.run_autonomous_research(area="銀座", theme="鮨", count=3)


def test_empty_spot_list_raises(monkeypatch):
    monkeypatch.setattr(research_engine, "call_gemini", lambda *a, **k: {})
    monkeypatch.setattr(
        research_engine,
        "_extract_text_and_sources",
        lambda _res: ('```json {"meta": {}, "spots": []} ```', [], []),
    )
    with pytest.raises(ResearchUnavailable):
        research_engine.run_autonomous_research(area="銀座", theme="鮨", count=3, api_key="dummy")


@pytest.mark.parametrize("symbol", FABRICATION_SYMBOLS)
def test_fabrication_helpers_are_gone(symbol):
    hits = [p.name for p in SOURCE_FILES if re.search(rf"\b{re.escape(symbol)}\b", p.read_text(encoding="utf-8"))]
    assert not hits, f"架空データ生成の識別子 '{symbol}' が {hits} に残っている"


def test_no_bundled_stock_photos():
    """実店舗名のキャプションを付けて流用していたストック写真が残っていないこと"""
    assert not (REPO_ROOT / "stock_photos").exists()


def test_photo_captions_cite_their_source(tmp_path, monkeypatch):
    """写真を付けるのは実URLを取得できたときだけで、出典を明記すること"""
    monkeypatch.setattr(research_engine, "download_and_crop_image", lambda url, path, **k: path.write_bytes(b"x") or True)
    data = {"spots": [{"id": "spot_1", "name": "テスト店", "photo_urls": ["https://example.com/a.jpg"]}]}
    assert research_engine.attach_photos(data, tmp_path) == 1
    assert "example.com" in data["spots"][0]["photos"][0]["caption"]


def test_no_photos_when_no_urls(tmp_path):
    data = {"spots": [{"id": "spot_1", "name": "テスト店", "photo_urls": []}]}
    assert research_engine.attach_photos(data, tmp_path) == 0
    assert "photos" not in data["spots"][0]


def test_reviews_without_a_source_are_not_merged():
    """出典を示せないクチコミは、件数を埋めるためであっても採らない"""
    spot = {"reviews": []}

    added = research_engine.merge_reviews(
        spot,
        ["出典のない声", {"text": "出典欄が空", "source_url": ""}, {"text": "出典あり", "source_url": "https://ok.example/1"}],
    )

    assert added == 1
    assert spot["reviews"] == ["出典あり"]


def test_ungrounded_response_carries_no_reviews(monkeypatch):
    """検索が走らなかった応答の「利用者の声」は実在の裏取りが無いので載せない。

    実測 (9/20 五反田×韓国料理): 検索クエリ 0件 / 出典 0件 のまま成果物になっていた。
    """
    monkeypatch.setattr(research_engine, "call_gemini", lambda *a, **k: {})
    monkeypatch.setattr(
        research_engine,
        "_extract_text_and_sources",
        lambda _res: ('```json {"spots": [{"name": "裏取り無し鮨", "reviews": ["よかった", "また行きたい"]}]} ```', [], []),
    )

    data = research_engine.run_autonomous_research(area="銀座", theme="鮨", count=3, api_key="dummy")

    assert data["meta"]["grounding_status"] == "ungrounded"
    assert data["spots"][0]["reviews"] == []
    assert data["meta"]["reviews_total"] == 0
