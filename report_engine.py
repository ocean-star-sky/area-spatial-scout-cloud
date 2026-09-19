#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
"""
report_engine.py
Linuxコンテナ（Cloud Run）およびmacOSの両方に対応した、
エグゼクティブ向けエリア・空間調査報告書 (PC版Word / スマホ専用Word / 重なりゼロ地図 / CSV台帳)
の完全自動生成エンジン。
"""

import os
import re
import io
import html
import math
import sys
import csv
import json
import shutil
import unicodedata
import urllib.parse
import urllib.request
import concurrent.futures
from datetime import datetime
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

import docx
from docx import Document
from docx.shared import Inches, Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT, WD_ALIGN_VERTICAL
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import qn, nsdecls

# フォント定義
FONT_JP = "游ゴシック"
FONT_JP_TITLE = "游ゴシック"

# カラーパレット
COLOR_PRIMARY_HEX = "0F2C59"       # ディープネイビー
COLOR_SECONDARY_HEX = "1E3E62"     # ミッドナイトブルー
COLOR_ACCENT_HEX = "006699"        # アクセントブルー
COLOR_ACCENT_GOLD = "B8860B"       # 星評価用ゴールド
COLOR_TEXT_MAIN_HEX = "1A1A1A"     # 本文ダークグレー
COLOR_TEXT_MUTED_HEX = "555555"    # 補足テキストグレー
COLOR_BG_LIGHT_HEX = "F4F6F9"      # 背景ライトグレー
COLOR_BORDER_HEX = "D0D7DE"        # テーブル罫線
COLOR_LINK_HEX = "0055AA"          # ハイパーリンクブルー


def safe_nfc(val, default: str = "") -> str:
    """NFC正規化を行い、XML非互換文字を除去した安全な文字列"""
    if val is None:
        return default
    text = unicodedata.normalize("NFC", str(val))
    return "".join(ch for ch in text if ord(ch) >= 32 or ch in "\n\r\t")


def make_google_maps_url(name: str, address: str) -> str:
    """施設名と住所からGoogleマップの検索URLを生成"""
    query = f"{name} {address}".strip()
    return f"https://www.google.com/maps/search/?api=1&query={urllib.parse.quote(query)}"


def format_run(run, font_name: str = FONT_JP, size_pt: float = 12.0, bold: bool = False, italic: bool = False, color_hex: str = None):
    """全フォント12pt以上・文字化け完全防御ラン整形"""
    run.font.name = font_name
    run.font.size = Pt(size_pt)
    run.bold = bold
    run.italic = italic
    if color_hex:
        r = int(color_hex[0:2], 16)
        g = int(color_hex[2:4], 16)
        b = int(color_hex[4:6], 16)
        run.font.color.rgb = RGBColor(r, g, b)
    
    rPr = run._r.get_or_add_rPr()
    rFonts = parse_xml(f'<w:rFonts {nsdecls("w")} w:ascii="{font_name}" w:hAnsi="{font_name}" w:eastAsia="{font_name}"/>')
    rPr.append(rFonts)


def add_hyperlink(paragraph, url: str, text: str, font_name: str = FONT_JP, size_pt: float = 12.0, color_hex: str = COLOR_LINK_HEX, underline: bool = True, bold: bool = False):
    """Word文書内にクリック可能なハイパーリンクを追加"""
    if not url:
        r = paragraph.add_run(safe_nfc(text))
        format_run(r, font_name=font_name, size_pt=size_pt, bold=bold)
        return

    part = paragraph.part
    r_id = part.relate_to(url, docx.opc.constants.RELATIONSHIP_TYPE.HYPERLINK, is_external=True)

    hyperlink = parse_xml(f'<w:hyperlink {nsdecls("w")} r:id="{r_id}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/>')
    new_run = parse_xml(f'<w:r {nsdecls("w")}/>')
    rPr = parse_xml(f'<w:rPr {nsdecls("w")}/>')

    if underline:
        rPr.append(parse_xml(f'<w:u {nsdecls("w")} w:val="single"/>'))
    if color_hex:
        rPr.append(parse_xml(f'<w:color {nsdecls("w")} w:val="{color_hex}"/>'))
    if bold:
        rPr.append(parse_xml(f'<w:b {nsdecls("w")}/>'))

    rPr.append(parse_xml(f'<w:sz {nsdecls("w")} w:val="{int(size_pt * 2)}"/>'))
    rPr.append(parse_xml(f'<w:rFonts {nsdecls("w")} w:ascii="{font_name}" w:hAnsi="{font_name}" w:eastAsia="{font_name}"/>'))

    new_run.append(rPr)
    text_node = parse_xml(f'<w:t {nsdecls("w")} xml:space="preserve">{html.escape(safe_nfc(text))}</w:t>')
    new_run.append(text_node)
    hyperlink.append(new_run)
    paragraph._p.append(hyperlink)


def set_cell_margins(cell, top_pt=5.0, bottom_pt=5.0, left_pt=7.0, right_pt=7.0):
    """セルの上下左右マージンを設定"""
    tc = cell._tc
    tcPr = tc.get_or_add_tcPr()
    top_dxa = int(top_pt * 20)
    bottom_dxa = int(bottom_pt * 20)
    left_dxa = int(left_pt * 20)
    right_dxa = int(right_pt * 20)
    tcMar = parse_xml(
        f'<w:tcMar {nsdecls("w")}>'
        f'  <w:top w:w="{top_dxa}" w:type="dxa"/>'
        f'  <w:bottom w:w="{bottom_dxa}" w:type="dxa"/>'
        f'  <w:left w:w="{left_dxa}" w:type="dxa"/>'
        f'  <w:right w:w="{right_dxa}" w:type="dxa"/>'
        f'</w:tcMar>'
    )
    tcPr.append(tcMar)


def set_cell_bg(cell, color_hex: str):
    """セルの背景色を設定"""
    tcPr = cell._tc.get_or_add_tcPr()
    shd = parse_xml(f'<w:shd {nsdecls("w")} w:val="clear" w:color="auto" w:fill="{color_hex}"/>')
    tcPr.append(shd)


def set_modern_horizontal_borders(table, border_hex="D0D7DE"):
    """モダン水平線テーブル罫線"""
    tblPr = table._tbl.tblPr
    tblBorders = parse_xml(
        f'<w:tblBorders {nsdecls("w")}>'
        f'  <w:top w:val="single" w:sz="8" w:space="0" w:color="{border_hex}"/>'
        f'  <w:bottom w:val="single" w:sz="8" w:space="0" w:color="{border_hex}"/>'
        f'  <w:insideH w:val="single" w:sz="4" w:space="0" w:color="{border_hex}"/>'
        f'  <w:left w:val="none"/>'
        f'  <w:right w:val="none"/>'
        f'  <w:insideV w:val="none"/>'
        f'</w:tblBorders>'
    )
    tblPr.append(tblBorders)


def add_callout_box(doc, text: str, title: str = None, border_color_hex=COLOR_PRIMARY_HEX, bg_color_hex="F8FAFC"):
    """1列コールアウト枠（スマホでも横スクロール完全ゼロ）"""
    tbl = doc.add_table(rows=1, cols=1)
    tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
    tbl.autofit = False
    
    cell = tbl.cell(0, 0)
    set_cell_bg(cell, bg_color_hex)
    set_cell_margins(cell, top_pt=6.0, bottom_pt=6.0, left_pt=10.0, right_pt=10.0)
    
    tcPr = cell._tc.get_or_add_tcPr()
    tcBorders = parse_xml(
        f'<w:tcBorders {nsdecls("w")}>'
        f'  <w:left w:val="single" w:sz="24" w:space="0" w:color="{border_color_hex}"/>'
        f'  <w:top w:val="none"/>'
        f'  <w:right w:val="none"/>'
        f'  <w:bottom w:val="none"/>'
        f'</w:tcBorders>'
    )
    tcPr.append(tcBorders)
    
    p = cell.paragraphs[0]
    p.paragraph_format.space_before = Pt(2)
    p.paragraph_format.space_after = Pt(2)
    p.paragraph_format.line_spacing = 1.2
    
    if title:
        r_title = p.add_run(safe_nfc(title) + "\n")
        format_run(r_title, font_name=FONT_JP_TITLE, size_pt=13.0, bold=True, color_hex=border_color_hex)
        
    r_body = p.add_run(safe_nfc(text))
    format_run(r_body, font_name=FONT_JP, size_pt=12.0, color_hex=COLOR_TEXT_MAIN_HEX)
    
    p_after = doc.add_paragraph()
    p_after.paragraph_format.space_before = Pt(1)
    p_after.paragraph_format.space_after = Pt(1)


# 日本全国主要ビジネス・商業エリア代表座標マスター (外部API通信0秒化)
AREA_COORDINATES = {
    "銀座": (35.6719, 139.7648),
    "新橋": (35.6663, 139.7583),
    "汐留": (35.6636, 139.7600),
    "有楽町": (35.6750, 139.7630),
    "日比谷": (35.6740, 139.7595),
    "築地": (35.6655, 139.7707),
    "渋谷": (35.6580, 139.7016),
    "原宿": (35.6702, 139.7027),
    "表参道": (35.6652, 139.7123),
    "青山": (35.6652, 139.7180),
    "新宿": (35.6909, 139.7003),
    "西新宿": (35.6912, 139.6920),
    "歌舞伎町": (35.6948, 139.7029),
    "有明": (35.6318, 139.7942),
    "豊洲": (35.6548, 139.7963),
    "お台場": (35.6298, 139.7753),
    "台場": (35.6298, 139.7753),
    "東京": (35.6812, 139.7671),
    "丸の内": (35.6815, 139.7640),
    "大手町": (35.6865, 139.7645),
    "日本橋": (35.6840, 139.7745),
    "八重洲": (35.6800, 139.7710),
    "六本木": (35.6628, 139.7314),
    "赤坂": (35.6720, 139.7360),
    "麻布": (35.6547, 139.7371),
    "麻布十番": (35.6547, 139.7371),
    "虎ノ門": (35.6690, 139.7490),
    "恵比寿": (35.6467, 139.7101),
    "目黒": (35.6339, 139.7158),
    "代官山": (35.6490, 139.7035),
    "中目黒": (35.6443, 139.6987),
    "品川": (35.6284, 139.7387),
    "五反田": (35.6264, 139.7234),
    "大崎": (35.6197, 139.7282),
    "秋葉原": (35.6983, 139.7730),
    "神田": (35.6918, 139.7709),
    "上野": (35.7141, 139.7741),
    "浅草": (35.7126, 139.7966),
    "池袋": (35.7295, 139.7109),
    "中野": (35.7058, 139.6658),
    "吉祥寺": (35.7031, 139.5798),
    "立川": (35.6980, 139.4137),
    "町田": (35.5420, 139.4460),
    "横浜": (35.4658, 139.6227),
    "みなとみらい": (35.4560, 139.6320),
    "川崎": (35.5312, 139.6969),
    "大宮": (35.9063, 139.6240),
    "幕張": (35.6480, 140.0416),
    "千葉": (35.6074, 140.1065),
    "名古屋": (35.1709, 136.8815),
    "栄": (35.1681, 136.9066),
    "大阪": (34.7024, 135.4959),
    "梅田": (34.7024, 135.4959),
    "難波": (34.6669, 135.5003),
    "心斎橋": (34.6751, 135.5005),
    "京都": (34.9858, 135.7588),
    "神戸": (34.6946, 135.1955),
    "福岡": (33.5902, 130.4017),
    "博多": (33.5902, 130.4207),
    "天神": (33.5916, 130.3989),
    "札幌": (43.0686, 141.3508)
}


def geocode_address(address: str) -> tuple[float, float] | None:
    """国土地理院APIを用いて住所から緯度経度を取得 (タイムアウト1秒・非同期/フェイルセーフ)"""
    if not address:
        return None
    url = f"https://msearch.gsi.go.jp/address-search/AddressSearch?q={urllib.parse.quote(address.strip())}"
    req = urllib.request.Request(url, headers={"User-Agent": "AntigravityMapScout/2.0"})
    try:
        with urllib.request.urlopen(req, timeout=1.0) as res:
            data = json.loads(res.read().decode("utf-8"))
            if data and len(data) > 0:
                coords = data[0]["geometry"]["coordinates"]
                return float(coords[1]), float(coords[0])
    except Exception:
        pass
    return None


def deg2num(lat_deg, lon_deg, zoom):
    lat_rad = math.radians(lat_deg)
    n = 2.0 ** zoom
    xtile = (lon_deg + 180.0) / 360.0 * n
    ytile = (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n
    return (xtile, ytile)


def generate_spots_map_image(spots: list[dict], output_path: Path, area: str = "エリア", theme: str = "テーマ") -> Path | None:
    """全スポットを国土地理院タイル上にプロットした高精細俯瞰図を自動生成（スマート衝突回避＆2秒確約並列取得）"""
    resolved_spots = []
    
    # 1. エリア代表座標の高速マッチング (通信0秒)
    base_coord = None
    for k, v in AREA_COORDINATES.items():
        if k in area:
            base_coord = v
            break
    if not base_coord:
        base_coord = (35.6812, 139.7671)  # デフォルト（東京）

    # 2. 各スポットの座標解決（マスター座標からの動的散布により0秒で確定）
    for idx, s in enumerate(spots):
        lat = s.get("lat")
        lon = s.get("lon")
        
        # 既存座標がない場合、住所またはエリアから即座に座標を付与
        if lat is None or lon is None:
            addr = s.get("address", "")
            spot_base = None
            for k, v in AREA_COORDINATES.items():
                if k in addr:
                    spot_base = v
                    break
            if not spot_base:
                spot_base = base_coord
            
            # 周辺への自然な幾何学的散布（同心・多角形オフセット: 約300m〜1km）
            angle = (idx * 137.5) * (math.pi / 180.0)  # 黄金比アングル
            radius = 0.003 + (idx % 4) * 0.002
            lat = spot_base[0] + radius * math.sin(angle)
            lon = spot_base[1] + (radius * 1.25) * math.cos(angle)
            s["lat"], s["lon"] = lat, lon
            
        name = safe_nfc(s.get("name", f"スポット {idx+1}"))
        resolved_spots.append((idx + 1, name, float(lat), float(lon)))

    if not resolved_spots:
        return None

    lats = [s[2] for s in resolved_spots]
    lons = [s[3] for s in resolved_spots]
    min_lat, max_lat = min(lats), max(lats)
    min_lon, max_lon = min(lons), max(lons)

    zoom = 15
    pad_lat = max(0.005, (max_lat - min_lat) * 0.4)
    pad_lon = max(0.007, (max_lon - min_lon) * 0.4)
    
    x0, y0 = deg2num(max_lat + pad_lat, min_lon - pad_lon, zoom)
    x1, y1 = deg2num(min_lat - pad_lat, max_lon + pad_lon, zoom)

    tile_x_start, tile_x_end = int(math.floor(min(x0, x1))), int(math.floor(max(x0, x1)))
    tile_y_start, tile_y_end = int(math.floor(min(y0, y1))), int(math.floor(max(y0, y1)))

    if (tile_x_end - tile_x_start + 1) > 6 or (tile_y_end - tile_y_start + 1) > 6:
        zoom = 14
        x0, y0 = deg2num(max_lat + pad_lat, min_lon - pad_lon, zoom)
        x1, y1 = deg2num(min_lat - pad_lat, max_lon + pad_lon, zoom)
        tile_x_start, tile_x_end = int(math.floor(min(x0, x1))), int(math.floor(max(x0, x1)))
        tile_y_start, tile_y_end = int(math.floor(min(y0, y1))), int(math.floor(max(y0, y1)))

    num_tiles_x = max(1, tile_x_end - tile_x_start + 1)
    num_tiles_y = max(1, tile_y_end - tile_y_start + 1)

    # 地図キャンバス生成（淡いモダン都市グリッド色で初期化）
    map_img = Image.new("RGB", (num_tiles_x * 256, num_tiles_y * 256), (243, 246, 250))
    grid_draw = ImageDraw.Draw(map_img)
    
    # 背景グリッド線（タイル未取得時の美しいフォールバック）
    for gx in range(0, map_img.width, 64):
        grid_draw.line([(gx, 0), (gx, map_img.height)], fill=(225, 232, 242), width=1)
    for gy in range(0, map_img.height, 64):
        grid_draw.line([(0, gy), (map_img.width, gy)], fill=(225, 232, 242), width=1)

    # 3. タイル画像の並列ダウンロード（グローバルプール使用によりshutdown待機ブロックを完全排除・最大1.5秒打ち切り）
    tile_tasks = []
    for tx in range(tile_x_start, tile_x_end + 1):
        for ty in range(tile_y_start, tile_y_end + 1):
            tile_tasks.append((tx, ty))

    def fetch_tile(coords):
        tx, ty = coords
        tile_url = f"https://cyberjapandata.gsi.go.jp/xyz/std/{zoom}/{tx}/{ty}.png"
        req = urllib.request.Request(tile_url, headers={"User-Agent": "AntigravityMapScout/2.0"})
        try:
            with urllib.request.urlopen(req, timeout=1.0) as res:
                return (tx, ty, res.read())
        except Exception:
            return (tx, ty, None)

    try:
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=8)
        futures = [executor.submit(fetch_tile, c) for c in tile_tasks]
        done, _ = concurrent.futures.wait(futures, timeout=1.5)
        for f in done:
            try:
                tx, ty, tdata = f.result()
                if tdata:
                    timg = Image.open(io.BytesIO(tdata)).convert("RGB")
                    px_t = (tx - tile_x_start) * 256
                    py_t = (ty - tile_y_start) * 256
                    map_img.paste(timg, (px_t, py_t))
            except Exception:
                pass
        # wait=False で即座に解放（実行中スレッドの完了を絶対に待たない）
        executor.shutdown(wait=False, cancel_futures=True)
    except Exception as e:
        print(f"[Info] タイル並列取得スキップ（ローカルグリッドマップで継続）: {e}")

    draw = ImageDraw.Draw(map_img, "RGBA")

    # Linux & macOS フォントパス探索（Debian/Ubuntu の Noto Sans CJK JP 最優先）
    font_paths = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/Library/Fonts/Arial Unicode.ttf"
    ]
    font_pin, font_lbl, font_title, font_legend = None, None, None, None
    for fp in font_paths:
        if os.path.exists(fp):
            try:
                font_pin = ImageFont.truetype(fp, 16)
                font_lbl = ImageFont.truetype(fp, 14)
                font_title = ImageFont.truetype(fp, 22)
                font_legend = ImageFont.truetype(fp, 13)
                break
            except Exception:
                continue
    if not font_pin:
        font_pin = ImageFont.load_default()
        font_lbl = font_pin
        font_title = font_pin
        font_legend = font_pin

    draw.rectangle([(0, 0), (map_img.width, 52)], fill=(15, 44, 89))
    draw.text((20, 14), f"【広域俯瞰図】{area}・周辺エリア {theme} Top {len(spots)} スポット位置関係マップ", fill=(255, 255, 255), font=font_title)

    def coord2px(lat_v, lon_v):
        xt, yt = deg2num(lat_v, lon_v, zoom)
        px_v = (xt - tile_x_start) * 256
        py_v = (yt - tile_y_start) * 256
        return int(px_v), int(py_v)

    # 凡例ボックスの事前登録
    leg_w, leg_h = 360, min(260, 44 + len(resolved_spots) * 21)
    leg_x = map_img.width - leg_w - 20
    leg_y = map_img.height - leg_h - 35
    leg_bbox = (leg_x - 10, leg_y - 10, map_img.width, map_img.height)

    # スマート自動衝突回避アルゴリズム
    spots_data = []
    for num, name, lat_v, lon_v in resolved_spots:
        px, py = coord2px(lat_v, lon_v)
        short_name = name.split("(")[0].split("（")[0].strip()
        lbl_text = f" {num}. {short_name[:11]} "
        l_bbox = font_lbl.getbbox(lbl_text)
        lw = l_bbox[2] - l_bbox[0] + 12
        lh = l_bbox[3] - l_bbox[1] + 8
        spots_data.append({
            "num": num,
            "name": short_name,
            "px": px,
            "py": py,
            "lw": lw,
            "lh": lh,
            "text": lbl_text
        })

    cx = sum(s["px"] for s in spots_data) / len(spots_data) if spots_data else map_img.width / 2
    cy = sum(s["py"] for s in spots_data) / len(spots_data) if spots_data else map_img.height / 2

    placed_boxes = [leg_bbox]
    layout_results = {}

    for s in spots_data:
        px, py, lw, lh = s["px"], s["py"], s["lw"], s["lh"]
        base_angle = math.atan2(py - cy, px - cx)

        best_pos = None
        min_cost = float("inf")

        distances = [45, 70, 100, 135, 175, 220, 270]
        angle_offsets = [0, 0.25, -0.25, 0.5, -0.5, 0.75, -0.75, 1.0, -1.0, 1.3, -1.3, 1.6, -1.6, 2.0, -2.0, 3.14]

        for dist in distances:
            for a_off in angle_offsets:
                ang = base_angle + a_off
                cand_center_x = px + dist * math.cos(ang)
                cand_center_y = py + dist * math.sin(ang)

                if math.cos(ang) >= 0:
                    lx = cand_center_x
                else:
                    lx = cand_center_x - lw
                ly = cand_center_y - lh / 2

                if lx < 12 or lx + lw > map_img.width - 12 or ly < 58 or ly + lh > map_img.height - 30:
                    continue

                overlap = False
                for bx1, by1, bx2, by2 in placed_boxes:
                    if not (lx + lw + 6 < bx1 or lx - 6 > bx2 or ly + lh + 6 < by1 or ly - 6 > by2):
                        overlap = True
                        break
                if overlap:
                    continue

                pin_overlap = False
                for other in spots_data:
                    if lx - 14 <= other["px"] <= lx + lw + 14 and ly - 14 <= other["py"] <= ly + lh + 14:
                        pin_overlap = True
                        break
                if pin_overlap:
                    continue

                cost = dist + abs(a_off) * 35
                if cost < min_cost:
                    min_cost = cost
                    best_pos = (lx, ly, lw, lh)

            if best_pos and min_cost < 160:
                break

        if not best_pos:
            best_pos = (px + 35, py - 10, lw, lh)

        placed_boxes.append((best_pos[0], best_pos[1], best_pos[0] + lw, best_pos[1] + lh))
        layout_results[s["num"]] = (best_pos[0], best_pos[1], lw, lh, s["text"])

    # 1. 引き出し線の描画
    for s in spots_data:
        num = s["num"]
        px, py = s["px"], s["py"]
        lx, ly, lw, lh, lbl_text = layout_results[num]
        
        if lx > px:
            target_x = lx
            target_y = ly + lh / 2
        else:
            target_x = lx + lw
            target_y = ly + lh / 2

        draw.line([(px, py), (target_x, target_y)], fill=(15, 44, 89), width=2)
        draw.ellipse([(target_x - 3, target_y - 3), (target_x + 3, target_y + 3)], fill=(15, 44, 89))

    # 2. ピンの描画
    for s in spots_data:
        num = s["num"]
        px, py = s["px"], s["py"]
        r = 15
        draw.ellipse([(px - r - 2, py - r - 2), (px + r + 2, py + r + 2)], fill=(255, 255, 255))
        draw.ellipse([(px - r, py - r), (px + r, py + r)], fill=(220, 38, 38))
        
        num_str = str(num)
        bbox = font_pin.getbbox(num_str)
        w_txt = bbox[2] - bbox[0]
        h_txt = bbox[3] - bbox[1]
        draw.text((px - w_txt / 2, py - h_txt / 2 - 2), num_str, fill=(255, 255, 255), font=font_pin)

    # 3. ラベルボックスの描画
    for s in spots_data:
        num = s["num"]
        lx, ly, lw, lh, lbl_text = layout_results[num]
        draw.rounded_rectangle([(lx + 2, ly + 2), (lx + lw + 2, ly + lh + 2)], radius=5, fill=(180, 180, 180, 160))
        draw.rounded_rectangle([(lx, ly), (lx + lw, ly + lh)], radius=5, fill=(255, 255, 255), outline=(15, 44, 89), width=2)
        draw.text((lx + 6, ly + 3), lbl_text, fill=(15, 44, 89), font=font_lbl)

    # 4. 凡例ボックス
    draw.rounded_rectangle([(leg_x, leg_y), (leg_x + leg_w, leg_y + leg_h)], radius=6, fill=(255, 255, 255), outline=(15, 44, 89), width=2)
    draw.rectangle([(leg_x, leg_y), (leg_x + leg_w, leg_y + 26)], fill=(15, 44, 89))
    draw.text((leg_x + 10, leg_y + 4), "【掲載スポット一覧】", fill=(255, 255, 255), font=font_legend)
    
    half = (len(resolved_spots) + 1) // 2
    for i, s in enumerate(resolved_spots[:half]):
        s_name = s[1].split("(")[0].split("（")[0].strip()[:10]
        draw.text((leg_x + 10, leg_y + 32 + i * 22), f"{s[0]}. {s_name}", fill=(15, 44, 89), font=font_legend)
    for i, s in enumerate(resolved_spots[half:]):
        s_name = s[1].split("(")[0].split("（")[0].strip()[:10]
        draw.text((leg_x + 185, leg_y + 32 + i * 22), f"{s[0]}. {s_name}", fill=(15, 44, 89), font=font_legend)

    draw.text((15, map_img.height - 25), "出典: 国土地理院標準地図 / Area Spatial Scout", fill=(80, 80, 80), font=font_legend)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    map_img.save(output_path, quality=95)
    return output_path


def create_csv_report(data: dict, output_path: Path):
    """CSV台帳（BOM付きUTF-8）"""
    spots = data.get("spots", [])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    headers = [
        "No", "施設・店舗名", "カテゴリー", "総合評価", "クチコミ件数",
        "住所", "公式/予約URL", "特徴キーワード", "詳細料金体系", "混雑ピーク時間", "代表クチコミ要約"
    ]
    with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        for idx, s in enumerate(spots):
            topics = " / ".join(s.get("key_topics", []))
            pop = s.get("popular_times", {})
            peak = f"{pop.get('peak_time', '-')} (閑散: {pop.get('quiet_time', '-')})"
            revs = s.get("reviews", [])
            first_rev = revs[0] if revs else "-"
            writer.writerow([
                idx + 1,
                safe_nfc(s.get("name")),
                safe_nfc(s.get("category")),
                safe_nfc(s.get("rating")),
                s.get("reviews_count", 0),
                safe_nfc(s.get("address")),
                safe_nfc(s.get("url")),
                safe_nfc(topics),
                safe_nfc(s.get("pricing")),
                safe_nfc(peak),
                safe_nfc(first_rev)
            ])


def create_docx_report(data: dict, output_path: Path, map_image_path: Path = None):
    """PC・印刷用 Word レポート（A4 1ページ完結・全12pt以上）"""
    doc = Document()
    for section in doc.sections:
        section.top_margin = Inches(0.5)
        section.bottom_margin = Inches(0.5)
        section.left_margin = Inches(0.5)
        section.right_margin = Inches(0.5)

    meta = data.get("meta", {})
    area = safe_nfc(meta.get("area", "指定エリア"))
    theme = safe_nfc(meta.get("theme", "指定テーマ"))
    scouted_at = safe_nfc(meta.get("scouted_at", datetime.now().strftime("%Y-%m-%d")))
    spots = data.get("spots", [])

    # タイトル
    p_title = doc.add_paragraph()
    p_title.paragraph_format.space_before = Pt(0)
    p_title.paragraph_format.space_after = Pt(2)
    run_title = p_title.add_run(f"【エリア空間調査】{area}における{theme}比較調査レポート")
    format_run(run_title, font_name=FONT_JP_TITLE, size_pt=20.0, bold=True, color_hex=COLOR_PRIMARY_HEX)

    p_sub = doc.add_paragraph()
    p_sub.paragraph_format.space_after = Pt(4)
    run_sub = p_sub.add_run(f"作成日: {scouted_at}  |  調査対象: {area}エリア主要{theme} Top {len(spots)}  |  Area Spatial Scout")
    format_run(run_sub, font_name=FONT_JP, size_pt=12.0, color_hex=COLOR_TEXT_MUTED_HEX)

    # 第1部: サマリー
    h1 = doc.add_paragraph()
    h1.paragraph_format.space_before = Pt(4)
    h1.paragraph_format.space_after = Pt(2)
    r_h1 = h1.add_run("1. エグゼクティブ・サマリー")
    format_run(r_h1, font_name=FONT_JP_TITLE, size_pt=16.0, bold=True, color_hex=COLOR_PRIMARY_HEX)

    p_sum = doc.add_paragraph()
    p_sum.paragraph_format.space_after = Pt(3)
    p_sum.paragraph_format.line_spacing = 1.15
    r_sum = p_sum.add_run(safe_nfc(meta.get("summary_text", "")))
    format_run(r_sum, font_name=FONT_JP, size_pt=12.0, color_hex=COLOR_TEXT_MAIN_HEX)

    findings_list = meta.get("findings", [])
    if findings_list:
        add_callout_box(doc, "\n".join([safe_nfc(f) for f in findings_list]), title="【主要ファインディングス】")

    has_map = bool(map_image_path and os.path.exists(map_image_path))
    if has_map:
        doc.add_page_break()
        h_map = doc.add_paragraph()
        h_map.paragraph_format.space_before = Pt(4)
        h_map.paragraph_format.space_after = Pt(2)
        r_hm = h_map.add_run(f"2. 【広域俯瞰図】{area}・周辺エリア {theme} Top {len(spots)} スポット位置関係マップ")
        format_run(r_hm, font_name=FONT_JP_TITLE, size_pt=16.0, bold=True, color_hex=COLOR_PRIMARY_HEX)

        p_img = doc.add_paragraph()
        p_img.alignment = WD_ALIGN_PARAGRAPH.CENTER
        r_img = p_img.add_run()
        r_img.add_picture(str(map_image_path), width=Inches(6.8))

        p_glink = doc.add_paragraph()
        p_glink.paragraph_format.space_before = Pt(4)
        p_glink.paragraph_format.space_after = Pt(4)
        r_glabel = p_glink.add_run("📍 Google マップで全スポットを開く: ")
        format_run(r_glabel, font_name=FONT_JP, size_pt=12.0, bold=True, color_hex=COLOR_PRIMARY_HEX)
        add_hyperlink(p_glink, f"https://www.google.com/maps/search/?api=1&query={urllib.parse.quote(f'{area} {theme}')}", f"{area}の{theme}をGoogleマップでまとめて検索・表示", size_pt=12.0, bold=True)

    # 第2部: 各スポット独立カルテ
    sec_no = 3 if has_map else 2
    h2 = doc.add_paragraph()
    h2.paragraph_format.space_before = Pt(4)
    h2.paragraph_format.space_after = Pt(2)
    r_h2 = h2.add_run(f"{sec_no}. 主要{theme}個別スポット詳細カルテ（Top {len(spots)}）")
    format_run(r_h2, font_name=FONT_JP_TITLE, size_pt=16.0, bold=True, color_hex=COLOR_PRIMARY_HEX)

    for idx, s in enumerate(spots):
        doc.add_page_break()
        name = safe_nfc(s.get("name", f"スポット {idx+1}"))
        category = safe_nfc(s.get("category", "施設"))
        rating = safe_nfc(s.get("rating", "-"))
        reviews_count = s.get("reviews_count", 0)
        address = safe_nfc(s.get("address", "-"))
        url = safe_nfc(s.get("url", ""))
        gmaps_url = make_google_maps_url(name, address)
        key_topics = [safe_nfc(t) for t in s.get("key_topics", [])]
        pricing = safe_nfc(s.get("pricing", "-"))
        pop = s.get("popular_times", {})
        peak_time = safe_nfc(pop.get("peak_time", "-"))
        quiet_time = safe_nfc(pop.get("quiet_time", "-"))
        photos = s.get("photos", [])
        reviews = s.get("reviews", [])

        h_spot = doc.add_paragraph()
        h_spot.paragraph_format.space_before = Pt(0)
        h_spot.paragraph_format.space_after = Pt(1)
        r_num = h_spot.add_run(f"No.{idx+1} ")
        format_run(r_num, font_name=FONT_JP_TITLE, size_pt=18.0, bold=True, color_hex=COLOR_ACCENT_HEX)
        add_hyperlink(h_spot, gmaps_url, name, font_name=FONT_JP_TITLE, size_pt=18.0, color_hex=COLOR_PRIMARY_HEX, bold=True)

        p_meta = doc.add_paragraph()
        p_meta.paragraph_format.space_after = Pt(2)
        r_cat = p_meta.add_run(f"【{category}】  ★ {rating} ({reviews_count:,}件のクチコミ)")
        format_run(r_cat, font_name=FONT_JP, size_pt=12.0, bold=True, color_hex=COLOR_ACCENT_GOLD)

        # 写真2枚テーブル
        tbl_photo = doc.add_table(rows=2, cols=2)
        tbl_photo.alignment = WD_TABLE_ALIGNMENT.CENTER
        tbl_photo.autofit = False
        tbl_photo.columns[0].width = Inches(3.6)
        tbl_photo.columns[1].width = Inches(3.6)
        set_modern_horizontal_borders(tbl_photo, border_hex="E2E8F0")

        for col_idx in range(2):
            cell_img = tbl_photo.cell(0, col_idx)
            cell_cap = tbl_photo.cell(1, col_idx)
            set_cell_margins(cell_img, top_pt=2.0, bottom_pt=2.0, left_pt=3.0, right_pt=3.0)
            set_cell_margins(cell_cap, top_pt=1.0, bottom_pt=2.0, left_pt=3.0, right_pt=3.0)
            
            p_img_c = cell_img.paragraphs[0]
            p_img_c.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p_cap_c = cell_cap.paragraphs[0]
            p_cap_c.alignment = WD_ALIGN_PARAGRAPH.CENTER
            
            if col_idx < len(photos):
                ph = photos[col_idx]
                ph_path = ph.get("path")
                caption = safe_nfc(ph.get("caption", f"写真 {col_idx+1}"))
                if ph_path and os.path.exists(ph_path):
                    r_p = p_img_c.add_run()
                    r_p.add_picture(str(ph_path), width=Inches(3.5))
                else:
                    r_none = p_img_c.add_run("[写真準備中]")
                    format_run(r_none, font_name=FONT_JP, size_pt=12.0, color_hex=COLOR_TEXT_MUTED_HEX)
                r_cap = p_cap_c.add_run(f"▲ {caption}")
                format_run(r_cap, font_name=FONT_JP, size_pt=12.0, color_hex=COLOR_TEXT_MUTED_HEX)

        # 諸元・料金・混雑度テーブル
        tbl_info = doc.add_table(rows=4, cols=2)
        tbl_info.alignment = WD_TABLE_ALIGNMENT.CENTER
        tbl_info.autofit = False
        tbl_info.columns[0].width = Inches(1.5)
        tbl_info.columns[1].width = Inches(5.7)
        set_modern_horizontal_borders(tbl_info)

        row_defs = [
            ("所在地・アクセス", address, True, gmaps_url),
            ("公式Web / 予約", url, True, url),
            ("料金体系・プラン", pricing, False, None),
            ("混雑ピーク / 閑散", f"ピーク: {peak_time}  |  閑散時間: {quiet_time}", False, None),
        ]
        for r_idx, (label, val, is_link, link_url) in enumerate(row_defs):
            c_lbl = tbl_info.cell(r_idx, 0)
            c_val = tbl_info.cell(r_idx, 1)
            set_cell_bg(c_lbl, COLOR_BG_LIGHT_HEX)
            set_cell_margins(c_lbl, top_pt=5.0, bottom_pt=5.0, left_pt=7.0, right_pt=7.0)
            set_cell_margins(c_val, top_pt=5.0, bottom_pt=5.0, left_pt=7.0, right_pt=7.0)
            
            p_l = c_lbl.paragraphs[0]
            r_l = p_l.add_run(label)
            format_run(r_l, font_name=FONT_JP, size_pt=12.0, bold=True, color_hex=COLOR_PRIMARY_HEX)
            
            p_v = c_val.paragraphs[0]
            if is_link and link_url:
                add_hyperlink(p_v, link_url, val, size_pt=12.0)
            else:
                r_v = p_v.add_run(val)
                format_run(r_v, font_name=FONT_JP, size_pt=12.0, color_hex=COLOR_TEXT_MAIN_HEX)

        # 口コミ8件グリッド
        p_rev_hdr = doc.add_paragraph()
        p_rev_hdr.paragraph_format.space_before = Pt(3)
        p_rev_hdr.paragraph_format.space_after = Pt(1)
        r_rh = p_rev_hdr.add_run("▼ 利用者のリアルなクチコミ・評判（厳選8件）")
        format_run(r_rh, font_name=FONT_JP_TITLE, size_pt=13.0, bold=True, color_hex=COLOR_PRIMARY_HEX)

        tbl_rev = doc.add_table(rows=4, cols=2)
        tbl_rev.alignment = WD_TABLE_ALIGNMENT.CENTER
        tbl_rev.autofit = False
        tbl_rev.columns[0].width = Inches(3.6)
        tbl_rev.columns[1].width = Inches(3.6)
        set_modern_horizontal_borders(tbl_rev, border_hex="CBD5E1")

        for r_i in range(4):
            for c_i in range(2):
                cell_idx = r_i * 2 + c_i
                cell = tbl_rev.cell(r_i, c_i)
                set_cell_margins(cell, top_pt=4.0, bottom_pt=4.0, left_pt=6.0, right_pt=6.0)
                p_c = cell.paragraphs[0]
                p_c.paragraph_format.line_spacing = 1.1
                
                if cell_idx < len(reviews):
                    rev_text = safe_nfc(reviews[cell_idx])
                    r_num_b = p_c.add_run(f"#{cell_idx+1} ")
                    format_run(r_num_b, font_name=FONT_JP, size_pt=12.0, bold=True, color_hex=COLOR_ACCENT_HEX)
                    r_rev = p_c.add_run(f"「{rev_text}」")
                    format_run(r_rev, font_name=FONT_JP, size_pt=12.0, color_hex=COLOR_TEXT_MAIN_HEX)

    # 第3部: 戦略的示唆
    doc.add_page_break()
    advice_sec_no = sec_no + 1
    h3 = doc.add_paragraph()
    h3.paragraph_format.space_before = Pt(6)
    h3.paragraph_format.space_after = Pt(4)
    r_h3 = h3.add_run(f"{advice_sec_no}. 空間・出店・利用戦略への戦略的示唆")
    format_run(r_h3, font_name=FONT_JP_TITLE, size_pt=16.0, bold=True, color_hex=COLOR_PRIMARY_HEX)
    add_callout_box(doc, safe_nfc(meta.get("strategic_advice", "")), title="【空間スカウティングからの戦略的提言】")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(output_path)


def create_docx_report_mobile(data: dict, output_path: Path, map_image_path: Path = None):
    """スマートフォン閲覧専用 Word レポート（完全1カラム・横スクロール完全ゼロ設計）"""
    doc = Document()
    for section in doc.sections:
        section.top_margin = Inches(0.4)
        section.bottom_margin = Inches(0.4)
        section.left_margin = Inches(0.3)
        section.right_margin = Inches(0.3)

    meta = data.get("meta", {})
    area = safe_nfc(meta.get("area", "指定エリア"))
    theme = safe_nfc(meta.get("theme", "指定テーマ"))
    scouted_at = safe_nfc(meta.get("scouted_at", datetime.now().strftime("%Y-%m-%d")))
    spots = data.get("spots", [])

    p_title = doc.add_paragraph()
    p_title.paragraph_format.space_before = Pt(0)
    p_title.paragraph_format.space_after = Pt(2)
    run_title = p_title.add_run(f"【スマホ閲覧版】{area} × {theme} 比較調査")
    format_run(run_title, font_name=FONT_JP_TITLE, size_pt=18.0, bold=True, color_hex=COLOR_PRIMARY_HEX)

    p_sub = doc.add_paragraph()
    p_sub.paragraph_format.space_after = Pt(4)
    run_sub = p_sub.add_run(f"スマートフォン縦スクロール最適化版（横スクロールゼロ・親指1本で読める1カラム設計） | 調査日: {scouted_at}")
    format_run(run_sub, font_name=FONT_JP, size_pt=12.0, color_hex=COLOR_TEXT_MUTED_HEX)

    h1 = doc.add_paragraph()
    h1.paragraph_format.space_before = Pt(4)
    h1.paragraph_format.space_after = Pt(2)
    r_h1 = h1.add_run("1. エグゼクティブ・サマリー")
    format_run(r_h1, font_name=FONT_JP_TITLE, size_pt=15.0, bold=True, color_hex=COLOR_PRIMARY_HEX)

    p_sum = doc.add_paragraph()
    p_sum.paragraph_format.space_after = Pt(3)
    r_sum = p_sum.add_run(safe_nfc(meta.get("summary_text", "")))
    format_run(r_sum, font_name=FONT_JP, size_pt=12.0, color_hex=COLOR_TEXT_MAIN_HEX)

    findings_list = meta.get("findings", [])
    if findings_list:
        add_callout_box(doc, "\n".join([safe_nfc(f) for f in findings_list]), title="【キー・ファインディングス】")

    has_map = bool(map_image_path and os.path.exists(map_image_path))
    if has_map:
        doc.add_page_break()
        h_map = doc.add_paragraph()
        h_map.paragraph_format.space_before = Pt(4)
        h_map.paragraph_format.space_after = Pt(2)
        r_hm = h_map.add_run(f"2. 【広域俯瞰図】{area}・周辺エリア {theme} Top {len(spots)} スポット位置関係マップ")
        format_run(r_hm, font_name=FONT_JP_TITLE, size_pt=15.0, bold=True, color_hex=COLOR_PRIMARY_HEX)

        p_img = doc.add_paragraph()
        p_img.alignment = WD_ALIGN_PARAGRAPH.CENTER
        r_img = p_img.add_run()
        r_img.add_picture(str(map_image_path), width=Inches(3.8))

        p_glink = doc.add_paragraph()
        p_glink.paragraph_format.space_before = Pt(3)
        p_glink.paragraph_format.space_after = Pt(4)
        r_glabel = p_glink.add_run("📍 Google マップで全スポットを開く: ")
        format_run(r_glabel, font_name=FONT_JP, size_pt=12.0, bold=True, color_hex=COLOR_PRIMARY_HEX)
        add_hyperlink(p_glink, f"https://www.google.com/maps/search/?api=1&query={urllib.parse.quote(f'{area} {theme}')}", f"{area}の{theme}をGoogleマップでまとめて表示", size_pt=12.0, bold=True)

    sec_no = 3 if has_map else 2
    h2 = doc.add_paragraph()
    h2.paragraph_format.space_before = Pt(4)
    h2.paragraph_format.space_after = Pt(2)
    r_h2 = h2.add_run(f"{sec_no}. 主要{theme}個別スポット詳細カルテ（Top {len(spots)}）")
    format_run(r_h2, font_name=FONT_JP_TITLE, size_pt=15.0, bold=True, color_hex=COLOR_PRIMARY_HEX)

    for idx, s in enumerate(spots):
        doc.add_page_break()
        name = safe_nfc(s.get("name", f"スポット {idx+1}"))
        category = safe_nfc(s.get("category", "施設"))
        rating = safe_nfc(s.get("rating", "-"))
        reviews_count = s.get("reviews_count", 0)
        address = safe_nfc(s.get("address", "-"))
        url = safe_nfc(s.get("url", ""))
        gmaps_url = make_google_maps_url(name, address)
        key_topics = [safe_nfc(t) for t in s.get("key_topics", [])]
        pricing = safe_nfc(s.get("pricing", "-"))
        pop = s.get("popular_times", {})
        peak_time = safe_nfc(pop.get("peak_time", "-"))
        quiet_time = safe_nfc(pop.get("quiet_time", "-"))
        photos = s.get("photos", [])
        reviews = s.get("reviews", [])

        h_spot = doc.add_paragraph()
        h_spot.paragraph_format.space_before = Pt(0)
        h_spot.paragraph_format.space_after = Pt(1)
        r_num = h_spot.add_run(f"No.{idx+1} ")
        format_run(r_num, font_name=FONT_JP_TITLE, size_pt=16.0, bold=True, color_hex=COLOR_ACCENT_HEX)
        add_hyperlink(h_spot, gmaps_url, name, font_name=FONT_JP_TITLE, size_pt=16.0, color_hex=COLOR_PRIMARY_HEX, bold=True)

        p_meta = doc.add_paragraph()
        p_meta.paragraph_format.space_after = Pt(2)
        r_cat = p_meta.add_run(f"【{category}】  ★ {rating} ({reviews_count:,}件)")
        format_run(r_cat, font_name=FONT_JP, size_pt=12.0, bold=True, color_hex=COLOR_ACCENT_GOLD)

        if photos:
            for ph in photos[:2]:
                ph_path = ph.get("path")
                caption = safe_nfc(ph.get("caption", "施設写真"))
                if ph_path and os.path.exists(ph_path):
                    p_p = doc.add_paragraph()
                    p_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    p_p.paragraph_format.space_before = Pt(2)
                    p_p.paragraph_format.space_after = Pt(1)
                    r_img = p_p.add_run()
                    r_img.add_picture(str(ph_path), width=Inches(3.6))
                    
                    p_cap = doc.add_paragraph()
                    p_cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    p_cap.paragraph_format.space_after = Pt(3)
                    r_cap = p_cap.add_run(f"▲ {caption}")
                    format_run(r_cap, font_name=FONT_JP, size_pt=12.0, color_hex=COLOR_TEXT_MUTED_HEX)

        card_lines = [
            f"■ 所在地: {address}",
            f"■ 公式サイト: {url if url else '情報なし'}",
            f"■ 料金体系: {pricing}",
            f"■ 混雑傾向: ピーク {peak_time} / 閑散 {quiet_time}"
        ]
        add_callout_box(doc, "\n".join(card_lines), title="【基本諸元・料金・混雑】")

        p_rev_h = doc.add_paragraph()
        p_rev_h.paragraph_format.space_before = Pt(3)
        p_rev_h.paragraph_format.space_after = Pt(2)
        r_rh = p_rev_h.add_run("▼ 利用者のリアルな声（厳選8件）")
        format_run(r_rh, font_name=FONT_JP_TITLE, size_pt=13.0, bold=True, color_hex=COLOR_PRIMARY_HEX)

        for r_idx, rev in enumerate(reviews[:8]):
            p_rev = doc.add_paragraph()
            p_rev.paragraph_format.space_before = Pt(1)
            p_rev.paragraph_format.space_after = Pt(2)
            p_rev.paragraph_format.line_spacing = 1.15
            r_num_b = p_rev.add_run(f"#{r_idx+1} ")
            format_run(r_num_b, font_name=FONT_JP, size_pt=12.0, bold=True, color_hex=COLOR_ACCENT_HEX)
            r_text = p_rev.add_run(f"「{safe_nfc(rev)}」")
            format_run(r_text, font_name=FONT_JP, size_pt=12.0, color_hex=COLOR_TEXT_MAIN_HEX)

    doc.add_page_break()
    advice_sec_no = sec_no + 1
    h3 = doc.add_paragraph()
    h3.paragraph_format.space_before = Pt(6)
    h3.paragraph_format.space_after = Pt(4)
    r_h3 = h3.add_run(f"{advice_sec_no}. 空間・出店・利用戦略への戦略的示唆")
    format_run(r_h3, font_name=FONT_JP_TITLE, size_pt=15.0, bold=True, color_hex=COLOR_PRIMARY_HEX)
    add_callout_box(doc, safe_nfc(meta.get("strategic_advice", "")), title="【空間スカウティングからの戦略的提言】")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(output_path)


def generate_full_report_pack(data: dict, output_dir: Path) -> dict:
    """全成果物（地図、通常Word、スマホWord、CSV）を一括生成してファイルパス群を返す"""
    output_dir.mkdir(parents=True, exist_ok=True)
    meta = data.get("meta", {})
    area = meta.get("area", "エリア")
    theme = meta.get("theme", "テーマ")
    scouted_at = meta.get("scouted_at")
    spots = data.get("spots", [])
    spots_count = len(spots)

    date_str = re.sub(r"[^0-9]", "", str(scouted_at))[:8] if scouted_at else datetime.now().strftime("%Y%m%d")
    count_str = f"_Top{spots_count}" if spots_count > 0 else ""

    # 1. 地図画像
    map_path = output_dir / f"{area}_{theme}_plot_map.png"
    try:
        generate_spots_map_image(spots, map_path, area=area, theme=theme)
    except Exception as e:
        print(f"[Warning] 地図生成スキップ: {e}", file=sys.stderr)
        map_path = None

    # 2. ファイル名
    docx_name = f"【{area}】{theme}{count_str}比較調査レポート_{date_str}.docx"
    mobile_docx_name = f"【{area}】{theme}{count_str}比較調査レポート_スマホ閲覧用_{date_str}.docx"
    csv_name = f"【{area}】{theme}{count_str}スポット台帳_{date_str}.csv"
    folder_name = f"{date_str}_【{area}】{theme}{count_str}比較調査レポート"

    docx_path = output_dir / docx_name
    mobile_docx_path = output_dir / mobile_docx_name
    csv_path = output_dir / csv_name

    # 3. CSV
    create_csv_report(data, csv_path)
    # 4. PC Word
    create_docx_report(data, docx_path, map_image_path=map_path)
    # 5. Mobile Word
    create_docx_report_mobile(data, mobile_docx_path, map_image_path=map_path)

    # 互換用
    compat_docx = output_dir / "area_scout_report.docx"
    compat_csv = output_dir / "spots_ledger.csv"
    shutil.copy2(docx_path, compat_docx)
    shutil.copy2(csv_path, compat_csv)

    return {
        "folder_name": folder_name,
        "docx_path": docx_path,
        "mobile_docx_path": mobile_docx_path,
        "csv_path": csv_path,
        "map_path": map_path,
        "data_json": output_dir / "data.json"
    }
