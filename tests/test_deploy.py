import importlib
import sys

from fastapi.testclient import TestClient

from agrisea.config import Settings
from agrisea.store import Store
from agrisea.web.app import create_app


def test_admin_endpoints_open_on_serverless(tmp_path):
    s = Settings(api_key="K", data_dir=tmp_path, serverless=True)
    c = TestClient(create_app(s, Store(":memory:")))
    r = c.post("/api/admin/sample")
    assert r.status_code == 200 and r.json()["utterances"] > 0
    # 배치 본문 처리: 대기 건이 없으면 0
    assert c.post("/api/admin/fetch-minutes?limit=2").json() == {"parsed": 0, "pending": 0, "log": []}
    assert c.get("/api/stats").json()["serverless"]


def test_vercel_entrypoint_uses_tmp_and_seed(tmp_path, monkeypatch):
    """VERCEL 환경변수가 있으면 /tmp 계열 경로를 쓰고, 초기 DB가 있으면 복사한다."""
    import agrisea.config as config
    from agrisea import pipeline

    import gzip
    raw = tmp_path / "seed.sqlite3"
    src = Store(raw)
    pipeline.load_sample(src)
    src.conn.close()
    seed = tmp_path / "seed.sqlite3.gz"
    seed.write_bytes(gzip.compress(raw.read_bytes()))

    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("AGRISEA_DATA_DIR", str(tmp_path / "run"))
    monkeypatch.setattr(config, "SEED_DB", seed)
    monkeypatch.setattr(config, "IS_SERVERLESS", True)
    monkeypatch.setattr(config.Settings, "serverless", True)
    sys.modules.pop("api.index", None)
    mod = importlib.import_module("api.index")
    c = TestClient(mod.app)
    st = c.get("/api/stats").json()
    assert st["storage"] == "seed" and st["meetings"] == 4
    assert (tmp_path / "run" / "agrisea.sqlite3").exists()


def test_health_endpoint(store, settings):
    c = TestClient(create_app(settings, store))
    h = c.get("/api/health").json()
    assert h["ok"] and h["static_index"] and "sqlite" in h and h["fts"] in (True, False)


def test_unwritable_data_dir_falls_back_to_memory(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")  # 디렉터리 자리에 파일 → mkdir 실패
    s = Settings(api_key="", data_dir=blocker / "sub", serverless=True)
    c = TestClient(create_app(s))
    h = c.get("/api/health").json()
    assert not h["ok"] and h["storage"] == "memory" and h["startup_error"]
    assert c.get("/").status_code == 200


def test_export_seed_roundtrip(tmp_path, monkeypatch):
    """초기 데이터 내보내기: 가상 예시 제외, 내용이 같으면 다시 쓰지 않음, 풀어서 열면 검색 동작."""
    import agrisea.config as config
    from agrisea import pipeline

    monkeypatch.setattr(config, "SEED_DB", tmp_path / "data" / "seed.sqlite3.gz")
    monkeypatch.setattr(config, "SEED_META", tmp_path / "data" / "seed.meta.json")
    import agrisea.ontology as ontology
    monkeypatch.setattr(ontology, "KG_PATH", tmp_path / "data" / "kg.nt.gz")
    s = Store(tmp_path / "work.sqlite3")
    pipeline.load_sample(s)
    with s.tx() as c:  # 예시 1건을 실제 수집 데이터처럼 표시
        c.execute("UPDATE meetings SET is_sample=0 WHERE id='SAMPLE-2025-1014'")

    meta = pipeline.export_seed(s)
    assert meta["changed"] and meta["meetings"] == 1
    assert pipeline.export_seed(s)["changed"] is False

    run = Settings(api_key="", data_dir=tmp_path / "run", serverless=True)
    assert pipeline.seed_store(run)
    restored = Store(run.db_path, fts=False)
    assert restored.stats()["meetings"] == 1 and restored.stats()["samples"] == 0
    # 사전 계산 결과(요약·집계·지식그래프)가 함께 실림
    assert restored.summary("SAMPLE-2025-1014", "rule")["key_issues"]
    assert {o["name"] for o in restored.kv_get("orgs") if o["mentions"]} >= {"농림축산식품부"}
    # 브리핑 화면의 주요 기관 버튼은 미리 만든 브리핑을 그대로 쓴다
    import json

    from agrisea.analysis import briefing
    from agrisea.precompute import briefing_key
    assert restored.kv_get(briefing_key("농림축산식품부")) == json.loads(
        json.dumps(briefing(restored, "농림축산식품부"), ensure_ascii=False))
    assert (tmp_path / "data" / "kg.nt.gz").exists() and meta["kg_triples"] > 0
    from agrisea.ontology import kg_from_file
    kg = kg_from_file(tmp_path / "data" / "kg.nt.gz")
    assert len(kg) == meta["kg_triples"]
    assert restored.search("시장격리")
    # 인덱스 없이 만든 DB를 FTS 모드로 열면 인덱스를 다시 채움
    restored.conn.close()
    assert Store(run.db_path, fts=True).search("시장격리")


def test_minutes_text_endpoint_validates_id(store, settings, monkeypatch):
    import agrisea.pipeline as pipeline
    seen = {}

    def fake_download(url, dest, name, http=None):
        seen["url"] = url
        return "◯위원장 홍길동 개의하겠습니다."

    monkeypatch.setattr(pipeline, "download_minutes_text", fake_download)
    c = TestClient(create_app(settings, store))
    r = c.get("/api/minutes-text", params={"id": "52457"})
    assert r.status_code == 200 and "홍길동" in r.text
    assert seen["url"].endswith("pdf.do?id=52457") and "record.assembly.go.kr" in seen["url"]
    assert c.get("/api/minutes-text", params={"id": "http://evil"}).status_code == 422


def test_fetch_minutes_uses_proxy(settings, monkeypatch):
    from agrisea import pipeline
    from agrisea.store import Store

    s = Store(":memory:")
    s.upsert_meeting({"meeting_id": "52457", "title": "제22대 제418회 제4차 농림축산식품해양수산위원회",
                      "date": "2024-10-07", "agendas": [],
                      "pdf_url": "https://record.assembly.go.kr/assembly/viewer/minutes/download/pdf.do?id=52457"})
    calls = []

    class Resp:
        text = "◯위원장 홍길동 개의하겠습니다.\n◯김가람 위원 쌀값 대책은 무엇입니까?"

        def raise_for_status(self):
            pass

    class Http:
        def get(self, url, params=None, timeout=None):
            calls.append((url, params))
            return Resp()

    monkeypatch.setenv("AGRISEA_MINUTES_PROXY", "https://proxy.example/api/minutes-text")
    monkeypatch.setattr(pipeline.requests, "Session", lambda: Http())
    assert pipeline.fetch_minutes(s, settings, progress=lambda m: None) == 1
    assert calls == [("https://proxy.example/api/minutes-text", {"id": "52457"})]
    assert len(s.utterances(meeting_id="52457")) == 2
