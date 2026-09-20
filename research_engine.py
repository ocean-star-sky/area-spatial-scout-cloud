#!/usr/bin/env python3
"""
research_engine.py
Google Gemini API (Google Search Grounding) でエリア×テーマのスポットを調査し、
構造化データ (JSON) を返すモジュール。

設計方針:
  リサーチが成立しなかった場合は ResearchUnavailable を送出する。
  架空のスポット・架空の口コミ・無関係なストック写真で「それらしい成果物」を
  作って返すことは行わない (調査レポートとしての信頼性を優先する)。
"""

import html
import ipaddress
import json
import os
import re
import socket
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from PIL import Image

from geocoding import geocode_address, haversine_km

# Gemini の無料枠は「モデル単位の 1 日あたりリクエスト数」で切られる
# (実測: 429 GenerateRequestsPerDayPerProjectPerModel-FreeTier)。
# そのため候補は必ず別モデルを並べ、1 モデルが枯渇しても次へ落ちるようにする。
DEFAULT_MODELS = ("gemini-flash-latest", "gemini-2.5-flash", "gemini-2.5-flash-lite")
API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
REQUEST_TIMEOUT = 60.0
MAX_OUTPUT_TOKENS = 8192
PHOTOS_PER_SPOT = 2

# 指定エリアからこの距離を超えるスポットは「別エリア」として除外する。
# 実測 (新橋を基準): 銀座 0.5km / 虎ノ門 0.7km / 六本木 2.3km / 渋谷 5.2km /
# 新宿 5.6km / 池袋 8.0km / 横浜 26km。同一・隣接と別エリアはこの値で分離できる。
AREA_MATCH_RADIUS_KM = float(os.environ.get("AREA_MATCH_RADIUS_KM", "3.0"))

# 住所が空だったスポットについて、住所だけを聞き直す追加リクエストを行うか。
# 1リクエスト増えるため、Gemini のモデル別日次上限を使い切る環境では無効化できる。
ADDRESS_BACKFILL_ENABLED = os.environ.get("ADDRESS_BACKFILL", "1") != "0"
ADDRESS_BACKFILL_MAX = int(os.environ.get("ADDRESS_BACKFILL_MAX", "20"))

# クチコミの目標件数。レポートの表示枠 (report_engine の mobile 版が reviews[:8]) に合わせる。
# 1回目の一括調査でここまで求めると JSON が maxOutputTokens で途切れてスポットごと落ちるため、
# 本調査とは別リクエストで集める (住所の backfill と同じ形)。
REVIEWS_TARGET = int(os.environ.get("REVIEWS_TARGET", "8"))
REVIEW_BACKFILL_ENABLED = os.environ.get("REVIEW_BACKFILL", "1") != "0"
REVIEW_BACKFILL_MAX = int(os.environ.get("REVIEW_BACKFILL_MAX", "20"))

# 公式サイトから OGP 画像を拾うときの制限。HTML は先頭だけ読めば meta に届く。
OG_FETCH_TIMEOUT = 5.0
OG_HTML_MAX_BYTES = 512 * 1024


class ResearchUnavailable(RuntimeError):
    """調査が成立しなかった。呼び出し元は捏造せず失敗として扱うこと。"""

    def __init__(self, message: str, reasons: list[str] | None = None):
        super().__init__(message)
        self.reasons = reasons or []


def is_public_http_url(url: str) -> bool:
    """外部から取得してよい URL か判定する。

    画像 URL は LLM 応答（=外部 Web の影響下にある untrusted 入力）なので、
    そのまま取得するとサーバ内部やクラウドのメタデータサーバへ要求を出せてしまう。
    http/https 以外と、名前解決先が内部アドレスになるホストを拒否する。
    """
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    except (socket.gaierror, UnicodeError, ValueError):
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return bool(infos)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """リダイレクト先で内部アドレスへ飛ばされるのを防ぐため、転送自体を許可しない"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def download_and_crop_image(url: str, output_path: Path, target_w: int = 1200, target_h: int = 800) -> bool:
    """Webから画像をダウンロードし、指定アスペクト比で高品質リサイズ"""
    if not is_public_http_url(url):
        print(f"[Warning] 取得を許可しないURLのためスキップ: {url[:80]}")
        return False
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    try:
        with opener.open(req, timeout=5) as res:
            with open(output_path, "wb") as f:
                f.write(res.read())

        with Image.open(output_path) as img:
            img = img.convert("RGB")
            target_ratio = target_w / target_h
            w, h = img.size
            if w / h > target_ratio:
                new_w = int(h * target_ratio)
                left = (w - new_w) // 2
                img = img.crop((left, 0, left + new_w, h))
            else:
                new_h = int(w / target_ratio)
                top = (h - new_h) // 2
                img = img.crop((0, top, w, top + new_h))
            img = img.resize((target_w, target_h), Image.Resampling.LANCZOS)
            img.save(output_path, "JPEG", quality=85)
        return True
    except Exception as e:
        print(f"[Warning] 画像ダウンロード失敗 ({url}): {e}")
        output_path.unlink(missing_ok=True)
        return False


class _ValidatedRedirect(urllib.request.HTTPRedirectHandler):
    """転送先を毎ホップ検証する。

    公式サイトは http->https や www 付与で転送するのが普通なので、画像直取得で使う
    _NoRedirect (転送全面禁止) をページ取得に流用すると大半が取れない。かわりに
    転送先URLを1ホップずつ is_public_http_url に通し、内部アドレスへ誘導されたら止める。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not is_public_http_url(newurl):
            print(f"[OGP] 転送先が許可されないためたどりません: {newurl[:80]}")
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _fetch_html_head(page_url: str) -> tuple[str, str] | None:
    """ページ HTML の先頭 (最大 OG_HTML_MAX_BYTES) を取得する。(最終URL, HTML) を返す。"""
    opener = urllib.request.build_opener(_ValidatedRedirect)
    req = urllib.request.Request(
        page_url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    try:
        with opener.open(req, timeout=OG_FETCH_TIMEOUT) as res:
            ctype = (res.headers.get("Content-Type") or "").lower()
            if ctype and "html" not in ctype:
                return None
            raw = res.read(OG_HTML_MAX_BYTES)
            charset = res.headers.get_content_charset() or "utf-8"
            final_url = res.geturl() or page_url
    except Exception as e:
        print(f"[OGP] ページを取得できません ({page_url[:60]}): {type(e).__name__}")
        return None
    try:
        return final_url, raw.decode(charset, errors="ignore")
    except LookupError:
        return final_url, raw.decode("utf-8", errors="ignore")


# og:image を優先し、無ければ twitter:image を使う。
_OG_IMAGE_KEYS = ("og:image", "og:image:secure_url", "og:image:url", "twitter:image", "twitter:image:src")
_META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_META_KEY_RE = re.compile(r"(?:property|name)\s*=\s*[\"\']?\s*([a-zA-Z0-9:_-]+)", re.IGNORECASE)
_META_CONTENT_RE = re.compile(r"content\s*=\s*[\"\']([^\"\']+)[\"\']", re.IGNORECASE)


def fetch_og_image_url(page_url: str) -> str | None:
    """公式サイトの OGP 画像URLを返す。取得できなければ None。

    Gemini は photo_urls を空で返すことが多い (実測: 3ジョブ連続で 0件)。
    施設の公式サイトは本調査で既に取れているので、そのページが自ら宣言している
    代表画像 (og:image) を写真候補にする。追加の外部APIは使わない。
    """
    page_url = (page_url or "").strip()
    if not page_url or not is_public_http_url(page_url):
        return None
    fetched = _fetch_html_head(page_url)
    if not fetched:
        return None
    final_url, html_text = fetched

    found: dict[str, str] = {}
    for tag in _META_TAG_RE.finditer(html_text):
        raw_tag = tag.group(0)
        m_key = _META_KEY_RE.search(raw_tag)
        if not m_key:
            continue
        key = m_key.group(1).lower()
        if key not in _OG_IMAGE_KEYS or key in found:
            continue
        m_val = _META_CONTENT_RE.search(raw_tag)
        if m_val and m_val.group(1).strip():
            found[key] = m_val.group(1).strip()

    for key in _OG_IMAGE_KEYS:
        if key not in found:
            continue
        # OGP の content は相対パスでも規約違反ではないため絶対URLへ直す
        candidate = urllib.parse.urljoin(final_url, html.unescape(found[key]))
        if is_public_http_url(candidate):
            return candidate
        print(f"[OGP] 画像URLが許可されないため使いません: {candidate[:80]}")
    return None


def extract_json_from_text(text: str) -> dict:
    """LLM応答テキストからJSONオブジェクトを抽出する。抽出できなければ ValueError。"""
    text = text.strip()

    # 1. コードブロック抽出
    m = re.search(r"```(?:json)?\s*([\s\S]*?)(?:```|$)", text)
    if m:
        text = m.group(1).strip()

    fb = text.find("{")
    if fb == -1:
        raise ValueError("JSONの開始 '{' が見つかりません")
    text = text[fb:]

    # 2. そのままパース
    try:
        return json.loads(text, strict=False)
    except Exception:
        pass

    # 3. 末尾カンマ除去
    cleaned = re.sub(r",\s*([\]}])", r"\1", text)
    try:
        return json.loads(cleaned, strict=False)
    except Exception:
        pass

    # 4. 途切れ自動修復（未完了の括弧・文字列を補完）
    stack = []
    in_str = False
    esc = False
    for c in cleaned:
        if esc:
            esc = False
            continue
        if c == "\\":
            esc = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if not in_str:
            if c in "{[":
                stack.append("}" if c == "{" else "]")
            elif c in "}]":
                if stack and stack[-1] == c:
                    stack.pop()

    repaired = cleaned
    if in_str:
        repaired += '"'
    repaired = re.sub(r"[:,\s]+$", "", repaired)
    repaired += "".join(reversed(stack))
    try:
        return json.loads(repaired, strict=False)
    except Exception:
        pass

    # 5. 最後の完全なスポットで打ち切って閉じる
    last_spot_end = cleaned.rfind("},")
    if last_spot_end != -1:
        try:
            return json.loads(cleaned[: last_spot_end + 1] + "]}", strict=False)
        except Exception:
            pass

    raise ValueError("応答から有効なJSONを抽出できませんでした")


def classify_genre(text: str) -> str:
    """ジャンル判定 — 異ジャンルの混入を弾くために使用"""
    t = (text or "").lower()
    if any(k in t for k in ["サウナ", "スパ", "銭湯", "温泉", "風呂", "ロウリュ", "水風呂", "sauna"]):
        return "サウナ"
    if any(k in t for k in ["鮨", "寿司", "すし", "sushi"]):
        return "鮨"
    if any(k in t for k in ["コワーキング", "シェアオフィス", "オフィス", "ラウンジ", "作業", "ワークスペース", "coworking"]):
        return "コワーキング"
    if any(k in t for k in ["イベント", "展示", "ホール", "アリーナ", "ビッグサイト", "カンファレンス"]):
        return "イベント"
    return "その他"


def build_prompt(area: str, theme: str, count: int) -> str:
    """Gemini へ渡す調査プロンプトを組み立てる"""
    return f"""
あなたは商業空間・飲食・施設の調査コンサルタントです。
Google 検索を必ず使い、エリア「{area}」における「{theme}」の上位{count}件を調査してください。

【厳守事項】
1. 検索で実在が確認できた施設のみを挙げること。推測で店名・住所・料金を創作しないこと。
2. 確認できなかった項目は、値を空文字 "" にすること。埋め合わせの作り話をしないこと。
3. クチコミ (reviews) には、検索結果に実際に存在した利用者の声の要約のみを入れること。
   実在のクチコミが見つからない場合は空配列 [] にすること。
4. テーマが「個室」「接待」を含む場合は、個室の有無を検索で確認できた施設を優先すること。
5. 住所は地図生成に使うため、検索で確認できた施設は必ず正式表記 (東京都…丁目…番…) で
   記載すること。ただし確認できない場合に住所を創作してはならない (空文字のままにする)。
6. 出力は ```json ... ``` のコードブロック内に、下記スキーマのJSONのみ。前置き・解説は不要。

【出力JSONスキーマ】
{{
  "meta": {{
    "area": "{area}",
    "theme": "{theme}",
    "scouted_at": "{datetime.now().strftime('%Y-%m-%d')}",
    "summary_text": "調査概要 (300字程度、検索で分かった事実のみ)",
    "findings": ["事実に基づく所見1", "所見2", "所見3", "所見4"],
    "strategic_advice": "1. 【…】…\\n\\n2. 【…】…"
  }},
  "spots": [
    {{
      "id": "spot_1",
      "name": "施設・店舗名",
      "category": "業態・特徴",
      "rating": "評価 (確認できた場合のみ。例: 食べログ 3.65)",
      "reviews_count": 350,
      "address": "東京都…",
      "url": "公式サイトまたは予約URL",
      "key_topics": ["特徴1", "特徴2", "特徴3", "特徴4"],
      "pricing": "昼: … ｜ 夜: …",
      "popular_times": {{"peak_time": "", "quiet_time": ""}},
      "reviews": ["実在するクチコミの要約1", "要約2"],
      "photo_urls": ["公式サイト等にある画像の直リンクURL (無ければ空配列)"]
    }}
  ]
}}
"""


def call_gemini(model: str, prompt: str, api_key: str, timeout: float = REQUEST_TIMEOUT) -> dict:
    """Gemini REST API を Google Search グラウンディング付きで1回呼ぶ。応答JSONを返す。"""
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        # 実測: google_search を付けると webSearchQueries が返り実際に検索が走る。
        "tools": [{"google_search": {}}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": MAX_OUTPUT_TOKENS,
            # 本プロジェクトの Gemini 運用既定。実測で検索の発火は阻害しない。
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    url = f"{API_BASE}/{model}:generateContent?key={urllib.parse.quote(api_key.strip())}"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return json.loads(res.read().decode("utf-8"))


def _extract_text_and_sources(res_json: dict) -> tuple[str, list[dict], list[str]]:
    """Gemini 応答から本文テキスト・グラウンディング出典・検索クエリを取り出す"""
    candidates = res_json.get("candidates", [])
    if not candidates:
        return "", [], []
    cand = candidates[0]
    parts = cand.get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts if "text" in p).strip()

    gm = cand.get("groundingMetadata", {}) or {}
    sources = []
    for chunk in gm.get("groundingChunks", []) or []:
        web = chunk.get("web") or {}
        if web.get("uri"):
            sources.append({"title": web.get("title", ""), "uri": web["uri"]})
    queries = list(gm.get("webSearchQueries", []) or [])
    return text, sources, queries


def _normalize_name(name: str) -> str:
    """施設名の照合用キー。全角/半角・空白・大小文字の揺れを吸収する。"""
    return unicodedata.normalize("NFKC", str(name or "")).replace(" ", "").replace("　", "").casefold()


def build_address_backfill_prompt(names: list[str], area: str, theme: str) -> str:
    """住所が空のままの施設について、住所だけを問い合わせるプロンプト"""
    listing = "\n".join(f"- {n}" for n in names)
    return f"""Google検索を使い、次の施設 (「{area}」周辺の「{theme}」) それぞれの所在地を調べてください。

{listing}

【厳守】
- 住所は都道府県から始まる正式表記にすること。
- 検索で確認できなかった施設は address を空文字 "" にすること。推測で住所を書かないこと。
- 入力した施設名を name にそのまま使い、勝手に別施設へ置き換えないこと。
- 出力は ```json ... ``` の中に次の形のJSONのみ。前置き・解説は不要。

{{"results": [{{"name": "施設名", "address": "東京都…", "source_url": ""}}]}}"""


def backfill_addresses(
    spots: list[dict],
    area: str,
    theme: str,
    api_key: str,
    models: tuple = DEFAULT_MODELS,
) -> int:
    """住所が空のスポットについて、追加の1リクエストで住所だけを補完する。

    1回目の一括調査は 1 リクエストで全項目を埋めさせるため、住所が空で返ることが多い
    (実測: 新橋×サウナ 8件中 4件が空)。対象を絞って住所だけを聞き直すと埋まる
    (実測: 空だった 4件のうち 3件を補完)。

    補完できなかったものは空のまま残す。ここで推測住所を入れてはいけない。
    失敗しても例外を投げない (補完は付加価値であって、調査本体の成否ではない)。
    """
    if not ADDRESS_BACKFILL_ENABLED:
        return 0

    targets = [s for s in spots if not (s.get("address") or "").strip() and (s.get("name") or "").strip()]
    if not targets:
        return 0
    targets = targets[:ADDRESS_BACKFILL_MAX]

    prompt = build_address_backfill_prompt([s["name"] for s in targets], area, theme)
    text = ""
    for model in models:
        try:
            text, _sources, _queries = _extract_text_and_sources(call_gemini(model, prompt, api_key))
        except Exception as e:
            print(f"[Address Backfill] {model} で失敗: {type(e).__name__}")
            continue
        if text:
            break
    if not text:
        print("[Address Backfill] 住所の追加取得ができませんでした (空のまま扱います)")
        return 0

    try:
        rows = extract_json_from_text(text).get("results", [])
    except Exception as e:
        print(f"[Address Backfill] 応答を解析できません: {e}")
        return 0

    by_name = {_normalize_name(s["name"]): s for s in targets}
    filled = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        spot = by_name.get(_normalize_name(row.get("name")))
        address = (row.get("address") or "").strip()
        # 既に住所があるものは上書きしない (1回目の結果を後から薄い根拠で置き換えない)
        if not spot or not address or (spot.get("address") or "").strip():
            continue
        spot["address"] = address
        spot["address_source"] = "backfill"
        if row.get("source_url"):
            spot["address_source_url"] = row["source_url"]
        filled += 1

    print(f"[Address Backfill] {len(targets)}件中 {filled}件の住所を補完しました")
    return filled


def _review_key(text: str) -> str:
    """クチコミの重複判定キー。表記の揺れと空白を吸収する。"""
    return unicodedata.normalize("NFKC", str(text or "")).replace(" ", "").replace("　", "").casefold()


def normalize_spot_reviews(spot: dict) -> None:
    """spot["reviews"] を文字列の配列へ揃え、出典を review_sources に並置する。

    レポート側 (report_engine) は reviews の要素を文字列として扱う (safe_nfc に渡す) ため、
    LLM が {"text": ..., "source_url": ...} 形式で返した場合もここで平坦化しておく。
    """
    raw = spot.get("reviews")
    if not isinstance(raw, list):
        spot["reviews"] = []
        spot["review_sources"] = []
        return
    prior_sources = spot.get("review_sources")
    texts: list[str] = []
    sources: list[str] = []
    for i, item in enumerate(raw):
        if isinstance(item, dict):
            body = str(item.get("text") or item.get("review") or "").strip()
            source = str(item.get("source_url") or "").strip()
        else:
            body = str(item or "").strip()
            source = ""
            if isinstance(prior_sources, list) and i < len(prior_sources):
                source = str(prior_sources[i] or "").strip()
        if not body:
            continue
        texts.append(body)
        sources.append(source)
    spot["reviews"] = texts
    spot["review_sources"] = sources


def merge_reviews(spot: dict, incoming) -> int:
    """出典付きのクチコミを既存へ追記する。既存は消さず、重複と出典無しは採らない。"""
    if not isinstance(incoming, list):
        return 0
    normalize_spot_reviews(spot)
    texts = spot["reviews"]
    sources = spot["review_sources"]
    seen = {_review_key(t) for t in texts}
    added = 0
    for item in incoming:
        if len(texts) >= REVIEWS_TARGET:
            break
        # 出典を言えないクチコミは採らない (実在の裏取りができないため)
        if not isinstance(item, dict):
            continue
        body = str(item.get("text") or "").strip()
        source = str(item.get("source_url") or "").strip()
        if not body or not source.startswith("http"):
            continue
        key = _review_key(body)
        if key in seen:
            continue
        seen.add(key)
        texts.append(body)
        sources.append(source)
        added += 1
    return added


def build_review_backfill_prompt(names: list[str], area: str, theme: str, target: int) -> str:
    """クチコミが足りない施設について、クチコミだけを問い合わせるプロンプト"""
    listing = "\n".join(f"- {n}" for n in names)
    return f"""Google検索を使い、次の施設 (「{area}」周辺の「{theme}」) それぞれの利用者のクチコミを調べてください。

{listing}

【厳守】
- 検索結果に実際に存在した利用者の声の要約のみを入れること。
  実在のクチコミが見つからない場合は reviews を空配列 [] にすること。創作は禁止。
- 1施設あたり最大{target}件。1件は60〜120字程度の要約にすること。
- text ごとに、その声を確認できたページの URL を source_url に必ず入れること。
  URL を示せない声は挙げないこと。
- 入力した施設名を name にそのまま使い、勝手に別施設へ置き換えないこと。
- 出力は ```json ... ``` の中に次の形のJSONのみ。前置き・解説は不要。

{{"results": [{{"name": "施設名", "reviews": [{{"text": "クチコミの要約", "source_url": "https://…"}}]}}]}}"""


def backfill_reviews(
    spots: list[dict],
    area: str,
    theme: str,
    api_key: str,
    models: tuple = DEFAULT_MODELS,
) -> int:
    """クチコミが目標件数に届かないスポットについて、追加の1リクエストで補完する。

    1回目の一括調査でクチコミまで求めると maxOutputTokens で JSON が途切れ、
    切り詰め修復でスポットごと落ちる。そのため住所と同じく対象を絞って聞き直す。
    失敗しても例外を投げない (補完は付加価値であって、調査本体の成否ではない)。
    """
    if not REVIEW_BACKFILL_ENABLED:
        return 0

    for s in spots:
        normalize_spot_reviews(s)

    targets = [s for s in spots if (s.get("name") or "").strip() and len(s["reviews"]) < REVIEWS_TARGET]
    if not targets:
        return 0
    targets = targets[:REVIEW_BACKFILL_MAX]

    prompt = build_review_backfill_prompt([s["name"] for s in targets], area, theme, REVIEWS_TARGET)
    text = ""
    for model in models:
        try:
            text, _sources, _queries = _extract_text_and_sources(call_gemini(model, prompt, api_key))
        except Exception as e:
            print(f"[Review Backfill] {model} で失敗: {type(e).__name__}")
            continue
        if text:
            break
    if not text:
        print("[Review Backfill] クチコミの追加取得ができませんでした (既存のまま扱います)")
        return 0

    try:
        rows = extract_json_from_text(text).get("results", [])
    except Exception as e:
        print(f"[Review Backfill] 応答を解析できません: {e}")
        return 0

    by_name = {_normalize_name(s["name"]): s for s in targets}
    added = 0
    filled_spots = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        spot = by_name.get(_normalize_name(row.get("name")))
        if not spot:
            continue
        got = merge_reviews(spot, row.get("reviews"))
        added += got
        if got:
            filled_spots += 1

    print(f"[Review Backfill] {len(targets)}件中 {filled_spots}件にクチコミを合計 {added}件追加しました")
    return added


def _area_tokens(area: str) -> list[str]:
    """エリア指定から住所照合に使う語を作る (「新橋駅」「新橋エリア」→「新橋」)"""
    base = (area or "").strip()
    tokens = {base}
    for suffix in ("駅周辺", "駅前", "駅", "エリア", "周辺", "近辺"):
        if base.endswith(suffix) and len(base) > len(suffix):
            tokens.add(base[: -len(suffix)])
    return [t for t in tokens if t]


def verify_area_match(
    spots: list[dict],
    area: str,
    radius_km: float = AREA_MATCH_RADIUS_KM,
) -> tuple[list[dict], list[dict]]:
    """指定エリアに実際に所在するスポットだけを残す。

    基準座標の求め方に注意: 「新橋」のような地名だけを国土地理院へ渡すと、全国の
    同名地点の先頭 (実測では宮城県) が返るため基準に使えない。そこで
    「住所にエリア名を含むスポット」を先に確定させ、その重心を基準座標とする。

    判定は 3 通り:
      - address にエリア名を含む            -> 一致 (method="address")
      - 基準から radius_km 以内             -> 一致 (method="proximity")
      - 基準から radius_km より遠い          -> 除外 (別エリアと確認できた)
    住所が無い / 解決できない場合は「確認できない」として残し、フラグで示す。
    基準座標を 1 件も確定できない場合は、除外の根拠が無いため誰も落とさない。
    """
    tokens = _area_tokens(area)
    anchors: list[tuple[float, float]] = []

    # 第1段: 住所にエリア名を含むものを基準として確定する
    for s in spots:
        addr = (s.get("address") or "").strip()
        if addr and any(t in addr for t in tokens):
            s["area_match"] = "address"
            coord = geocode_address(addr)
            if coord:
                s["lat"], s["lon"] = coord
                anchors.append(coord)

    if not anchors:
        # 基準が作れない = 遠い近いを判断する根拠が無い。憶測で落とさない。
        for s in spots:
            s.setdefault("area_match", "unverified")
        print(f"[Area Filter] 「{area}」の基準座標を確定できないため、エリア判定は行いません")
        return spots, []

    ref = (
        sum(c[0] for c in anchors) / len(anchors),
        sum(c[1] for c in anchors) / len(anchors),
    )

    kept: list[dict] = []
    dropped: list[dict] = []
    for s in spots:
        if s.get("area_match") == "address":
            kept.append(s)
            continue

        addr = (s.get("address") or "").strip()
        coord = geocode_address(addr) if addr else None
        if not coord:
            s["area_match"] = "unverified"
            kept.append(s)
            continue

        distance = haversine_km(ref, coord)
        s["distance_km"] = round(distance, 2)
        if distance <= radius_km:
            s["lat"], s["lon"] = coord
            s["area_match"] = "proximity"
            kept.append(s)
        else:
            s["area_match"] = "outside"
            dropped.append(s)
            print(f"[Area Filter] 別エリアのため除外: {s.get('name', '?')} ({distance:.1f}km, {addr[:24]})")

    return kept, dropped


def filter_by_genre(spots: list[dict], theme: str) -> list[dict]:
    """要求ジャンルと明確に矛盾するスポットを除外する"""
    user_genre = classify_genre(theme)
    if user_genre == "その他":
        return spots
    kept = []
    for s in spots:
        spot_genre = classify_genre(f"{s.get('name', '')} {s.get('category', '')}")
        if spot_genre in (user_genre, "その他"):
            kept.append(s)
        else:
            print(f"[Genre Filter] 異ジャンル排除: '{s.get('name', '?')}' (検出={spot_genre}, 要求={user_genre})")
    return kept


def safe_spot_slug(raw, fallback: str) -> str:
    """LLM が返した id をファイル名に使えるスラグへ正規化する。

    id は LLM 応答そのもの (= untrusted) なので、そのままパスへ連結すると
    出力ディレクトリの外にファイルを書けてしまう。
    """
    slug = re.sub(r"[^A-Za-z0-9_-]", "", str(raw or ""))[:40]
    return slug or fallback


def attach_photos(data: dict, output_dir: Path) -> int:
    """調査結果が示した実在画像URLのみを取得して添付する。取得できなければ写真なし。"""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    attached = 0
    for i, s in enumerate(data.get("spots", [])):
        urls = [u for u in (s.get("photo_urls") or []) if isinstance(u, str) and u.startswith("http")]
        origins = ["llm"] * len(urls)
        if not urls:
            # 調査応答が画像URLを返さなかった場合だけ、公式サイトの OGP 画像を候補にする
            og_url = fetch_og_image_url(s.get("url") or "")
            if og_url:
                urls, origins = [og_url], ["ogp"]
        photos = []
        slug = safe_spot_slug(s.get("id"), f"spot_{i + 1}")
        for p_idx, url in enumerate(urls[:PHOTOS_PER_SPOT]):
            target = output_dir / f"{slug}_photo_{p_idx + 1}.jpg"
            if download_and_crop_image(url, target):
                host = urllib.parse.urlparse(url).netloc
                photos.append(
                    {
                        "path": str(target),
                        "caption": f"{s.get('name', '')}（出典: {host}）",
                        "source_url": url,
                        "photo_source": origins[p_idx],
                    }
                )
        if photos:
            s["photos"] = photos
            attached += len(photos)
    return attached


def run_autonomous_research(
    area: str,
    theme: str,
    count: int = 10,
    output_dir: Path = None,
    api_key: str = None,
    models: tuple = DEFAULT_MODELS,
) -> dict:
    """
    Gemini + Google Search でエリア×テーマの Top N を調査する。
    成立しなかった場合は ResearchUnavailable を送出する (架空データは返さない)。
    """
    key = api_key or os.environ.get("GEMINI_API_KEY", "")
    if not key.strip():
        raise ResearchUnavailable(
            "GEMINI_API_KEY が設定されていないため調査を実行できません",
            ["GEMINI_API_KEY 未設定"],
        )

    prompt = build_prompt(area, theme, count)
    print(f"[Research Engine] 調査開始: {area} × {theme} (Top {count})")

    reasons: list[str] = []
    text_resp, sources, queries = "", [], []
    # 検索が走らなかった応答は捨てずに退避し、全モデルが未グラウンディングだった時だけ使う
    ungrounded: tuple[str, list, list] | None = None

    for model in models:
        try:
            res_json = call_gemini(model, prompt, key)
        except urllib.error.HTTPError as he:
            body = he.read().decode("utf-8", errors="ignore")[:200]
            reasons.append(f"{model}: HTTP {he.code} {body}")
            print(f"[Warning] Gemini APIエラー ({model}): {he.code} {body}")
            continue
        except Exception as e:
            reasons.append(f"{model}: {type(e).__name__}: {e}")
            print(f"[Warning] Gemini 通信エラー ({model}): {e}")
            continue

        cand_text, cand_sources, cand_queries = _extract_text_and_sources(res_json)
        if not cand_text:
            reasons.append(f"{model}: 応答テキストが空")
            continue
        if not cand_queries:
            # 実測 (9/20 五反田): 検索クエリ 0件 = Google 検索を使わずに生成した応答。
            # 裏取りが無いので、他のモデルで検索付きの応答が取れないか先に試す。
            reasons.append(f"{model}: 検索が実行されていない (webSearchQueries 0件)")
            print(f"[Warning] モデル '{model}' は検索を使わずに応答しました。次のモデルを試します")
            if ungrounded is None:
                ungrounded = (cand_text, cand_sources, cand_queries)
            continue
        text_resp, sources, queries = cand_text, cand_sources, cand_queries
        print(f"[Research Engine] モデル '{model}' で応答取得 (検索クエリ {len(queries)}件 / 出典 {len(sources)}件)")
        break

    grounded = bool(text_resp)
    if not grounded and ungrounded is not None:
        text_resp, sources, queries = ungrounded
        print("[Warning] どのモデルも検索を使えませんでした。裏取り無しの応答を使いますが、クチコミは掲載しません")

    if not text_resp:
        raise ResearchUnavailable("Gemini から調査結果を取得できませんでした", reasons)

    try:
        data = extract_json_from_text(text_resp)
    except Exception as parse_err:
        reasons.append(f"JSONパース失敗: {parse_err}")
        raise ResearchUnavailable("調査結果の解析に失敗しました", reasons) from parse_err

    if not isinstance(data, dict):
        raise ResearchUnavailable("調査結果の形式が不正です", reasons + [f"型={type(data).__name__}"])

    spots = data.get("spots")
    if not isinstance(spots, list):
        raise ResearchUnavailable("調査結果にスポット一覧が含まれていません", reasons)

    by_genre = filter_by_genre([s for s in spots if isinstance(s, dict)], theme)
    # エリア判定より前に住所を補完する。住所が無いとエリアの確認も地図掲載もできないため。
    backfilled = backfill_addresses(by_genre, area, theme, key, models)
    in_area, out_of_area = verify_area_match(by_genre, area)
    data["spots"] = in_area[:count]
    if not data["spots"]:
        raise ResearchUnavailable(
            f"「{area} × {theme}」に該当するスポットを確認できませんでした",
            reasons
            + [
                f"取得 {len(spots)}件 → ジャンル一致 {len(by_genre)}件 → エリア一致 0件",
                *[f"別エリア: {s.get('name', '?')} ({s.get('distance_km')}km)" for s in out_of_area[:3]],
            ],
        )

    # クチコミの形を揃えてから確定させる (LLM は文字列と {text, source_url} の両方を返しうる)
    for s in data["spots"]:
        normalize_spot_reviews(s)

    if grounded:
        reviews_added = backfill_reviews(data["spots"], area, theme, key, models)
    else:
        # 検索が走っていない応答の「利用者の声」は実在の裏取りが無い。載せない。
        dropped = sum(len(s["reviews"]) for s in data["spots"])
        for s in data["spots"]:
            s["reviews"] = []
            s["review_sources"] = []
        reviews_added = 0
        print(f"[Research Engine] 未グラウンディングのためクチコミ {dropped}件を掲載対象から外しました")

    meta = data.setdefault("meta", {})
    meta.setdefault("area", area)
    meta.setdefault("theme", theme)
    meta.setdefault("scouted_at", datetime.now().strftime("%Y-%m-%d"))
    meta["sources"] = sources
    meta["search_queries"] = queries
    meta["requested_count"] = count
    meta["returned_count"] = len(data["spots"])
    meta["area_match_radius_km"] = AREA_MATCH_RADIUS_KM
    meta["address_backfilled_count"] = sum(1 for s in data["spots"] if s.get("address_source") == "backfill")
    meta["address_backfill_attempted"] = backfilled
    meta["grounding_status"] = "grounded" if grounded else "ungrounded"
    meta["reviews_backfilled_count"] = reviews_added
    meta["reviews_total"] = sum(len(s.get("reviews") or []) for s in data["spots"])
    meta["area_verified_count"] = sum(1 for s in data["spots"] if s.get("area_match") in ("address", "proximity"))
    meta["area_unverified_count"] = sum(1 for s in data["spots"] if s.get("area_match") == "unverified")
    meta["area_excluded"] = [
        {"name": s.get("name", ""), "address": s.get("address", ""), "distance_km": s.get("distance_km")}
        for s in out_of_area
    ]

    if output_dir:
        attached = attach_photos(data, Path(output_dir))
        print(f"[Research Engine] 実在画像の取得: {attached}枚")

    print(
        f"[Research Engine] 調査完了: {len(data['spots'])} 件 (要求 {count} 件 / "
        f"エリア確認済 {meta['area_verified_count']} 件 / 別エリア除外 {len(out_of_area)} 件)"
    )
    return data
