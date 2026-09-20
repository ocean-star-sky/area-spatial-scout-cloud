"""Gemini 呼び出しの回帰テスト。

修正前の事故:
  - モデル名 gemini-1.5-flash / gemini-2.0-flash が実 API で 404 になり調査が一度も動かなかった
  - README が謳う Google Search グラウンディングが payload に入っていなかった
"""

import io
import json
import urllib.error

import pytest

import research_engine
from research_engine import ResearchUnavailable, classify_genre, extract_json_from_text, filter_by_genre


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
        return _FakeResponse(_gemini_body('```json {"spots": [{"name": "テスト鮨"}]} ```'))

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
