#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
app.py
Area Spatial Scout - Cloud Run サーバーアプリケーション (FastAPI)
スマホからリクエストを受け、自律リサーチ ➔ 地図合成 ➔ Word生成 ➔ Google ドライブ保存を完全自動実行。
"""

import os
import re
import json
import uuid
import shutil
import tempfile
import traceback
from datetime import datetime
from pathlib import Path
from pydantic import BaseModel
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse

from research_engine import run_autonomous_research
from report_engine import generate_full_report_pack
from drive_uploader import upload_report_directory

app = FastAPI(title="Area Spatial Scout Cloud", version="2.1.0")

BASE_DIR = Path(__file__).resolve().parent

# セキュリティパスワード（環境変数 SCOUT_PASSWORD、未設定時は認証フリー）
SCOUT_PASSWORD = os.environ.get("SCOUT_PASSWORD", "")

# 成果物キャッシュディレクトリ (/tmp/scout_outputs)
OUTPUTS_DIR = Path(tempfile.gettempdir()) / "scout_outputs"
OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)


def cleanup_old_jobs(max_keep: int = 10):
    """古いジョブディレクトリを安全に削除してディスクを解放"""
    try:
        jobs = sorted(list(OUTPUTS_DIR.glob("*")), key=lambda p: p.stat().st_mtime)
        if len(jobs) > max_keep:
            for old_job in jobs[:-max_keep]:
                shutil.rmtree(old_job, ignore_errors=True)
    except Exception as e:
        print(f"[Warning] キャッシュクリーンアップ失敗: {e}")


class ScoutRequest(BaseModel):
    area: str
    theme: str
    count: int = 10
    password: str = ""


@app.get("/")
async def get_index():
    """スマートフォン最適化 Web UI"""
    html_file = BASE_DIR / "templates" / "index.html"
    return FileResponse(str(html_file), media_type="text/html")


@app.get("/health")
async def health_check():
    """Cloud Run 用ヘルスチェック"""
    return {"status": "ok", "service": "area-spatial-scout-cloud"}


JOBS = {}


def background_scout_task(job_id: str, area: str, theme: str, count: int, job_dir: Path):
    """別スレッドで安全に実行される非同期リサーチ・レポート生成ワーカー"""
    try:
        JOBS[job_id] = {
            "status": "processing",
            "progress": 25,
            "step": "AI検索グラウンディング中...",
            "detail": "Gemini 3.6 Flashが最新の口コミ・住所・営業情報をリサーチ中"
        }
        data = run_autonomous_research(area=area, theme=theme, count=count, output_dir=job_dir)

        # 構造化JSONを保存
        json_path = job_dir / "data.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        JOBS[job_id] = {
            "status": "processing",
            "progress": 65,
            "step": "地図合成＆デュアルWord生成中...",
            "detail": "国土地理院プロット地図とスマホ専用Wordを作成中"
        }
        pack = generate_full_report_pack(data, job_dir)
        folder_name = pack["folder_name"]

        # 全成果物の一括ZIPアーカイブ生成
        zip_base = job_dir / f"{folder_name}_一括納品パック"
        shutil.make_archive(str(zip_base), "zip", root_dir=job_dir)

        JOBS[job_id] = {
            "status": "processing",
            "progress": 90,
            "step": "成果物を保存中...",
            "detail": "ダウンロードリンクとGoogleドライブ保存を処理中"
        }
        drive_url = None
        try:
            drive_url = upload_report_directory(job_dir, target_folder_name=folder_name)
        except Exception as drive_err:
            print(f"[Notice] Google Drive への同期をスキップ: {drive_err}")

        cleanup_old_jobs()

        JOBS[job_id] = {
            "status": "completed",
            "progress": 100,
            "step": "レポート生成完了！",
            "detail": "すべての成果物の準備が整いました",
            "result": {
                "status": "success",
                "folder_name": folder_name,
                "drive_url": drive_url,
                "job_id": job_id,
                "spots_count": len(data.get("spots", [])),
                "map_url": f"/api/download/{job_id}/map",
                "mobile_docx_url": f"/api/download/{job_id}/mobile_docx",
                "pc_docx_url": f"/api/download/{job_id}/pc_docx",
                "csv_url": f"/api/download/{job_id}/csv",
                "zip_url": f"/api/download/{job_id}/zip"
            }
        }
    except Exception as e:
        traceback.print_exc()
        JOBS[job_id] = {
            "status": "failed",
            "error": str(e)
        }


@app.post("/api/scout")
def start_scout_job(req: ScoutRequest):
    """
    ジョブを即座（0.05秒）にバックグラウンド起動し、タイムアウトや503を物理遮断するAPI
    """
    if SCOUT_PASSWORD and req.password != SCOUT_PASSWORD:
        raise HTTPException(status_code=401, detail="アクセスキー（パスワード）が正しくありません")

    area = req.area.strip()
    theme = req.theme.strip()
    count = min(max(req.count, 3), 20)

    if not area or not theme:
        raise HTTPException(status_code=400, detail="エリアとテーマを指定してください")

    job_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    job_dir = OUTPUTS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    JOBS[job_id] = {
        "status": "processing",
        "progress": 10,
        "step": "リサーチを開始します...",
        "detail": "クラウド自律エンジン起動中"
    }

    import threading
    worker = threading.Thread(
        target=background_scout_task,
        args=(job_id, area, theme, count, job_dir),
        daemon=True
    )
    worker.start()

    return JSONResponse({
        "status": "started",
        "job_id": job_id
    })


@app.get("/api/scout/status/{job_id}")
def get_scout_job_status(job_id: str):
    """スマホ側から2秒おきに進捗を取得するステータスAPI"""
    if job_id not in JOBS:
        raise HTTPException(status_code=404, detail="指定された調査ジョブが見つかりません")
    return JSONResponse(JOBS[job_id])


@app.get("/api/download/{job_id}/{file_type}")
async def download_file(job_id: str, file_type: str):
    """生成成果物のスマホ／PC直接ダウンロード配信"""
    job_dir = OUTPUTS_DIR / job_id
    if not job_dir.exists():
        raise HTTPException(status_code=404, detail="ファイルが見つかりません（有効期限切れの可能性があります）")

    if file_type == "map":
        for f in job_dir.glob("*_plot_map.png"):
            return FileResponse(str(f), media_type="image/png", filename=f.name)
    elif file_type == "mobile_docx":
        for f in job_dir.glob("*スマホ閲覧用*.docx"):
            return FileResponse(
                str(f),
                media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                filename=f.name
            )
    elif file_type == "pc_docx":
        for f in job_dir.glob("*.docx"):
            if "スマホ閲覧用" not in f.name and "area_scout_report" not in f.name:
                return FileResponse(
                    str(f),
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    filename=f.name
                )
    elif file_type == "csv":
        for f in job_dir.glob("*.csv"):
            if "spots_ledger" not in f.name:
                return FileResponse(str(f), media_type="text/csv; charset=utf-8", filename=f.name)
    elif file_type == "zip":
        for f in job_dir.glob("*.zip"):
            return FileResponse(str(f), media_type="application/zip", filename=f.name)

    raise HTTPException(status_code=404, detail="対象ファイルが見つかりません")


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=True)
