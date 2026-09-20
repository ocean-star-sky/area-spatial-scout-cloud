"""エリア一致検証の回帰テスト。

背景: 「新橋 × サウナ」の実行で、かるまる池袋・ウェルビー新宿店といった
別エリアの施設が混ざっていた。ジャンルのフィルタはあったが、所在地の検証が無かった。

基準座標の取り方に落とし穴がある。国土地理院の住所検索に地名だけを渡すと全国の
同名地点の先頭が返り、実測では 新橋 -> 宮城県 / 有明 -> 北海道 / 梅田 -> 青森県 だった。
そのため「住所にエリア名を含むスポットの重心」を基準にしている。ここではその
設計が守られていることを固定する。
"""

import pytest

import geocoding
import research_engine
from research_engine import verify_area_match

# 実測値 (国土地理院)。新橋基準の距離は
#   銀座 0.5km / 虎ノ門 0.7km / 六本木 2.3km / 渋谷 5.2km / 新宿 5.6km / 池袋 8.0km
COORDS = {
    "東京都港区新橋3-12-3": (35.66605, 139.756363),
    "東京都港区新橋2-15-14": (35.66620, 139.756000),
    "東京都中央区銀座8-7-6": (35.668453, 139.761292),
    "東京都港区六本木7-14-4": (35.66480, 139.72900),
    "東京都豊島区東池袋1-30-6": (35.731102, 139.716141),
    "東京都新宿区歌舞伎町1-1-2": (35.69500, 139.70200),
}


@pytest.fixture(autouse=True)
def _offline_geocoder(monkeypatch):
    """ジオコーディングを実測値の辞書に差し替え、ネットワークなしで検証する"""
    calls = []

    def _fake(address, timeout=None):
        calls.append(address)
        return COORDS.get((address or "").strip())

    monkeypatch.setattr(research_engine, "geocode_address", _fake)
    return calls


def _spot(name, address):
    return {"name": name, "address": address}


def test_spot_in_the_area_is_kept_by_address():
    spots = [_spot("アスティル", "東京都港区新橋3-12-3")]
    kept, dropped = verify_area_match(spots, "新橋")
    assert dropped == []
    assert kept[0]["area_match"] == "address"


def test_far_away_spots_are_excluded():
    """実際に起きた誤混入 (新橋の調査に池袋・新宿が入る) を落とすこと"""
    spots = [
        _spot("アスティル", "東京都港区新橋3-12-3"),
        _spot("かるまる池袋", "東京都豊島区東池袋1-30-6"),
        _spot("ウェルビー新宿店", "東京都新宿区歌舞伎町1-1-2"),
    ]
    kept, dropped = verify_area_match(spots, "新橋")
    assert [s["name"] for s in kept] == ["アスティル"]
    assert sorted(s["name"] for s in dropped) == ["かるまる池袋", "ウェルビー新宿店"]
    assert all(s["area_match"] == "outside" for s in dropped)
    assert all(s["distance_km"] > 3.0 for s in dropped)


def test_adjacent_area_is_kept_by_proximity():
    """隣接エリア (新橋に対する銀座 0.5km) は落とさない"""
    spots = [
        _spot("アスティル", "東京都港区新橋3-12-3"),
        _spot("銀座の店", "東京都中央区銀座8-7-6"),
        _spot("六本木の店", "東京都港区六本木7-14-4"),
    ]
    kept, dropped = verify_area_match(spots, "新橋")
    assert dropped == []
    assert kept[1]["area_match"] == "proximity"
    assert kept[1]["distance_km"] < 3.0
    assert kept[2]["distance_km"] < 3.0


def test_radius_is_configurable():
    spots = [
        _spot("アスティル", "東京都港区新橋3-12-3"),
        _spot("六本木の店", "東京都港区六本木7-14-4"),
    ]
    kept, dropped = verify_area_match(spots, "新橋", radius_km=1.0)
    assert [s["name"] for s in dropped] == ["六本木の店"]
    assert len(kept) == 1


def test_spots_without_address_are_kept_but_flagged():
    """住所が無いものは判定できない。憶測で落とさず、未確認として残す"""
    spots = [_spot("アスティル", "東京都港区新橋3-12-3"), _spot("住所不明の店", "")]
    kept, dropped = verify_area_match(spots, "新橋")
    assert dropped == []
    assert kept[1]["area_match"] == "unverified"


def test_nothing_is_dropped_when_no_anchor_can_be_established():
    """基準座標を作れない場合は、遠近を判断する根拠が無いので誰も落とさない"""
    spots = [_spot("店A", ""), _spot("店B", "")]
    kept, dropped = verify_area_match(spots, "新橋")
    assert dropped == []
    assert len(kept) == 2
    assert all(s["area_match"] == "unverified" for s in kept)


def test_area_name_alone_is_never_geocoded(_offline_geocoder):
    """地名だけを国土地理院へ渡さないこと。

    実測では「新橋」単独で宮城県の座標が返るため、これを基準にすると
    東京の全スポットが「別エリア」として落ちる。
    """
    spots = [
        _spot("アスティル", "東京都港区新橋3-12-3"),
        _spot("かるまる池袋", "東京都豊島区東池袋1-30-6"),
    ]
    verify_area_match(spots, "新橋")
    assert "新橋" not in _offline_geocoder, "エリア名だけでジオコーディングしている"
    assert all(a.startswith("東京都") for a in _offline_geocoder)


@pytest.mark.parametrize(
    ("area", "expected"),
    [("新橋駅", "新橋"), ("新橋エリア", "新橋"), ("新橋周辺", "新橋"), ("新橋", "新橋")],
)
def test_area_tokens_strip_common_suffixes(area, expected):
    assert expected in research_engine._area_tokens(area)


def test_geocoded_coordinates_are_reused_by_the_map():
    """検証で解決した座標をスポットに残し、地図生成で再取得しないこと"""
    spots = [_spot("アスティル", "東京都港区新橋3-12-3")]
    kept, _ = verify_area_match(spots, "新橋")
    assert kept[0]["lat"] == pytest.approx(35.66605)
    assert kept[0]["lon"] == pytest.approx(139.756363)


def test_real_geocoder_rejects_bare_place_name_as_reference():
    """(ドキュメント兼防止) 地名単独の解決結果は東京ではないことを明示する。

    ネットワークに出ないよう、実測で得た値をそのまま記録して比較する。
    """
    measured_shinbashi_alone = (38.439537, 141.289337)  # 宮城県
    tokyo_shinbashi = (35.66605, 139.756363)
    assert geocoding.haversine_km(measured_shinbashi_alone, tokyo_shinbashi) > 300
