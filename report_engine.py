#!/usr/bin/env python3
"""
report_engine.py
Linuxコンテナ（Cloud Run）およびmacOSの両方に対応した、
エグゼクティブ向けエリア・空間調査報告書 (PC版Word / スマホ専用Word / 重なりゼロ地図 / CSV台帳)
の完全自動生成エンジン。
"""

from __future__ import annotations

import os
import re
import io
import html
import math
import sys
import csv
import unicodedata
import urllib.parse
import urllib.request
import concurrent.futures
from datetime import datetime
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

from geocoding import geocode_address

import docx
from docx import Document
from docx.shared import Inches, Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls

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


def as_int(val, default: int = 0) -> int:
    """クチコミ件数などを安全に整数化する。

    LLM は 350 だけでなく "350件" / "約350" / None も返すため、書式指定 (f"{n:,}") の
    直前に必ずここを通す。通さないと ValueError: Cannot specify ',' with 's' で
    レポート生成が丸ごと落ちる。
    """
    if isinstance(val, bool):
        return default
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return int(val)
    digits = re.sub(r"[^0-9]", "", str(val or ""))
    return int(digits) if digits else default


_UNSAFE_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_filename_component(val, default: str = "未指定", max_len: int = 60) -> str:
    """ファイル名に埋め込める安全な断片へ正規化する。

    area / theme は利用者入力と LLM 応答の両方から来るため、そのまま f-string で
    ファイル名に入れると `../..` で出力ディレクトリの外にファイルを作れてしまう
    (読み出し側だけ固めても、書き込み側が素通りでは意味がない)。
    """
    text = safe_nfc(val).strip()
    text = _UNSAFE_FILENAME_RE.sub("_", text)
    text = text.replace(os.sep, "_")
    text = re.sub(r"\.{2,}", "_", text).strip(". 　")
    return text[:max_len] or default


def review_source_note(spot: dict, idx: int) -> str:
    """クチコミに添える出典表記。出典が無ければ空文字。

    写真キャプションと同じ「（出典: host）」の体裁に揃える。research_engine 側で
    reviews と review_sources は同じ長さに正規化されている。
    """
    sources = spot.get("review_sources")
    if not isinstance(sources, list) or idx >= len(sources):
        return ""
    url = str(sources[idx] or "").strip()
    if not url:
        return ""
    host = urllib.parse.urlparse(url).netloc
    return f"（出典: {host}）" if host else ""


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


def deg2num(lat_deg, lon_deg, zoom):
    lat_rad = math.radians(lat_deg)
    n = 2.0 ** zoom
    xtile = (lon_deg + 180.0) / 360.0 * n
    ytile = (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n
    return (xtile, ytile)


def generate_spots_map_image(spots: list[dict], output_path: Path, area: str = "エリア", theme: str = "テーマ") -> Path | None:
    """住所を実際にジオコーディングできたスポットだけを国土地理院タイル上にプロットする。

    重要: 解決できなかったスポットに「それらしい座標」を与えて地図に載せてはいけない。
    実在の店名が、実在しない位置に、公的地図の出典表記付きで描かれることになる。
    解決できたものが 1 件も無ければ地図自体を作らない (None を返す)。
    """
    resolved_spots = []

    for idx, s in enumerate(spots):
        lat = s.get("lat")
        lon = s.get("lon")

        if lat is None or lon is None:
            addr = s.get("address", "")
            geo = geocode_address(addr) if addr else None
            if not geo:
                s["geocoded"] = False
                print(f"[Map] 住所を解決できないため地図から除外: {s.get('name', '?')} ({addr or '住所なし'})")
                continue
            lat, lon = geo
            # 同一住所に複数店が入る場合のピン重なりだけを避ける微小オフセット (約30m)
            angle = (idx * 137.5) * (math.pi / 180.0)
            lat += 0.0003 * math.sin(angle)
            lon += 0.0004 * math.cos(angle)
            s["lat"], s["lon"] = lat, lon

        s["geocoded"] = True
        name = safe_nfc(s.get("name", f"スポット {idx + 1}"))
        resolved_spots.append((idx + 1, name, float(lat), float(lon)))

    if not resolved_spots:
        print("[Map] 座標を確認できたスポットが無いため地図を生成しません")
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

    # 国土地理院標準地図タイルの並列高速ダウンロード（各タイルタイムアウト 2.0秒）
    headers = {"User-Agent": "AntigravityMapScout/2.0"}
    def fetch_gsi_tile(tx, ty):
        url = f"https://cyberjapandata.gsi.go.jp/xyz/std/{zoom}/{tx}/{ty}.png"
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=2.0) as res:
                t_img = Image.open(io.BytesIO(res.read())).convert("RGB")
                return (tx, ty, t_img)
        except Exception:
            return (tx, ty, None)

    tile_tasks = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        for tx in range(tile_x_start, tile_x_end + 1):
            for ty in range(tile_y_start, tile_y_end + 1):
                tile_tasks.append(executor.submit(fetch_gsi_tile, tx, ty))
        
        for future in concurrent.futures.as_completed(tile_tasks):
            tx, ty, t_img = future.result()
            if t_img:
                px = (tx - tile_x_start) * 256
                py = (ty - tile_y_start) * 256
                map_img.paste(t_img, (px, py))

    # 3. 高精細プロット都市グリッドマップ
    # 同心円距離スケール（中心から1km, 2kmのガイドライン）
    c_px = map_img.width // 2
    c_py = map_img.height // 2
    for r in [80, 160, 240, 320]:
        if r < min(c_px, c_py):
            grid_draw.ellipse([c_px - r, c_py - r, c_px + r, c_py + r], outline=(218, 226, 238), width=1)

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


def add_sources_section(doc, meta: dict, heading_pt: float = 14.0):
    """Google 検索グラウンディングで実際に参照した出典を巻末に列挙する"""
    sources = meta.get("sources") or []
    queries = meta.get("search_queries") or []
    if not sources and not queries:
        return

    doc.add_page_break()
    h = doc.add_paragraph()
    h.paragraph_format.space_before = Pt(6)
    h.paragraph_format.space_after = Pt(3)
    r_h = h.add_run("調査の出典")
    format_run(r_h, font_name=FONT_JP_TITLE, size_pt=heading_pt, bold=True, color_hex=COLOR_PRIMARY_HEX)

    if queries:
        p_q = doc.add_paragraph()
        p_q.paragraph_format.space_after = Pt(3)
        r_q = p_q.add_run("検索クエリ: " + " / ".join(safe_nfc(q) for q in queries[:8]))
        format_run(r_q, font_name=FONT_JP, size_pt=12.0, color_hex=COLOR_TEXT_MUTED_HEX)

    for i, src in enumerate(sources[:30], start=1):
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(1)
        p.paragraph_format.space_after = Pt(1)
        r_n = p.add_run(f"[{i}] ")
        format_run(r_n, font_name=FONT_JP, size_pt=12.0, bold=True, color_hex=COLOR_ACCENT_HEX)
        title = safe_nfc(src.get("title") or src.get("uri", ""))
        add_hyperlink(p, src.get("uri", ""), title, size_pt=12.0)


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
                as_int(s.get("reviews_count")),
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

    if meta.get("grounding_status") == "ungrounded":
        add_callout_box(
            doc,
            "この調査では Google 検索が実行されませんでした（モデル側の上限・タイムアウトのため）。"
            "\n実在の裏取りができないため、利用者のクチコミは掲載していません。"
            "\n記載内容は公式サイト等の一次情報でご確認ください。",
            title="【重要: 検索による裏取りができていません】",
            border_color_hex=COLOR_ACCENT_GOLD,
        )

    area_notes = []
    if meta.get("area_excluded"):
        names = "、".join(f"{e['name']}({e.get('distance_km')}km)" for e in meta["area_excluded"][:5])
        area_notes.append(f"指定エリアから離れていたため除外: {names}")
    if meta.get("area_unverified_count"):
        area_notes.append(f"住所を確認できずエリア判定ができなかったスポット: {meta['area_unverified_count']}件")
    if meta.get("address_backfilled_count"):
        area_notes.append(
            f"住所を追加の検索で補完したスポット: {meta['address_backfilled_count']}件"
            "（番地の精度は一次情報でご確認ください）"
        )
    if area_notes:
        add_callout_box(doc, "\n".join(area_notes), title="【エリア一致の検証について】")

    findings_list = meta.get("findings", [])
    if findings_list:
        add_callout_box(doc, "\n".join([safe_nfc(f) for f in findings_list]), title="【主要ファインディングス】")

    has_map = bool(map_image_path and os.path.exists(map_image_path))
    if not has_map:
        # 地図を出せない理由を黙って伏せない (章が消えるだけだと理由が伝わらない)
        unmapped = [s for s in spots if not s.get("geocoded")]
        if unmapped:
            add_callout_box(
                doc,
                f"掲載 {len(spots)} 件のうち {len(unmapped)} 件は住所を確認できなかったため、"
                "位置関係マップは作成していません（推定位置で地図に描くことはしません）。",
                title="【広域俯瞰図について】",
            )
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
        reviews_count = as_int(s.get("reviews_count"))
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

        # 写真テーブル（実在画像を取得できたスポットのみ。無い場合は枠ごと出さない）
        usable_photos = [p for p in photos if p.get("path") and os.path.exists(p.get("path"))]
        if usable_photos:
            tbl_photo = doc.add_table(rows=2, cols=len(usable_photos))
            tbl_photo.alignment = WD_TABLE_ALIGNMENT.CENTER
            tbl_photo.autofit = False
            for col in tbl_photo.columns:
                col.width = Inches(3.6)
            set_modern_horizontal_borders(tbl_photo, border_hex="E2E8F0")

            for col_idx, ph in enumerate(usable_photos):
                cell_img = tbl_photo.cell(0, col_idx)
                cell_cap = tbl_photo.cell(1, col_idx)
                set_cell_margins(cell_img, top_pt=2.0, bottom_pt=2.0, left_pt=3.0, right_pt=3.0)
                set_cell_margins(cell_cap, top_pt=1.0, bottom_pt=2.0, left_pt=3.0, right_pt=3.0)

                p_img_c = cell_img.paragraphs[0]
                p_img_c.alignment = WD_ALIGN_PARAGRAPH.CENTER
                p_cap_c = cell_cap.paragraphs[0]
                p_cap_c.alignment = WD_ALIGN_PARAGRAPH.CENTER

                r_p = p_img_c.add_run()
                r_p.add_picture(str(ph["path"]), width=Inches(3.5))
                r_cap = p_cap_c.add_run(f"▲ {safe_nfc(ph.get('caption', ''))}")
                format_run(r_cap, font_name=FONT_JP, size_pt=12.0, color_hex=COLOR_TEXT_MUTED_HEX)

        # 特徴キーワード
        if key_topics:
            p_topics = doc.add_paragraph()
            p_topics.paragraph_format.space_before = Pt(2)
            p_topics.paragraph_format.space_after = Pt(2)
            r_tl = p_topics.add_run("特徴: ")
            format_run(r_tl, font_name=FONT_JP, size_pt=12.0, bold=True, color_hex=COLOR_PRIMARY_HEX)
            r_tv = p_topics.add_run(" / ".join(key_topics))
            format_run(r_tv, font_name=FONT_JP, size_pt=12.0, color_hex=COLOR_TEXT_MAIN_HEX)

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

        # クチコミ（調査で確認できた件数のみ。見出しの件数も実数に合わせる）
        if reviews:
            p_rev_hdr = doc.add_paragraph()
            p_rev_hdr.paragraph_format.space_before = Pt(3)
            p_rev_hdr.paragraph_format.space_after = Pt(1)
            r_rh = p_rev_hdr.add_run(f"▼ 調査で確認できた利用者のクチコミ（{len(reviews)}件）")
            format_run(r_rh, font_name=FONT_JP_TITLE, size_pt=13.0, bold=True, color_hex=COLOR_PRIMARY_HEX)

            n_rows = (len(reviews) + 1) // 2
            tbl_rev = doc.add_table(rows=n_rows, cols=2)
            tbl_rev.alignment = WD_TABLE_ALIGNMENT.CENTER
            tbl_rev.autofit = False
            tbl_rev.columns[0].width = Inches(3.6)
            tbl_rev.columns[1].width = Inches(3.6)
            set_modern_horizontal_borders(tbl_rev, border_hex="CBD5E1")

            for r_i in range(n_rows):
                for c_i in range(2):
                    cell_idx = r_i * 2 + c_i
                    cell = tbl_rev.cell(r_i, c_i)
                    set_cell_margins(cell, top_pt=4.0, bottom_pt=4.0, left_pt=6.0, right_pt=6.0)
                    p_c = cell.paragraphs[0]
                    p_c.paragraph_format.line_spacing = 1.1

                    if cell_idx < len(reviews):
                        r_num_b = p_c.add_run(f"#{cell_idx + 1} ")
                        format_run(r_num_b, font_name=FONT_JP, size_pt=12.0, bold=True, color_hex=COLOR_ACCENT_HEX)
                        r_rev = p_c.add_run(f"「{safe_nfc(reviews[cell_idx])}」")
                        format_run(r_rev, font_name=FONT_JP, size_pt=12.0, color_hex=COLOR_TEXT_MAIN_HEX)
                        note = review_source_note(s, cell_idx)
                        if note:
                            r_src = p_c.add_run(f" {note}")
                            format_run(r_src, font_name=FONT_JP, size_pt=9.0, color_hex=COLOR_TEXT_MUTED_HEX)

    # 第3部: 戦略的示唆
    doc.add_page_break()
    advice_sec_no = sec_no + 1
    h3 = doc.add_paragraph()
    h3.paragraph_format.space_before = Pt(6)
    h3.paragraph_format.space_after = Pt(4)
    r_h3 = h3.add_run(f"{advice_sec_no}. 空間・出店・利用戦略への戦略的示唆")
    format_run(r_h3, font_name=FONT_JP_TITLE, size_pt=16.0, bold=True, color_hex=COLOR_PRIMARY_HEX)
    add_callout_box(doc, safe_nfc(meta.get("strategic_advice", "")), title="【空間スカウティングからの戦略的提言】")

    add_sources_section(doc, meta)

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

    if meta.get("grounding_status") == "ungrounded":
        add_callout_box(
            doc,
            "この調査では Google 検索が実行されませんでした（モデル側の上限・タイムアウトのため）。"
            "\n実在の裏取りができないため、利用者のクチコミは掲載していません。"
            "\n記載内容は公式サイト等の一次情報でご確認ください。",
            title="【重要: 検索による裏取りができていません】",
            border_color_hex=COLOR_ACCENT_GOLD,
        )

    area_notes = []
    if meta.get("area_excluded"):
        names = "、".join(f"{e['name']}({e.get('distance_km')}km)" for e in meta["area_excluded"][:5])
        area_notes.append(f"指定エリアから離れていたため除外: {names}")
    if meta.get("area_unverified_count"):
        area_notes.append(f"住所を確認できずエリア判定ができなかったスポット: {meta['area_unverified_count']}件")
    if meta.get("address_backfilled_count"):
        area_notes.append(
            f"住所を追加の検索で補完したスポット: {meta['address_backfilled_count']}件"
            "（番地の精度は一次情報でご確認ください）"
        )
    if area_notes:
        add_callout_box(doc, "\n".join(area_notes), title="【エリア一致の検証について】")

    findings_list = meta.get("findings", [])
    if findings_list:
        add_callout_box(doc, "\n".join([safe_nfc(f) for f in findings_list]), title="【キー・ファインディングス】")

    has_map = bool(map_image_path and os.path.exists(map_image_path))
    if not has_map:
        # 地図を出せない理由を黙って伏せない (章が消えるだけだと理由が伝わらない)
        unmapped = [s for s in spots if not s.get("geocoded")]
        if unmapped:
            add_callout_box(
                doc,
                f"掲載 {len(spots)} 件のうち {len(unmapped)} 件は住所を確認できなかったため、"
                "位置関係マップは作成していません（推定位置で地図に描くことはしません）。",
                title="【広域俯瞰図について】",
            )
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
        reviews_count = as_int(s.get("reviews_count"))
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
        if key_topics:
            card_lines.append(f"■ 特徴: {' / '.join(key_topics)}")
        add_callout_box(doc, "\n".join(card_lines), title="【基本諸元・料金・混雑】")

        if not reviews:
            continue

        p_rev_h = doc.add_paragraph()
        p_rev_h.paragraph_format.space_before = Pt(3)
        p_rev_h.paragraph_format.space_after = Pt(2)
        r_rh = p_rev_h.add_run(f"▼ 調査で確認できた利用者の声（{len(reviews)}件）")
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
            note = review_source_note(s, r_idx)
            if note:
                r_src = p_rev.add_run(f" {note}")
                format_run(r_src, font_name=FONT_JP, size_pt=9.0, color_hex=COLOR_TEXT_MUTED_HEX)

    doc.add_page_break()
    advice_sec_no = sec_no + 1
    h3 = doc.add_paragraph()
    h3.paragraph_format.space_before = Pt(6)
    h3.paragraph_format.space_after = Pt(4)
    r_h3 = h3.add_run(f"{advice_sec_no}. 空間・出店・利用戦略への戦略的示唆")
    format_run(r_h3, font_name=FONT_JP_TITLE, size_pt=15.0, bold=True, color_hex=COLOR_PRIMARY_HEX)
    add_callout_box(doc, safe_nfc(meta.get("strategic_advice", "")), title="【空間スカウティングからの戦略的提言】")

    add_sources_section(doc, meta)

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

    # ファイル名に使う断片は必ずサニタイズする (本文表示には元の値を使う)
    area_fn = safe_filename_component(area, default="エリア")
    theme_fn = safe_filename_component(theme, default="テーマ")
    date_str = re.sub(r"[^0-9]", "", str(scouted_at))[:8] if scouted_at else datetime.now().strftime("%Y%m%d")
    count_str = f"_Top{spots_count}" if spots_count > 0 else ""

    # 1. 地図画像
    map_path = output_dir / f"{area_fn}_{theme_fn}_plot_map.png"
    try:
        # 戻り値を必ず受ける。座標を確認できたスポットが無い場合は None が返るため、
        # ここで無視すると「存在しないファイルのパス」を成果物として返してしまう。
        map_path = generate_spots_map_image(spots, map_path, area=area, theme=theme)
    except Exception as e:
        print(f"[Warning] 地図生成スキップ: {e}", file=sys.stderr)
        map_path = None

    # 2. ファイル名
    docx_name = f"【{area_fn}】{theme_fn}{count_str}比較調査レポート_{date_str}.docx"
    mobile_docx_name = f"【{area_fn}】{theme_fn}{count_str}比較調査レポート_スマホ閲覧用_{date_str}.docx"
    csv_name = f"【{area_fn}】{theme_fn}{count_str}スポット台帳_{date_str}.csv"
    folder_name = f"{date_str}_【{area_fn}】{theme_fn}{count_str}比較調査レポート"

    docx_path = output_dir / docx_name
    mobile_docx_path = output_dir / mobile_docx_name
    csv_path = output_dir / csv_name

    # 3. CSV
    create_csv_report(data, csv_path)
    # 4. PC Word
    create_docx_report(data, docx_path, map_image_path=map_path)
    # 5. Mobile Word
    create_docx_report_mobile(data, mobile_docx_path, map_image_path=map_path)

    return {
        "folder_name": folder_name,
        "docx_path": docx_path,
        "mobile_docx_path": mobile_docx_path,
        "csv_path": csv_path,
        "map_path": map_path,
        "data_json": output_dir / "data.json"
    }
