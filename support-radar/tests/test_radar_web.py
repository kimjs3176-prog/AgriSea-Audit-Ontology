import importlib.util
import sys

import pytest
from fastapi.testclient import TestClient

from radar import config
from radar.catalog import Catalog
from radar.config import Settings
from radar.web.app import create_app


@pytest.fixture()
def client(anns, tmp_path):
    cat = Catalog([dict(a) for a in anns])
    return TestClient(create_app(Settings(api_key="", data_dir=tmp_path), cat))


def test_index_health_meta(client):
    assert client.get("/").status_code == 200
    h = client.get("/api/health").json()
    assert h["ok"] and h["announcements"] == 4 and not h["api_key"]
    m = client.get("/api/meta").json()
    assert m["open"] == 3 and "서울" in m["regions"] and not m["can_refresh"]


def test_match_api(client):
    r = client.post("/api/match", json={"kind": "company", "founded": "", "age": "33", "keywords": "스마트팜"}).json()
    assert r["results"][0]["id"] in ("A1", "A4") and "summary" in r


def test_announcements_and_detail(client):
    d = client.get("/api/announcements").json()
    assert d["total"] == 3
    assert client.get("/api/announcements?state=closed").json()["items"][0]["id"] == "A3"
    assert client.get("/api/announcements?topic=smartfarm").json()["total"] == 2
    assert client.get("/api/announcements?region=서울").json()["total"] == 3
    a = client.get("/api/announcements/A1?kind=company&age=45").json()
    assert a["evaluation"]["verdict"] == "ineligible" and a["topics"] == ["smartfarm"]
    assert client.get("/api/announcements/nope").status_code == 404


def test_radar_api(client):
    assert len(client.get("/api/radar").json()["topics"]) > 5
    assert client.get("/api/radar/smartfarm").json()["open"] == 2
    assert client.get("/api/radar/nope").status_code == 404


def test_refresh_needs_key_and_proxy_guard(client):
    assert client.post("/api/admin/refresh").status_code == 400
    assert client.get("/api/kstartup/evil").status_code == 404
    assert client.get("/api/kstartup/getAnnouncementInformation01").status_code == 400


def test_refresh_merges_and_cools_down(anns, tmp_path, monkeypatch):
    from radar import kstartup
    new = {**anns[0], "id": "N1", "title": "[테스트] 새 공고"}
    monkeypatch.setattr(kstartup.KStartupClient, "announcements", lambda self, **kw: [new])
    cat = Catalog([dict(a) for a in anns])
    c = TestClient(create_app(Settings(api_key="K", data_dir=tmp_path), cat))
    r = c.post("/api/admin/refresh").json()
    assert r["added"] == 1 and (tmp_path / "catalog.json.gz").exists()
    assert c.post("/api/admin/refresh").status_code == 429
    assert Catalog.read(tmp_path / "catalog.json.gz").by_id["N1"]["title"] == "[테스트] 새 공고"


def test_vercel_entrypoint(anns, tmp_path, monkeypatch):
    seed = tmp_path / "seed.json.gz"
    Catalog([dict(a) for a in anns], meta={"updated_at": "2026-01-01T00:00:00+00:00"}).write(seed)
    monkeypatch.setattr(config, "SEED_PATH", seed)
    monkeypatch.setattr(config, "SIGNALS_PATH", tmp_path / "none.json")
    monkeypatch.setenv("RADAR_DATA_DIR", str(tmp_path / "run"))
    # 루트 서비스의 api/index.py 와 이름이 겹치지 않게 파일 경로로 불러오고,
    # 진입점이 sys.path 맨 앞에 넣는 서비스 폴더는 테스트가 끝나면 되돌린다
    monkeypatch.setattr(sys, "path", list(sys.path))
    spec = importlib.util.spec_from_file_location("radar_vercel_entry", config.ROOT / "api" / "index.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    h = TestClient(mod.app).get("/api/health").json()
    assert h["ok"] and h["announcements"] == 4 and not h["signals"]
