#!/usr/bin/env python3
"""
geocoding.py
住所 → 緯度経度の解決と距離計算。調査エンジン (エリア一致の検証) と
レポートエンジン (地図描画) の両方から使うため独立させている。

注意: 国土地理院の住所検索に「新橋」「有明」のような地名だけを渡してはいけない。
全国の同名地点のうち先頭が返るため、実測では
  新橋 -> (38.44, 141.29) 宮城県 / 有明 -> (43.03, 144.85) 北海道 / 梅田 -> 青森県
となる。エリアの基準座標は、必ず「都道府県から始まる完全な住所」から求めること。
"""

import json
import math
import urllib.parse
import urllib.request

GSI_ENDPOINT = "https://msearch.gsi.go.jp/address-search/AddressSearch"
USER_AGENT = "AreaSpatialScout/3.1"
GEOCODE_TIMEOUT = 3.0


def geocode_address(address: str, timeout: float = GEOCODE_TIMEOUT) -> tuple[float, float] | None:
    """国土地理院APIで住所から緯度経度を取得する。解決できなければ None。"""
    if not address or not address.strip():
        return None
    url = f"{GSI_ENDPOINT}?q={urllib.parse.quote(address.strip())}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            data = json.loads(res.read().decode("utf-8"))
        if data:
            coords = data[0]["geometry"]["coordinates"]
            return float(coords[1]), float(coords[0])
    except Exception as e:
        print(f"[Geocode] 解決できません ({address[:30]}): {type(e).__name__}")
    return None


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """2地点間の大円距離 (km)"""
    radius = 6371.0
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    d_lat = lat2 - lat1
    d_lon = math.radians(b[1] - a[1])
    h = math.sin(d_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(d_lon / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(h))
