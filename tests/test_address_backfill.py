"""住所の追加取得 (backfill) の回帰テスト。

背景: 1リクエストで全項目を埋めさせると住所が空で返ることが多い。
実測 (新橋 × サウナ, gemini-2.5-flash-lite):
  1回目の一括調査        8件中 4件が address="" だった
  住所だけを1回聞き直す   空だった4件のうち3件を補完できた
住所が無いとエリア一致の検証も地図掲載もできないため、エリア判定の前に補完する。

補完できなかったものは空のまま残す。ここで推測住所を入れては、捏造を廃した意味が無い。
"""

import json

import pytest

import research_engine
from research_engine import backfill_addresses, build_address_backfill_prompt


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
            rows = [{"name": n, "address": a} for n, a in state["results"].items()]
            body = "```json\n" + json.dumps({"results": rows}, ensure_ascii=False) + "\n```"
        return {"candidates": [{"content": {"parts": [{"text": body}]}}]}

    monkeypatch.setattr(research_engine, "call_gemini", _fake_call)
    monkeypatch.setattr(research_engine, "ADDRESS_BACKFILL_ENABLED", True)
    return state


def _spots():
    return [
        {"name": "アスティル", "address": "東京都港区新橋3-12-3"},
        {"name": "91° SAUNA", "address": ""},
        {"name": "玄柳 -GENRYU", "address": ""},
    ]


def test_fills_only_missing_addresses(gemini_stub):
    gemini_stub["results"] = {"91° SAUNA": "東京都中央区銀座7-2-18", "玄柳 -GENRYU": ""}
    spots = _spots()
    filled = backfill_addresses(spots, "新橋", "サウナ", "key")

    assert filled == 1
    assert spots[1]["address"] == "東京都中央区銀座7-2-18"
    assert spots[1]["address_source"] == "backfill"
    # 取得できなかったものは空のまま (推測で埋めない)
    assert spots[2]["address"] == ""
    assert "address_source" not in spots[2]


def test_never_overwrites_an_existing_address(gemini_stub):
    """1回目で得た住所を、後からの薄い根拠で置き換えない"""
    gemini_stub["results"] = {"アスティル": "東京都渋谷区でたらめ1-1-1"}
    spots = _spots()
    backfill_addresses(spots, "新橋", "サウナ", "key")
    assert spots[0]["address"] == "東京都港区新橋3-12-3"


def test_duplicate_rows_do_not_overwrite_the_first_answer(gemini_stub):
    """同じ施設が応答に2回出ても、最初に採った住所を後の行で上書きしないこと。

    「住所が空のものだけ問い合わせる」という絞り込みだけでは、この経路は塞げない
    (1行目で埋まった後、2行目が同じスポットに当たる)。
    """
    gemini_stub["text"] = (
        "```json\n"
        '{"results": ['
        '{"name": "91° SAUNA", "address": "東京都中央区銀座7-2-18"},'
        '{"name": "91° SAUNA", "address": "東京都渋谷区でたらめ9-9-9"}'
        "]}\n```"
    )
    spots = _spots()
    filled = backfill_addresses(spots, "新橋", "サウナ", "key")
    assert filled == 1, "同じスポットを二重に数えている"
    assert spots[1]["address"] == "東京都中央区銀座7-2-18"


def test_only_missing_names_are_asked(gemini_stub):
    gemini_stub["results"] = {}
    backfill_addresses(_spots(), "新橋", "サウナ", "key")
    prompt = gemini_stub["prompts"][0]
    assert "91° SAUNA" in prompt and "玄柳 -GENRYU" in prompt
    assert "- アスティル" not in prompt, "既に住所があるスポットまで問い合わせている"


def test_no_request_when_nothing_is_missing(gemini_stub):
    spots = [{"name": "アスティル", "address": "東京都港区新橋3-12-3"}]
    assert backfill_addresses(spots, "新橋", "サウナ", "key") == 0
    assert gemini_stub["prompts"] == [], "補完不要なのにリクエストを出している"


@pytest.mark.parametrize(
    ("returned_name", "should_match"),
    [("91° SAUNA", True), ("91°SAUNA", True), ("９１° ＳＡＵＮＡ", True), ("別の店", False)],
)
def test_name_matching_absorbs_width_and_space_differences(gemini_stub, returned_name, should_match):
    gemini_stub["results"] = {returned_name: "東京都中央区銀座7-2-18"}
    spots = _spots()
    backfill_addresses(spots, "新橋", "サウナ", "key")
    assert bool(spots[1]["address"]) is should_match


def test_unknown_names_in_response_are_ignored(gemini_stub):
    """入力していない施設を勝手に足されても取り込まないこと"""
    gemini_stub["results"] = {"知らない店": "東京都港区新橋1-1-1"}
    spots = _spots()
    assert backfill_addresses(spots, "新橋", "サウナ", "key") == 0
    assert all(s.get("address") == "" for s in spots[1:])


def test_api_failure_is_not_fatal(gemini_stub):
    """補完は付加価値。失敗しても調査本体を巻き込まない"""
    gemini_stub["raise_times"] = len(research_engine.DEFAULT_MODELS)
    spots = _spots()
    assert backfill_addresses(spots, "新橋", "サウナ", "key") == 0
    assert spots[1]["address"] == ""


def test_unparsable_response_is_not_fatal(gemini_stub):
    gemini_stub["text"] = "これはJSONではありません"
    spots = _spots()
    assert backfill_addresses(spots, "新橋", "サウナ", "key") == 0


def test_can_be_disabled(monkeypatch, gemini_stub):
    """モデル別の日次上限を使い切る環境では止められること"""
    monkeypatch.setattr(research_engine, "ADDRESS_BACKFILL_ENABLED", False)
    assert backfill_addresses(_spots(), "新橋", "サウナ", "key") == 0
    assert gemini_stub["prompts"] == []


def test_request_count_is_capped(monkeypatch, gemini_stub):
    monkeypatch.setattr(research_engine, "ADDRESS_BACKFILL_MAX", 2)
    gemini_stub["results"] = {}
    spots = [{"name": f"店{i}", "address": ""} for i in range(5)]
    backfill_addresses(spots, "新橋", "サウナ", "key")
    prompt = gemini_stub["prompts"][0]
    # 【厳守】の箇条書きも "- " で始まるため、件数は施設名で数える
    assert "- 店0" in prompt and "- 店1" in prompt
    assert "- 店2" not in prompt, "上限を超えて問い合わせている"


def test_prompt_forbids_inventing_addresses():
    prompt = build_address_backfill_prompt(["店A"], "新橋", "サウナ")
    assert "推測で住所を書かないこと" in prompt
    assert '空文字 ""' in prompt


def test_backfill_runs_before_area_verification(monkeypatch):
    """補完した住所がエリア判定に使われること (順序が逆だと意味がない)"""
    order = []

    def _fake_backfill(spots, area, theme, key, models=None):
        order.append("backfill")
        spots[0]["address"] = "東京都港区新橋3-12-3"
        return 1

    def _fake_verify(spots, area, radius_km=None):
        order.append("verify")
        assert spots[0]["address"] == "東京都港区新橋3-12-3", "補完前にエリア判定が走っている"
        return spots, []

    monkeypatch.setattr(research_engine, "backfill_addresses", _fake_backfill)
    monkeypatch.setattr(research_engine, "verify_area_match", _fake_verify)
    monkeypatch.setattr(
        research_engine,
        "call_gemini",
        lambda *a, **k: {
            "candidates": [
                {"content": {"parts": [{"text": '```json {"spots":[{"name":"鮨 一番","address":""}]} ```'}]}}
            ]
        },
    )
    data = research_engine.run_autonomous_research(area="新橋", theme="鮨", count=3, api_key="k")
    assert order == ["backfill", "verify"]
    assert data["meta"]["address_backfill_attempted"] == 1
