"""テスト共通のフィクスチャ。

本番の /tmp を汚さないよう、OUTPUTS_DIR は必ずテスト用一時ディレクトリ配下に閉じ込める。
app.py は SCOUT_PASSWORD / OUTPUTS_DIR をモジュール読み込み時に確定するため、
環境を差し替えたうえで importlib.reload する。
"""

import importlib
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

TEST_PASSWORD = "test-access-key"

SAMPLE_DATA = {
    "meta": {
        "area": "銀座",
        "theme": "個室鮨",
        "scouted_at": "2026-09-20",
        "summary_text": "テスト用の調査概要。",
        "findings": ["所見1", "所見2"],
        "strategic_advice": "1. 【テスト】提言本文",
        "sources": [{"title": "出典サイトA", "uri": "https://example.com/a"}],
        "search_queries": ["銀座 個室 鮨"],
    },
    "spots": [
        {
            "id": "spot_1",
            "name": "テスト鮨 一番",
            "category": "江戸前鮨",
            "rating": "食べログ 3.70",
            "reviews_count": 350,
            "address": "東京都中央区銀座1-1-1",
            "url": "https://example.com/spot1",
            "key_topics": ["個室あり", "駅近"],
            "pricing": "夜: 20,000円〜",
            "popular_times": {"peak_time": "19:00", "quiet_time": "14:00"},
            "reviews": ["静かな個室で接待に使えた。"],
        },
        {
            "id": "spot_2",
            "name": "テスト鮨 二番",
            "category": "江戸前鮨",
            "rating": "",
            "reviews_count": "",
            "address": "東京都中央区銀座2-2-2",
            "url": "",
            "key_topics": [],
            "pricing": "",
            "popular_times": {},
            "reviews": [],
        },
    ],
}


def _reload_app(monkeypatch, tmp_path, password):
    """環境変数を固定したうえで app モジュールを読み直す"""
    monkeypatch.setenv("SCOUT_PASSWORD", password)
    monkeypatch.delenv("SCOUT_DEBUG_ENABLED", raising=False)
    monkeypatch.delenv("DRIVE_PARENT_FOLDER_ID", raising=False)
    # tempfile.gettempdir() は初回呼び出し結果をキャッシュするため直接差し替える
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    import app as app_module

    importlib.reload(app_module)
    assert str(app_module.OUTPUTS_DIR).startswith(str(tmp_path)), "テストが本番の一時領域を使おうとしている"
    return app_module


@pytest.fixture
def app_module(monkeypatch, tmp_path):
    return _reload_app(monkeypatch, tmp_path, TEST_PASSWORD)


@pytest.fixture
def client(app_module):
    from fastapi.testclient import TestClient

    return TestClient(app_module.app)


@pytest.fixture
def unconfigured_client(monkeypatch, tmp_path):
    """SCOUT_PASSWORD が未設定の状態 (fail-closed の検証用)"""
    from fastapi.testclient import TestClient

    return TestClient(_reload_app(monkeypatch, tmp_path, "").app)


@pytest.fixture
def stub_research(app_module, monkeypatch):
    """調査結果を固定値に差し替える (ネットワーク・API キー不要にする)"""
    import copy

    def _fake(area, theme, count=10, output_dir=None, **kwargs):
        return copy.deepcopy(SAMPLE_DATA)

    monkeypatch.setattr(app_module, "run_autonomous_research", _fake)
    return app_module
