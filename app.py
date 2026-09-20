#!/usr/bin/env python3
"""
app.py
Area Spatial Scout - Cloud Run サーバーアプリケーション (FastAPI)

スマホからのリクエストを受け、Gemini (Google Search グラウンディング) で調査 →
地図合成 → Word/CSV 生成 → 共有ドライブ保存 を実行する。

設計方針:
  - 調査が成立しなかった場合は 200 の偽成功を返さず、失敗として返す。
  - 認証は全 API 共通の依存関数に一本化し、SCOUT_PASSWORD 未設定なら全拒否 (fail-closed)。
"""

import hmac
import json
import os
import re
import secrets
import shutil
import tempfile
import traceback
import uuid
import zipfile
from collections import OrderedDict
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from drive_uploader import DriveUploadError, upload_report_directory
from report_engine import generate_full_report_pack
from research_engine import ResearchUnavailable, run_autonomous_research

app = FastAPI(title="Area Spatial Scout Cloud", version="3.0.0")

# CORS: allow_credentials=True と allow_origins=["*"] の併用は仕様上無効なため
# 資格情報は送らない前提 (認証はパスワード/トークン) で credentials=False とする。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

BASE_DIR = Path(__file__).resolve().parent

# アクセスキー。未設定の場合は全 API を拒否する (fail-closed)。
SCOUT_PASSWORD = os.environ.get("SCOUT_PASSWORD", "")

# ダウンロードトークンの署名鍵。アクセスキーとは別の秘密にする。
# 同じ鍵を使うと、URL に載るトークンとジョブIDの組から
# アクセスキー自体をオフラインで総当たりできる検証オラクルになるため。
SCOUT_TOKEN_SECRET = os.environ.get("SCOUT_TOKEN_SECRET", "")

# 診断エンドポイントは明示的に有効化したときのみ公開 (Gemini 課金を焼くため)
SCOUT_DEBUG_ENABLED = os.environ.get("SCOUT_DEBUG_ENABLED", "") == "1"

# 成果物キャッシュディレクトリ
OUTPUTS_DIR = Path(tempfile.gettempdir()) / "scout_outputs"
OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

# job_id の正規形 (これ以外は一切ディスクに触らせない)
JOB_ID_RE = re.compile(r"^[0-9]{8}_[0-9]{6}_[0-9a-f]{6}$")

MAX_TRACKED_JOBS = 100
JOBS: "OrderedDict[str, dict]" = OrderedDict()


# --------------------------------------------------------------------------
# 認証・パス安全性
# --------------------------------------------------------------------------
def require_auth(password: str = "") -> None:
    """全 API 共通の認証。SCOUT_PASSWORD 未設定なら全拒否。"""
    if not SCOUT_PASSWORD:
        raise HTTPException(
            status_code=503,
            detail="SCOUT_PASSWORD が未設定のためサービスを提供できません（管理者設定が必要です）",
        )
    if not password or not secrets.compare_digest(password, SCOUT_PASSWORD):
        raise HTTPException(status_code=401, detail="アクセスキー（パスワード）が正しくありません")


def require_auth_header(x_scout_key: str = Header("")) -> None:
    """ヘッダ経由の認証 (GET 用)。

    クエリ文字列に載せるとアクセスログ (Cloud Logging の httpRequest.requestUrl) に
    平文で残るため、管理系 GET はヘッダのみ受け付ける。
    """
    require_auth(x_scout_key)


def _token_key() -> bytes:
    """トークン署名鍵。未設定時はアクセスキーから派生するが、用途を分離する。"""
    if SCOUT_TOKEN_SECRET:
        return SCOUT_TOKEN_SECRET.encode("utf-8")
    return sha256(b"area-spatial-scout/download-token/v1|" + SCOUT_PASSWORD.encode("utf-8")).digest()


def download_token(job_id: str) -> str:
    """ジョブ単位のダウンロード用トークン。リンクを踏むだけで開けるが推測はできない。"""
    return hmac.new(_token_key(), job_id.encode("utf-8"), sha256).hexdigest()[:32]


def resolve_job_dir(job_id: str) -> Path:
    """job_id を検証し OUTPUTS_DIR 配下の実ディレクトリだけを返す。"""
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=404, detail="ファイルが見つかりません")
    job_dir = (OUTPUTS_DIR / job_id).resolve()
    if not job_dir.is_relative_to(OUTPUTS_DIR.resolve()) or not job_dir.is_dir():
        raise HTTPException(status_code=404, detail="ファイルが見つかりません")
    return job_dir


def new_job_id() -> str:
    return f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"


def remember_job(job_id: str, payload: dict) -> None:
    JOBS[job_id] = payload
    while len(JOBS) > MAX_TRACKED_JOBS:
        JOBS.popitem(last=False)


# --------------------------------------------------------------------------
# 成果物ユーティリティ
# --------------------------------------------------------------------------
def create_report_zip(job_dir: Path, folder_name: str) -> Path:
    """
    成果物のみをZIP化する。job_dir 内で直接 make_archive すると生成中のZIP自身を
    再帰的に取り込んで膨張するため、job_dir の外で作ってから移動する。
    """
    zip_path = job_dir / f"{folder_name}_一括納品パック.zip"
    temp_zip = job_dir.parent / f"temp_{uuid.uuid4().hex[:8]}.zip"
    try:
        with zipfile.ZipFile(temp_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in job_dir.glob("*"):
                if f.is_file() and not f.name.endswith(".zip") and not f.name.startswith("."):
                    zf.write(f, arcname=f"{folder_name}/{f.name}")
        shutil.move(str(temp_zip), str(zip_path))
    except Exception:
        temp_zip.unlink(missing_ok=True)
        raise
    return zip_path


def cleanup_old_jobs(max_keep: int = 10) -> None:
    """古いジョブディレクトリを削除してディスクを解放"""
    try:
        # ディレクトリだけを対象にする。OUTPUTS_DIR 直下には ZIP 生成中の
        # 一時ファイルも置かれるため、glob("*") をそのまま消すと
        # 並行実行中の別ジョブの成果物を壊しうる。
        jobs = sorted((p for p in OUTPUTS_DIR.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime)
        for old_job in jobs[:-max_keep]:
            shutil.rmtree(old_job, ignore_errors=True)
    except Exception as e:
        print(f"[Warning] キャッシュクリーンアップ失敗: {e}")


def deliver_to_drive(job_dir: Path, folder_name: str) -> tuple[str | None, str]:
    """共有ドライブへ納品する。(url, status) を返す。失敗は理由を残す。"""
    try:
        url = upload_report_directory(job_dir, folder_name)
        return url, "uploaded"
    except DriveUploadError as e:
        print(f"[Notice] 共有ドライブ納品に失敗: {e}")
        return None, f"failed: {e}"
    except Exception as e:  # 想定外も理由を残す (握り潰さない)
        traceback.print_exc()
        return None, f"failed: {type(e).__name__}: {e}"


class ScoutRequest(BaseModel):
    area: str
    theme: str
    count: int = 10
    password: str = ""


# --------------------------------------------------------------------------
# エンドポイント
# --------------------------------------------------------------------------
@app.get("/")
async def get_index():
    """スマートフォン最適化 Web UI"""
    return FileResponse(str(BASE_DIR / "templates" / "index.html"), media_type="text/html")


@app.get("/health")
async def health_check():
    """Cloud Run 用ヘルスチェック (認証不要)"""
    return {
        "status": "ok",
        "service": "area-spatial-scout-cloud",
        "version": app.version,
        "auth_configured": bool(SCOUT_PASSWORD),
    }


@app.post("/api/scout/instant")
def scout_instant_endpoint(req: ScoutRequest):
    """調査 → 地図・Word・CSV・ZIP 生成 → 共有ドライブ納品 を同期実行する。"""
    require_auth(req.password)

    area = req.area.strip()
    theme = req.theme.strip()
    count = min(max(req.count, 3), 20)
    if not area or not theme:
        raise HTTPException(status_code=400, detail="エリアとテーマを指定してください")

    job_id = new_job_id()
    job_dir = OUTPUTS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    t_start = datetime.now()

    # 1. 調査。失敗したら架空データで埋めず、そのまま失敗として返す。
    try:
        data = run_autonomous_research(area=area, theme=theme, count=count, output_dir=job_dir)
    except ResearchUnavailable as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(
            status_code=502,
            detail={"message": str(e), "reasons": e.reasons[:5]},
        ) from e

    # 2. 成果物生成
    pack = generate_full_report_pack(data, job_dir)
    folder_name = pack["folder_name"]

    # 3. 構造化JSON保存 (地図生成で付与された緯度経度を含めるため成果物生成の後)
    with open(job_dir / "data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    # 4. ZIP
    create_report_zip(job_dir, folder_name)

    # 5. 共有ドライブ納品
    drive_url, drive_status = deliver_to_drive(job_dir, folder_name)

    cleanup_old_jobs()

    token = download_token(job_id)
    elapsed = (datetime.now() - t_start).total_seconds()
    print(f"[Instant API] 生成完了: {elapsed:.2f}秒 (スポット {len(data.get('spots', []))}件 / Drive {drive_status})")

    result = {
        "status": "success",
        "folder_name": folder_name,
        "drive_url": drive_url,
        "drive_status": drive_status,
        "job_id": job_id,
        "spots_count": len(data.get("spots", [])),
        "requested_count": count,
        "sources_count": len(data.get("meta", {}).get("sources", [])),
        "mapped_count": sum(1 for s in data.get("spots", []) if s.get("geocoded")),
        "elapsed_seconds": round(elapsed, 2),
        "map_url": f"/api/download/{job_id}/map?t={token}",
        "mobile_docx_url": f"/api/download/{job_id}/mobile_docx?t={token}",
        "pc_docx_url": f"/api/download/{job_id}/pc_docx?t={token}",
        "csv_url": f"/api/download/{job_id}/csv?t={token}",
        "zip_url": f"/api/download/{job_id}/zip?t={token}",
    }
    remember_job(job_id, {"status": "completed", "result": result})
    return JSONResponse(result)


@app.get("/api/scout/jobs", dependencies=[Depends(require_auth_header)])
def list_scout_jobs():
    """管理・監視用: 直近ジョブの一覧 (要アクセスキー)"""
    return JSONResponse(dict(JOBS))


@app.get("/api/download/{job_id}/{file_type}")
async def download_file(job_id: str, file_type: str, t: str = Query("")):
    """成果物の配信。job_id 単位の署名付きトークンを必須とする。"""
    if not SCOUT_PASSWORD:
        raise HTTPException(status_code=503, detail="SCOUT_PASSWORD が未設定のためサービスを提供できません")
    # トークン検証を先に行う。ディレクトリ解決を先にすると、404 と 403 の差で
    # 「そのジョブが存在するか」を未認証で言い当てられてしまう。
    if not t or not secrets.compare_digest(t, download_token(job_id)):
        raise HTTPException(status_code=403, detail="このファイルへのアクセス権がありません")
    job_dir = resolve_job_dir(job_id)

    docx_mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if file_type == "map":
        for f in job_dir.glob("*_plot_map.png"):
            return FileResponse(str(f), media_type="image/png", filename=f.name)
    elif file_type == "mobile_docx":
        for f in job_dir.glob("*スマホ閲覧用*.docx"):
            return FileResponse(str(f), media_type=docx_mime, filename=f.name)
    elif file_type == "pc_docx":
        for f in job_dir.glob("*.docx"):
            if "スマホ閲覧用" not in f.name and "area_scout_report" not in f.name:
                return FileResponse(str(f), media_type=docx_mime, filename=f.name)
    elif file_type == "csv":
        for f in job_dir.glob("*.csv"):
            if "spots_ledger" not in f.name:
                return FileResponse(str(f), media_type="text/csv; charset=utf-8", filename=f.name)
    elif file_type == "zip":
        for f in job_dir.glob("*.zip"):
            return FileResponse(str(f), media_type="application/zip", filename=f.name)

    raise HTTPException(status_code=404, detail="対象ファイルが見つかりません")


if SCOUT_DEBUG_ENABLED:

    @app.get("/api/scout/debug", dependencies=[Depends(require_auth_header)])
    def scout_debug_endpoint(area: str = "銀座", theme: str = "鮨"):
        """各処理フェーズの所要時間を計測する診断用 (SCOUT_DEBUG_ENABLED=1 のときのみ)"""
        import time

        timings = {}
        t0 = time.time()
        job_id = new_job_id()
        job_dir = OUTPUTS_DIR / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        timings["01_init"] = round(time.time() - t0, 3)

        t1 = time.time()
        data = run_autonomous_research(area=area, theme=theme, count=3, output_dir=job_dir)
        timings["02_research"] = round(time.time() - t1, 3)

        t2 = time.time()
        pack = generate_full_report_pack(data, job_dir)
        timings["03_report_pack"] = round(time.time() - t2, 3)

        t3 = time.time()
        create_report_zip(job_dir, pack["folder_name"])
        timings["04_zip"] = round(time.time() - t3, 3)

        timings["total_elapsed_seconds"] = round(time.time() - t0, 3)
        return JSONResponse({"status": "ok", "timings": timings, "spots_count": len(data.get("spots", []))})


@app.get("/{full_path:path}")
async def catch_all_fallback(full_path: str):
    """未登録URLはトップページを表示する。ただし API パスは 404 を隠さない。"""
    if full_path.startswith("api/"):
        raise HTTPException(status_code=404, detail="そのAPIは存在しません")
    return FileResponse(str(BASE_DIR / "templates" / "index.html"), media_type="text/html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
