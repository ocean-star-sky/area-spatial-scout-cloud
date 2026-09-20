"""クチコミの追加取得 (backfill) の回帰テスト。

背景 (実測 9/20、新橋×サウナ / 銀座×個室接待鮨 / 五反田×韓国料理 の3ジョブ):
  1回目の一括調査は 1 施設あたりクチコミ 2件しか返さなかった。
  プロンプトに件数の指示が無く、出力スキーマの例が2件だけだったため、
  モデルが例の形をなぞっていた。レポートの表示枠は 8件ある。

1回目の応答にクチコミまで詰め込むと maxOutputTokens (8192) で JSON が途切れ、
切り詰め修復でスポットごと落ちる。そのため住所と同じく、対象を絞って聞き直す。

聞き直しても見つからないものは空のまま残す。ここで創作クチコミを入れては、
捏造を廃した意味が無い (出典URLを示せない声は採らない)。
"""

import json

import pytest

import research_engine
from research_engine import backfill_reviews, build_review_backfill_prompt, normalize_spot_reviews


@pytest.fixture
def gemini_stub(monkeypatch):
    """call_gemini を差し替え、送ったプロンプトと返す結果を制御する"""
    state = {"prompts": [], "results": {}, "raise_times": 0, "text": None}

    def _fake_call(model, prompt, api_key, timeout=None):
        state["prompts"].append(prompt)
        if state["raise_times"] > 0:
            state["raise_times"] -= 1
            raise RuntimeError("boom")
        body = state["text"]
        if body is None:
            rows = [{"name": n, "reviews": r} for n, r in state["results"].items()]
            body = "```json\n" + json.dumps({"results": rows}, ensure_ascii=False) + "\n```"
        return {"candidates": [{"content": {"parts": [{"text": body}]}}]}

    monkeypatch.setattr(research_engine, "call_gemini", _fake_call)
    monkeypatch.setattr(research_engine, "REVIEW_BACKFILL_ENABLED", True)
    return state


def _rev(text, source="https://tabelog.example/a/1"):
    return {"text": text, "source_url": source}


def _spots():
    return [
        {"name": "アスティル", "reviews": ["整い方が丁寧だった"]},
        {"name": "91° SAUNA", "reviews": []},
    ]


def test_appends_reviews_with_their_source(gemini_stub):
    gemini_stub["results"] = {
        "91° SAUNA": [_rev("水風呂が広く回転が良い", "https://x.example/1"), _rev("朝が空いている", "https://y.example/2")]
    }
    spots = _spots()

    added = backfill_reviews(spots, "新橋", "サウナ", "key")

    assert added == 2
    assert spots[1]["reviews"] == ["水風呂が広く回転が良い", "朝が空いている"]
    assert spots[1]["review_sources"] == ["https://x.example/1", "https://y.example/2"]


def test_reviews_without_a_source_url_are_not_taken(gemini_stub):
    """出典を示せない声は実在を確認できない。件数を埋めるために採らない。"""
    gemini_stub["results"] = {
        "91° SAUNA": [
            {"text": "とても良かった"},
            {"text": "きれいだった", "source_url": ""},
            {"text": "出典あり", "source_url": "https://ok.example/1"},
        ]
    }
    spots = _spots()

    assert backfill_reviews(spots, "新橋", "サウナ", "key") == 1
    assert spots[1]["reviews"] == ["出典あり"]


def test_plain_string_reviews_in_the_response_are_not_taken(gemini_stub):
    """文字列だけで返ってきた声も出典が無いので採らない"""
    gemini_stub["text"] = '```json {"results": [{"name": "91° SAUNA", "reviews": ["出典のない声"]}]} ```'
    spots = _spots()

    assert backfill_reviews(spots, "新橋", "サウナ", "key") == 0
    assert spots[1]["reviews"] == []


def test_existing_reviews_are_kept_and_duplicates_are_skipped(gemini_stub):
    gemini_stub["results"] = {
        "アスティル": [_rev("整い方が丁寧だった"), _rev("整い方が 丁寧だった"), _rev("新しい声")],
    }
    spots = _spots()

    added = backfill_reviews(spots, "新橋", "サウナ", "key")

    # 1回目の応答で得た声は消さず、表記の揺れだけの重複は足さない
    assert added == 1
    assert spots[0]["reviews"] == ["整い方が丁寧だった", "新しい声"]


def test_only_spots_below_the_target_are_asked(gemini_stub, monkeypatch):
    monkeypatch.setattr(research_engine, "REVIEWS_TARGET", 2)
    spots = [
        {"name": "足りている店", "reviews": ["声1", "声2"]},
        {"name": "足りない店", "reviews": ["声1"]},
    ]

    backfill_reviews(spots, "新橋", "サウナ", "key")

    assert "足りない店" in gemini_stub["prompts"][0]
    assert "足りている店" not in gemini_stub["prompts"][0]


def test_no_request_when_every_spot_already_has_enough(gemini_stub, monkeypatch):
    monkeypatch.setattr(research_engine, "REVIEWS_TARGET", 1)
    spots = [{"name": "足りている店", "reviews": ["声1"]}]

    assert backfill_reviews(spots, "新橋", "サウナ", "key") == 0
    assert gemini_stub["prompts"] == []


def test_target_count_is_not_exceeded(gemini_stub, monkeypatch):
    monkeypatch.setattr(research_engine, "REVIEWS_TARGET", 3)
    gemini_stub["results"] = {"91° SAUNA": [_rev(f"声{i}", f"https://x.example/{i}") for i in range(10)]}
    spots = _spots()

    backfill_reviews(spots, "新橋", "サウナ", "key")

    assert len(spots[1]["reviews"]) == 3
    assert len(spots[1]["review_sources"]) == 3


@pytest.mark.parametrize(
    "returned_name,should_match",
    [
        ("91° SAUNA", True),
        ("91°　SAUNA", True),
        ("91° sauna", True),
        ("別のサウナ", False),
    ],
)
def test_name_matching_absorbs_width_and_space_differences(gemini_stub, returned_name, should_match):
    gemini_stub["results"] = {returned_name: [_rev("水風呂が広い")]}
    spots = _spots()

    added = backfill_reviews(spots, "新橋", "サウナ", "key")

    assert added == (1 if should_match else 0)


def test_unknown_names_in_response_are_ignored(gemini_stub):
    gemini_stub["results"] = {"存在しない店": [_rev("架空の声")]}
    spots = _spots()

    assert backfill_reviews(spots, "新橋", "サウナ", "key") == 0
    assert spots[1]["reviews"] == []


def test_api_failure_is_not_fatal(gemini_stub):
    """補完は付加価値。失敗しても調査本体を落とさない。"""
    gemini_stub["raise_times"] = len(research_engine.DEFAULT_MODELS)
    spots = _spots()

    assert backfill_reviews(spots, "新橋", "サウナ", "key") == 0
    assert spots[0]["reviews"] == ["整い方が丁寧だった"]


def test_unparsable_response_is_not_fatal(gemini_stub):
    gemini_stub["text"] = "JSONではない返答"
    spots = _spots()

    assert backfill_reviews(spots, "新橋", "サウナ", "key") == 0


def test_can_be_disabled(monkeypatch, gemini_stub):
    """Gemini のモデル別日次上限を使い切る環境では無効化できること"""
    monkeypatch.setattr(research_engine, "REVIEW_BACKFILL_ENABLED", False)

    assert backfill_reviews(_spots(), "新橋", "サウナ", "key") == 0
    assert gemini_stub["prompts"] == []


def test_request_count_is_capped(monkeypatch, gemini_stub):
    monkeypatch.setattr(research_engine, "REVIEW_BACKFILL_MAX", 2)
    spots = [{"name": f"店{i}", "reviews": []} for i in range(5)]

    backfill_reviews(spots, "新橋", "サウナ", "key")

    prompt = gemini_stub["prompts"][0]
    assert prompt.count("- 店") == 2


def test_prompt_demands_a_source_and_forbids_invention():
    prompt = build_review_backfill_prompt(["91° SAUNA"], "新橋", "サウナ", 8)

    assert "source_url" in prompt
    assert "創作は禁止" in prompt
    assert "空配列" in prompt
    assert "最大8件" in prompt


def test_dict_shaped_reviews_from_the_main_call_are_flattened():
    """レポート側は reviews の要素を文字列として扱うため、dict 形式は平坦化する"""
    spot = {"reviews": [{"text": "静かで良い", "source_url": "https://a.example/1"}, "出典なしの声", {"text": ""}]}

    normalize_spot_reviews(spot)

    assert spot["reviews"] == ["静かで良い", "出典なしの声"]
    assert spot["review_sources"] == ["https://a.example/1", ""]


def test_non_list_reviews_become_empty():
    spot = {"reviews": "クチコミは見つかりませんでした"}

    normalize_spot_reviews(spot)

    assert spot["reviews"] == []
    assert spot["review_sources"] == []
