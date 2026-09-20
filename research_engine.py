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

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from PIL import Image

# Gemini の無料枠は「モデル単位の 1 日あたりリクエスト数」で切られる
# (実測: 429 GenerateRequestsPerDayPerProjectPerModel-FreeTier)。
# そのため候補は必ず別モデルを並べ、1 モデルが枯渇しても次へ落ちるようにする。
DEFAULT_MODELS = ("gemini-flash-latest", "gemini-2.5-flash", "gemini-2.5-flash-lite")
API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
REQUEST_TIMEOUT = 30.0
MAX_OUTPUT_TOKENS = 8192
PHOTOS_PER_SPOT = 2


class ResearchUnavailable(RuntimeError):
    """調査が成立しなかった。呼び出し元は捏造せず失敗として扱うこと。"""

    def __init__(self, message: str, reasons: list[str] | None = None):
        super().__init__(message)
        self.reasons = reasons or []


def download_and_crop_image(url: str, output_path: Path, target_w: int = 1200, target_h: int = 800) -> bool:
    """Webから画像をダウンロードし、指定アスペクト比で高品質リサイズ"""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    try:
        with urllib.request.urlopen(req, timeout=5) as res:
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
5. 住所は国土地理院でジオコーディングできる正式表記 (東京都…丁目…番…) にすること。
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


def attach_photos(data: dict, output_dir: Path) -> int:
    """調査結果が示した実在画像URLのみを取得して添付する。取得できなければ写真なし。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    attached = 0
    for i, s in enumerate(data.get("spots", [])):
        urls = [u for u in (s.get("photo_urls") or []) if isinstance(u, str) and u.startswith("http")]
        photos = []
        for p_idx, url in enumerate(urls[:PHOTOS_PER_SPOT]):
            target = output_dir / f"{s.get('id', f'spot_{i + 1}')}_photo_{p_idx + 1}.jpg"
            if download_and_crop_image(url, target):
                host = urllib.parse.urlparse(url).netloc
                photos.append(
                    {
                        "path": str(target),
                        "caption": f"{s.get('name', '')}（出典: {host}）",
                        "source_url": url,
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

        text_resp, sources, queries = _extract_text_and_sources(res_json)
        if text_resp:
            print(f"[Research Engine] モデル '{model}' で応答取得 (検索クエリ {len(queries)}件 / 出典 {len(sources)}件)")
            break
        reasons.append(f"{model}: 応答テキストが空")

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

    data["spots"] = filter_by_genre([s for s in spots if isinstance(s, dict)], theme)[:count]
    if not data["spots"]:
        raise ResearchUnavailable(
            f"「{area} × {theme}」に該当するスポットを確認できませんでした",
            reasons + [f"ジャンル絞り込み前 {len(spots)}件 → 0件"],
        )

    meta = data.setdefault("meta", {})
    meta.setdefault("area", area)
    meta.setdefault("theme", theme)
    meta.setdefault("scouted_at", datetime.now().strftime("%Y-%m-%d"))
    meta["sources"] = sources
    meta["search_queries"] = queries
    meta["requested_count"] = count
    meta["returned_count"] = len(data["spots"])

    if output_dir:
        attached = attach_photos(data, Path(output_dir))
        print(f"[Research Engine] 実在画像の取得: {attached}枚")

    print(f"[Research Engine] 調査完了: {len(data['spots'])} 件 (要求 {count} 件)")
    return data
