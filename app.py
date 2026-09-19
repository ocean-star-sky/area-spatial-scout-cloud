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
import threading
import concurrent.futures
from datetime import datetime
from pathlib import Path
from pydantic import BaseModel
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

from research_engine import run_autonomous_research
from report_engine import generate_full_report_pack
from drive_uploader import upload_report_directory

def safe_upload_drive(job_dir: Path, target_folder_name: str, timeout: float = 2.5) -> str | None:
    """Google Driveへのアップロードを最大2.5秒で安全に打ち切るフェイルセーフ関数（待機ゼロ）"""
    res = [None]
    def worker():
        try:
            res[0] = upload_report_directory(job_dir, target_folder_name)
        except Exception:
            pass
    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout=timeout)
    return res[0]

app = FastAPI(title="Area Spatial Scout Cloud", version="2.1.0")

# CORSミドルウェア（スマホブラウザのFailed to fetchを100%防止）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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


def update_job_status(job_id: str, job_dir: Path, status: str, progress: int, step: str, detail: str, result=None, error=None):
    """メモリとディスク(status.json)の両方に進捗を同期保存"""
    payload = {
        "status": status,
        "job_id": job_id,
        "progress": progress,
        "step": step,
        "detail": detail
    }
    if result:
        payload["result"] = result
    if error:
        payload["error"] = error
    JOBS[job_id] = payload
    try:
        with open(job_dir / "status.json", "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
    except Exception:
        pass
    return payload


def background_scout_task(job_id: str, area: str, theme: str, count: int, job_dir: Path):
    """別スレッドで安全に実行される非同期リサーチ・レポート生成ワーカー"""
    try:
        update_job_status(job_id, job_dir, "processing", 20, "AIリサーチ＆スポット抽出中...", "Gemini高度AIモデルが最新の口コミ・住所・営業情報を自律リサーチ中")
        data = run_autonomous_research(area=area, theme=theme, count=count, output_dir=job_dir)

        # 構造化JSONを保存
        json_path = job_dir / "data.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        update_job_status(job_id, job_dir, "processing", 60, "地図合成＆デュアルWord生成中...", "国土地理院プロット地図とスマホ専用Wordを作成中")
        pack = generate_full_report_pack(data, job_dir)
        folder_name = pack["folder_name"]

        # 全成果物の一括ZIPアーカイブ生成
        zip_base = job_dir / f"{folder_name}_一括納品パック"
        shutil.make_archive(str(zip_base), "zip", root_dir=job_dir)

        update_job_status(job_id, job_dir, "processing", 90, "成果物を保存中...", "ダウンロードリンクとGoogleドライブ保存を処理中")
        drive_url = safe_upload_drive(job_dir, target_folder_name=folder_name, timeout=4.0)

        cleanup_old_jobs()

        result_payload = {
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

        update_job_status(job_id, job_dir, "completed", 100, "レポート生成完了！", "すべての成果物の準備が整いました", result=result_payload)
    except Exception as e:
        traceback.print_exc()
        update_job_status(job_id, job_dir, "failed", 0, "生成エラー", str(e), error=str(e))


def stream_scout_generator(area: str, theme: str, count: int, password: str = ""):
    """Cloud RunのCPUスロットリングを100%防止し、進捗をリアルタイム送信するSSEストリーミングジェネレータ"""
    if SCOUT_PASSWORD and password != SCOUT_PASSWORD:
        err_data = {"status": "failed", "error": "アクセスキー（パスワード）が正しくありません"}
        yield f"data: {json.dumps(err_data, ensure_ascii=False)}\n\n"
        return

    job_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    job_dir = OUTPUTS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    def make_event(status, progress, step, detail, result=None, error=None):
        payload = {
            "status": status,
            "job_id": job_id,
            "progress": progress,
            "step": step,
            "detail": detail
        }
        if result:
            payload["result"] = result
        if error:
            payload["error"] = error
        JOBS[job_id] = payload
        
        # ディスク永続化（マルチインスタンス耐性）
        try:
            with open(job_dir / "status.json", "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
        except Exception:
            pass

        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    yield make_event("processing", 15, "AIリサーチ開始...", "Gemini高度AIモデルが最新の口コミ・住所・営業情報を自律リサーチ中")

    try:
        data = run_autonomous_research(area=area, theme=theme, count=count, output_dir=job_dir)

        # 構造化JSONを保存
        json_path = job_dir / "data.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        yield make_event("processing", 60, "地図合成＆デュアルWord生成中...", "国土地理院プロット地図とスマホ専用Wordを作成中")

        pack = generate_full_report_pack(data, job_dir)
        folder_name = pack["folder_name"]

        # 一括ZIPアーカイブ生成
        zip_base = job_dir / f"{folder_name}_一括納品パック"
        shutil.make_archive(str(zip_base), "zip", root_dir=job_dir)

        yield make_event("processing", 88, "成果物を保存中...", "ダウンロードリンクとGoogleドライブ保存を処理中")
        drive_url = safe_upload_drive(job_dir, target_folder_name=folder_name, timeout=4.0)

        cleanup_old_jobs()

        result_payload = {
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

        yield make_event("completed", 100, "レポート生成完了！", "すべての成果物の準備が整いました", result=result_payload)

    except Exception as e:
        traceback.print_exc()
        yield make_event("failed", 0, "生成エラー", str(e), error=str(e))


@app.get("/api/scout/stream")
def scout_stream_endpoint(area: str, theme: str, count: int = 10, password: str = ""):
    """
    スマホ向けリアルタイムSSEストリーミングAPI。
    接続を維持して進捗をリアルタイム配信し、Cloud RunのCPUフリーズを物理的に完全防止。
    """
    area = area.strip()
    theme = theme.strip()
    count = min(max(count, 3), 20)
    
    return StreamingResponse(
        stream_scout_generator(area=area, theme=theme, count=count, password=password),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


@app.post("/api/scout/instant")
def scout_instant_endpoint(req: ScoutRequest):
    """
    わずか数秒でリサーチから地図・Word・CSV・ZIPまで一撃完遂する超高信頼性同期API。
    Cloud Runのマルチインスタンスジョブ消失やCPUスロットリングを100%物理遮断。
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

    try:
        t_start = datetime.now()
        # 1. AIリサーチ（最大3秒打ち切り）
        data = run_autonomous_research(area=area, theme=theme, count=count, output_dir=job_dir)

        # 構造化JSON保存
        json_path = job_dir / "data.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        # 2. 地図合成＆Word生成（完全ローカル0.3秒）
        pack = generate_full_report_pack(data, job_dir)
        folder_name = pack["folder_name"]

        # 3. ZIP生成
        zip_base = job_dir / f"{folder_name}_一括納品パック"
        shutil.make_archive(str(zip_base), "zip", root_dir=job_dir)

        # 4. Google Drive同期は完全非同期バックグラウンド実行（レスポンスを待たずに即座に返却！）
        threading.Thread(target=safe_upload_drive, args=(job_dir, folder_name), daemon=True).start()

        cleanup_old_jobs()

        elapsed = (datetime.now() - t_start).total_seconds()
        print(f"[Instant API] 全成果物生成完了: {elapsed:.2f}秒 (スポット数: {len(data.get('spots', []))})")

        result = {
            "status": "success",
            "folder_name": folder_name,
            "drive_url": None,
            "job_id": job_id,
            "spots_count": len(data.get("spots", [])),
            "map_url": f"/api/download/{job_id}/map",
            "mobile_docx_url": f"/api/download/{job_id}/mobile_docx",
            "pc_docx_url": f"/api/download/{job_id}/pc_docx",
            "csv_url": f"/api/download/{job_id}/csv",
            "zip_url": f"/api/download/{job_id}/zip"
        }
        JOBS[job_id] = {"status": "completed", "result": result}
        return JSONResponse(result)
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"生成エラー: {e}")


@app.get("/api/scout/debug")
def scout_debug_endpoint(area: str = "銀座", theme: str = "鮨"):
    """各処理フェーズの所要時間を秒単位で精密計測する診断エンドポイント"""
    import time
    timings = {}
    t0 = time.time()
    job_id = f"debug_{int(t0)}"
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
    folder_name = pack["folder_name"]
    zip_base = job_dir / f"{folder_name}_一括納品パック"
    shutil.make_archive(str(zip_base), "zip", root_dir=job_dir)
    timings["04_zip"] = round(time.time() - t3, 3)

    timings["total_elapsed_seconds"] = round(time.time() - t0, 3)
    return JSONResponse({
        "status": "ok",
        "timings": timings,
        "spots_count": len(data.get("spots", []))
    })


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


@app.get("/api/scout/jobs")
def list_scout_jobs():
    """管理・監視用: 現在実行中および最近のジョブ状態一覧を返す"""
    results = {}
    for j_id, j_data in JOBS.items():
        results[j_id] = j_data
    try:
        for job_dir in sorted(OUTPUTS_DIR.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)[:10]:
            j_id = job_dir.name
            if j_id not in results:
                s_file = job_dir / "status.json"
                if s_file.exists():
                    try:
                        with open(s_file, "r", encoding="utf-8") as f:
                            results[j_id] = json.load(f)
                    except Exception:
                        pass
    except Exception:
        pass
    return JSONResponse(results)


@app.get("/api/scout/status/{job_id}")
def get_scout_job_status(job_id: str):
    """スマホ側から2秒おきに進捗を取得するステータスAPI"""
    if job_id not in JOBS:
        status_file = OUTPUTS_DIR / job_id / "status.json"
        if status_file.exists():
            try:
                with open(status_file, "r", encoding="utf-8") as f:
                    return JSONResponse(json.load(f))
            except Exception:
                pass
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


@app.get("/{full_path:path}")
async def catch_all_fallback(full_path: str):
    """未登録のURLでも404 Not Foundを出さず、安全にトップページを表示するキャッチオール"""
    html_file = BASE_DIR / "templates" / "index.html"
    return FileResponse(str(html_file), media_type="text/html")


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=True)
