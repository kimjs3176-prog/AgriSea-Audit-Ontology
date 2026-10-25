import base64
import json

import pytest
from fastapi.testclient import TestClient

from agrisea import governance as gov
from agrisea.config import Settings
from agrisea.web.app import create_app
from test_followup import _tracked_store


# ── 원장 ────────────────────────────────────────────────
def test_hash_chain_detects_tampering(tmp_path):
    led = gov.Ledger(gov.FileSink(tmp_path / "l.jsonl"))
    for i in range(3):
        led.append("decision", "검토자", "reviewer", {"action": "approve", "n": i}, case=f"followup:{i}")
    events = gov.FileSink(tmp_path / "l.jsonl").load()  # 다시 읽어도 그대로
    assert gov.verify_chain(events)["ok"] and len(events) == 3 and events[1]["prev"] == events[0]["hash"]
    events[1]["payload"]["action"] = "reject"  # 결정 내용 바꿔치기
    assert gov.verify_chain(events) == {"ok": False, "events": 3, "broken_at": 1}
    assert gov.verify_chain(events[:1] + events[2:])["ok"] is False  # 중간 삭제


class FakeGitHub:
    """contents API 흉내: 브랜치·파일·sha, 그리고 다른 인스턴스가 끼어드는 충돌."""

    def __init__(self):
        self.branches, self.file, self.sha, self.interfere = {"main"}, None, None, False
        self.puts = 0

    class R:
        def __init__(self, code, data=None):
            self.status_code, self._d = code, data

        def json(self):
            return self._d

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(self.status_code)

    def get(self, url, params=None, headers=None, timeout=None):
        if "/contents/" in url:
            if self.file is None:
                return self.R(404)
            return self.R(200, {"sha": self.sha, "size": len(self.file),
                                "content": base64.b64encode(self.file.encode()).decode()})
        if "/branches/" in url:
            return self.R(200 if url.rsplit("/", 1)[-1] in self.branches else 404, {})
        if url.endswith("/git/ref/heads/main"):
            return self.R(200, {"object": {"sha": "abc"}})
        return self.R(200, {"default_branch": "main"})

    def post(self, url, headers=None, json=None, timeout=None):
        self.branches.add(json["ref"].rsplit("/", 1)[-1])
        return self.R(201, {})

    def put(self, url, headers=None, json=None, timeout=None):
        self.puts += 1
        if json.get("sha") != self.sha:
            return self.R(409)
        if self.interfere:  # 이 쓰기 직전에 다른 인스턴스가 한 줄을 먼저 씀
            self.interfere = False
            other = gov.MemorySink(self._events())
            other.commit(lambda ev: {**{"seq": len(ev), "ts": "t", "kind": "decision", "actor": "다른 인스턴스",
                                        "role": "reviewer", "case": "c", "payload": {}, "prev": ev[-1]["hash"] if ev else ""}})
            evs = other.load()
            evs[-1]["hash"] = gov.event_hash(evs[-1])
            self._write(evs)
            return self.R(409)
        self.file = base64.b64decode(json["content"]).decode()
        self.sha = f"sha{self.puts}"
        return self.R(200, {})

    def _events(self):
        return [__import__("json").loads(x) for x in (self.file or "").splitlines() if x]

    def _write(self, evs):
        self.file = "".join(gov._canon(e) + "\n" for e in evs)
        self.sha = f"sha-other{self.puts}"


def test_github_sink_creates_branch_and_retries_on_conflict():
    gh = FakeGitHub()
    led = gov.Ledger(gov.GitHubSink("o/r", "tok", http=gh))
    led.append("run", "AI 판정", "system", {"cases": 1})
    assert "governance-log" in gh.branches and gov.verify_chain(gh._events())["ok"]
    gh.interfere = True
    ev = led.append("decision", "검토자", "reviewer", {"action": "approve"}, case="x")
    events = gh._events()
    # 다른 인스턴스의 기록 뒤에 이어 붙고, 연결이 깨지지 않는다
    assert [e["actor"] for e in events] == ["AI 판정", "다른 인스턴스", "검토자"]
    assert ev["seq"] == 2 and gov.verify_chain(events)["ok"]


# ── 역할·위험·경로 ─────────────────────────────────────────
def test_roles():
    r = gov.Roles("홍검토:r1", "박관리:a1")
    assert r.who("r1") == {"name": "홍검토", "role": "reviewer", "open": False}
    assert r.who("a1")["role"] == "admin" and r.who("nope")["role"] == "viewer" and r.who(None)["role"] == "viewer"
    assert gov.Roles(open_mode=True).who(None)["role"] == "admin"  # 토큰 없는 로컬 개발
    assert not gov.Roles("홍검토:r1", open_mode=True).open_mode       # 토큰을 두면 개방하지 않음


def test_risk_and_routing():
    pol = dict(gov.DEFAULT_POLICY)
    done_law = {"id": 1, "status": "done", "status_label": "이행 보고", "action": "law",
                "evidence": [{"source": "minutes", "kind": "done", "score": 2, "date": "2025-01-01"}]}
    r = gov.assess(done_law, pol)
    assert r["tier"] == "high" and {f["code"] for f in r["factors"]} >= {"status", "action", "single", "weak"}
    assert gov.route("followup:1", r, pol) == "escalated"
    quiet = {"id": 2, "status": "none", "action": "review", "evidence": []}
    assert gov.assess(quiet, pol)["tier"] == "low"
    routes = {gov.route(f"followup:{i}", gov.assess(quiet, pol), pol) for i in range(200)}
    assert routes == {"auto", "sampled"}  # 저위험 대부분 자동, 일부 표본
    assert gov.route("followup:7", gov.assess(quiet, pol), {**pol, "sample_rate": 1.0}) == "sampled"
    assert gov.route("followup:7", gov.assess(quiet, pol), pol) == gov.route("followup:7", gov.assess(quiet, pol), pol)


def test_proposals_from_repeated_keyword_rejections():
    items = [{"id": i, "status": "done", "evidence": [], "keywords": ["시스템", f"k{i}"]} for i in range(3)]
    events = []
    led = gov.Ledger(gov.MemorySink())
    for x in items:
        led.append("decision", "검토자", "reviewer", {
            "action": "reject", "ai_status": "done", "final_status": "none", "reason": "keyword",
            "keywords": ["시스템"], "fingerprint": gov.fingerprint(x), "path": "escalated", "policy_version": 1},
            case=gov.case_id(x))
    events = led.events()
    ps = gov.proposals(items, events, gov.active_policy(events))
    assert [p["type"] for p in ps] == ["keyword_block"] and ps[0]["value"] == "시스템" and len(ps[0]["support"]) == 3
    new = gov.adopt(gov.DEFAULT_POLICY, ps[0])
    assert new["version"] == 2 and new["keyword_block"] == ["시스템"]
    led.append("policy", "관리자", "admin", {"policy": new, "proposal": ps[0]["id"], "change": ps[0]["title"]})
    events = led.events()
    assert gov.active_policy(events)["version"] == 2 and gov.proposals(items, events, gov.active_policy(events)) == []


# ── 관문 전 과정(웹) ───────────────────────────────────────
@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AGRISEA_REVIEWERS", "홍검토:r1,김검토:r2")
    monkeypatch.setenv("AGRISEA_ADMINS", "박관리:a1")
    monkeypatch.delenv("AGRISEA_GOV_GITHUB_REPO", raising=False)
    s = _tracked_store(tmp_path)
    return TestClient(create_app(Settings(api_key="K", data_dir=tmp_path / "run"), s))


H = lambda tok: {"X-Agrisea-Token": tok}


def fp_of(c, cid):
    return c.get(f"/api/governance/cases/{cid}").json()["gov"]["fingerprint"]


def test_gate_flow_review_rollback_evaluation_institution(client, monkeypatch):
    c = client
    st = c.get("/api/governance/status").json()
    assert st["roles"] == {"configured": True, "open_mode": False} and st["me"]["role"] == "viewer"
    ov = c.get("/api/governance/overview").json()
    assert ov["metrics"]["total"] == 2 and ov["metrics"]["runs"] == 1  # G1: AI 판정 실행 기록
    q = c.get("/api/governance/queue").json()
    assert q["count"] == 2 and all(x["gov"]["path"] == "escalated" for x in q["items"])  # G3: 고위험 → 숙련자
    hen = next(x for x in q["items"] if x["ai_status"] == "done")
    manure = next(x for x in q["items"] if x["ai_status"] == "reraised")
    case = c.get(f"/api/governance/cases/{manure['id']}").json()
    fp = case["gov"]["fingerprint"]

    # 권한: 열람자는 결정 불가, 사유 없는 거부 불가
    assert c.post(f"/api/governance/cases/{manure['id']}/decision", json={"action": "approve"}).status_code == 403
    bad = c.post(f"/api/governance/cases/{manure['id']}/decision", headers=H("r1"),
                 json={"action": "reject", "final_status": "none"})
    assert bad.status_code == 400
    assert c.post(f"/api/governance/cases/{manure['id']}/decision", headers=H("r1"),
                  json={"action": "approve", "fingerprint": "old"}).status_code == 409  # 바뀐 판정에 대한 결정 막기

    # G4: 거부(정정) — 공개 판정이 숙련자 판정을 따른다
    r = c.post(f"/api/governance/cases/{manure['id']}/decision", headers=H("r1"), json={
        "action": "reject", "final_status": "none", "reason": "keyword", "keywords": ["퇴비화"], "fingerprint": fp})
    assert r.status_code == 200 and r.json()["case"]["gov"]["state"] == "corrected"
    row = next(x for x in c.get("/api/followups").json()["items"] if x["id"] == manure["id"])
    assert row["status"] == "none" and row["ai_status"] == "reraised" and row["gov"]["state"] == "corrected"
    from agrisea.followup import STATUS
    assert c.get(f"/api/followups/{manure['id']}").json()["status_desc"] == STATUS["none"][1]  # 설명도 정정 판정 기준
    assert c.post(f"/api/governance/cases/{manure['id']}/decision", headers=H("r1"),
                  json={"action": "approve", "fingerprint": fp}).status_code == 409  # 이미 결정됨
    assert c.post(f"/api/governance/cases/{hen['id']}/decision", headers=H("r2"),
                  json={"action": "approve"}).status_code == 400  # 검토한 판정의 지문 없이 결정 불가
    hen_fp = fp_of(c, hen["id"])
    c.post(f"/api/governance/cases/{hen['id']}/decision", headers=H("r2"), json={"action": "approve", "fingerprint": hen_fp})

    # G5: 롤백 — 남의 결정은 관리자만, 사유 필수
    assert c.post(f"/api/governance/cases/{hen['id']}/rollback", headers=H("r1"), json={"reason": "x"}).status_code == 403
    assert c.post(f"/api/governance/cases/{hen['id']}/rollback", headers=H("a1"), json={}).status_code == 400
    rb = c.post(f"/api/governance/cases/{hen['id']}/rollback", headers=H("a1"), json={"reason": "근거 재확인"})
    assert rb.json()["case"]["gov"]["state"] == "pending_review"
    c.post(f"/api/governance/cases/{hen['id']}/decision", headers=H("r2"), json={"action": "approve", "fingerprint": hen_fp})

    # G6: 평가 데이터
    m = c.get("/api/governance/metrics").json()
    assert m["reviewed"] == 2 and m["agreement"]["rate"] == 0.5 and m["rollbacks"] == 1
    assert m["confusion"] == {"reraised": {"none": 1}, "done": {"done": 1}} and m["reasons"] == {"keyword": 1}
    assert m["exceptions"] == {"rolled_back": 1}
    rows = [json.loads(x) for x in c.get("/api/governance/dataset.jsonl").text.strip().splitlines()]
    # 롤백된 결정도 예외 기록으로 남는다(사유 포함), 지표에는 유효 결정만
    assert [r["outcome"] for r in rows] == ["active", "rolled_back", "active"]
    assert rows[1]["rollback_reason"] == "근거 재확인" and {r["agree"] for r in rows} == {True, False}
    csv = c.get("/api/governance/dataset.csv").text
    assert csv.startswith("﻿case,") and "퇴비화" in csv

    # G7: 제도 반영 — 거부 근거가 쌓이면 개정안 → 관리자 채택 → 새 정책으로 재판정
    monkeypatch.setattr(gov, "MIN_SUPPORT", 1)
    props = c.get("/api/governance/proposals").json()
    p = next(x for x in props["proposals"] if x["type"] == "keyword_block")
    assert p["value"] == "퇴비화"
    assert c.post(f"/api/governance/proposals/{p['id']}/adopt", headers=H("r1"), json={}).status_code == 403
    ad = c.post(f"/api/governance/proposals/{p['id']}/adopt", headers=H("a1"), json={"note": "검토 반영"})
    assert ad.json()["policy"] == {**gov.DEFAULT_POLICY, "version": 2, "keyword_block": ["퇴비화"], "note": p["title"]}
    fu = c.get("/api/followups").json()
    # 차단된 대상어를 빼면 그 약속은 대상이 드러나지 않아 추적에서 빠진다(정책 효과)
    assert fu["policy_version"] == 2 and fu["total"] == 1 and fu["vague"] == 1
    assert c.get(f"/api/followups/{manure['id']}").status_code == 404
    m = c.get("/api/governance/metrics").json()  # 판정 대상에서 빠진 건의 결정은 일치율에서 뺀다
    assert m["reviewed"] == 1 and m["removed"] == 1 and m["agreement"]["n"] == 1
    runs = c.get("/api/governance/ledger", params={"kind": "run"}).json()["events"]
    assert runs[0]["payload"]["policy_version"] == 2 and runs[0]["payload"]["trigger"] == "정책 적용 재판정"

    # 모든 관문의 증거: 원장 연결 정상, 보고서에 관문별 기록
    led = c.get("/api/governance/ledger").json()
    assert led["chain"]["ok"] and {e["kind"] for e in led["events"]} == {"run", "decision", "rollback", "policy"}
    rep = c.get("/api/governance/report.md").text
    for gate in ("G1 접수", "G2 위험평가", "G4 숙련자 검토", "G6 평가 데이터", "G7 제도 반영", "v2"):
        assert gate in rep


def test_governance_cli_records_run(tmp_path, monkeypatch, settings):
    from agrisea.__main__ import main
    from agrisea.pipeline import load_sample
    from agrisea.store import Store
    monkeypatch.setenv("AGRISEA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AGRISEA_GOVERNANCE_LEDGER", str(tmp_path / "gov.jsonl"))
    monkeypatch.delenv("AGRISEA_GOV_GITHUB_REPO", raising=False)
    s = Store(tmp_path / "agrisea.sqlite3")
    load_sample(s)
    s.conn.close()
    assert main(["governance-run", "--trigger", "테스트"]) == 0
    events = gov.FileSink(tmp_path / "gov.jsonl").load()
    assert events[-1]["kind"] == "run" and events[-1]["payload"]["trigger"] == "테스트"
    assert sum(events[-1]["payload"]["paths"].values()) == events[-1]["payload"]["cases"]
    assert main(["governance-run"]) == 0  # 같은 판정 결과·정책이면 다시 기록하지 않음
    assert len(gov.FileSink(tmp_path / "gov.jsonl").load()) == len(events)
    assert main(["governance-run", "--force"]) == 0
    assert len(gov.FileSink(tmp_path / "gov.jsonl").load()) == len(events) + 1


def test_two_instances_cannot_double_decide_or_overwrite_policy(tmp_path, monkeypatch):
    """서버리스 인스턴스 둘이 같은 원장을 쓰는 경우(각자 읽기 캐시가 낡음):
    같은 건 이중 결정은 409, 서로 다른 개정 채택은 최신 정책 위에 다시 얹혀 둘 다 반영된다."""
    monkeypatch.setenv("AGRISEA_REVIEWERS", "홍검토:r1,김검토:r2")
    monkeypatch.setenv("AGRISEA_ADMINS", "박관리:a1,최관리:a2")
    monkeypatch.delenv("AGRISEA_GOV_GITHUB_REPO", raising=False)
    monkeypatch.setattr(gov, "MIN_SUPPORT", 1)
    s = _tracked_store(tmp_path)
    settings = Settings(api_key="K", data_dir=tmp_path / "run")
    a, b = TestClient(create_app(settings, s)), TestClient(create_app(settings, s))
    q = a.get("/api/governance/queue").json()
    b.get("/api/governance/queue")  # B가 지금 원장을 읽어 캐시
    hen = next(x for x in q["items"] if x["ai_status"] == "done")
    manure = next(x for x in q["items"] if x["ai_status"] == "reraised")
    ok = {"action": "approve", "fingerprint": fp_of(a, hen["id"])}

    assert a.post(f"/api/governance/cases/{hen['id']}/decision", headers=H("r1"), json=ok).status_code == 200
    late = b.post(f"/api/governance/cases/{hen['id']}/decision", headers=H("r2"), json=ok)
    assert late.status_code == 409  # B의 낡은 캐시로는 통과했지만 기록 직전 최신 원장에서 막힘
    led = gov.FileSink(tmp_path / "run" / "governance.jsonl").load()
    assert sum(1 for e in led if e["kind"] == "decision") == 1 and gov.verify_chain(led)["ok"]

    a.post(f"/api/governance/cases/{manure['id']}/decision", headers=H("r1"), json={
        "action": "reject", "final_status": "none", "reason": "keyword", "keywords": ["가축분뇨", "퇴비화"],
        "fingerprint": fp_of(a, manure["id"])})
    b.app.state.ledger._loaded = 0  # B도 제안 두 개를 봄
    props = {p["value"]: p["id"] for p in b.get("/api/governance/proposals").json()["proposals"]}
    assert set(props) == {"가축분뇨", "퇴비화"}
    assert a.post(f"/api/governance/proposals/{props['퇴비화']}/adopt", headers=H("a1"), json={}).json()["policy"]["version"] == 2
    r = b.post(f"/api/governance/proposals/{props['가축분뇨']}/adopt", headers=H("a2"), json={})
    pol = r.json()["policy"]
    assert pol["version"] == 3 and pol["keyword_block"] == ["퇴비화", "가축분뇨"]  # 덮어쓰지 않고 이어서 반영
    assert r.json()["event"]["payload"]["previous_version"] == 2
    b.app.state.ledger._loaded = 0
    again = b.post(f"/api/governance/proposals/{props['퇴비화']}/adopt", headers=H("a2"), json={})
    assert again.status_code in (404, 409)  # 이미 채택된 제안은 다시 채택하지 않음


# ── 검증에서 드러난 결함의 회귀 테스트 ─────────────────────────
def test_assertive_verdicts_always_escalate_and_sampling_survives_unrelated_revisions():
    pol = dict(gov.DEFAULT_POLICY)
    strong = [{"source": "minutes", "kind": "reraised", "score": 5, "date": "2025-01-01"},
              {"source": "minutes", "kind": "reraised", "score": 6, "date": "2025-02-01"}]
    reraised = {"id": 44333, "status": "reraised", "action": "review", "evidence": strong}
    r = gov.assess(reraised, pol)
    assert r["score"] < pol["risk_threshold"] and r["tier"] == "high" and "assertive" in {f["code"] for f in r["factors"]}
    for i in range(50):  # 표본 추출 운과 무관하게 언제나 숙련자에게
        assert gov.route(f"followup:{i}", r, {**pol, "version": i}) == "escalated"
    quiet = gov.assess({"id": 1, "status": "none", "evidence": []}, pol)
    v2 = {**pol, "version": 2, "keyword_block": ["시스템"]}
    assert [gov.route(f"followup:{i}", quiet, pol) for i in range(300)] == \
           [gov.route(f"followup:{i}", quiet, v2) for i in range(300)]  # 표본 무관 개정으로 표본이 섞이지 않음


def test_repeated_overclaims_propose_escalating_that_verdict():
    items = [{"id": i, "status": "progress", "evidence": [], "keywords": []} for i in range(2)]
    led = gov.Ledger(gov.MemorySink())
    for x in items:
        led.append("decision", "검토자", "reviewer", {
            "action": "reject", "ai_status": "progress", "final_status": "none", "reason": "overclaim",
            "keywords": [], "fingerprint": gov.fingerprint(x), "path": "sampled", "policy_version": 1}, case=gov.case_id(x))
    events = led.events()
    p = next(p for p in gov.proposals(items, events, gov.active_policy(events)) if p["type"] == "escalate_status")
    assert p["value"] == "progress" and len(p["support"]) == 2
    new = gov.adopt(gov.active_policy(events), p)
    assert new["escalate_statuses"] == ["done", "reraised", "progress"]
    assert gov.assess(items[0], new)["tier"] == "high"
    with pytest.raises(gov.LedgerConflict):
        gov.adopt(new, p)


def test_tampered_ledger_is_not_trusted_and_stops_writes(client, tmp_path):
    c = client
    manure = next(x for x in c.get("/api/governance/queue").json()["items"] if x["ai_status"] == "reraised")
    assert c.post(f"/api/governance/cases/{manure['id']}/decision", headers=H("r1"), json={
        "action": "reject", "final_status": "none", "reason": "keyword", "keywords": ["퇴비화"],
        "fingerprint": fp_of(c, manure["id"])}).status_code == 200
    path = tmp_path / "run" / "governance.jsonl"
    events = gov.FileSink(path).load()
    events[-1]["payload"]["final_status"] = "done"  # 결정 내용 바꿔치기
    path.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events), encoding="utf-8")
    c.app.state.ledger._loaded = 0

    st = c.get("/api/governance/status").json()
    assert st["chain"]["ok"] is False
    row = c.get(f"/api/followups/{manure['id']}").json()
    assert row["status"] == "reraised" and row["gov"]["state"] == "pending_review"  # 변조된 결정은 반영 안 함
    hen = next(x for x in c.get("/api/governance/queue").json()["items"] if x["ai_status"] == "done")
    r = c.post(f"/api/governance/cases/{hen['id']}/decision", headers=H("r1"),
               json={"action": "approve", "fingerprint": fp_of(c, hen["id"])})
    assert r.status_code == 503 and "깨져" in r.json()["detail"]
    assert len(gov.FileSink(path).load()) == len(events)  # 깨진 원장에 덧붙이지 않음


def test_decisions_need_durable_ledger_and_open_mode_is_local_only(tmp_path, monkeypatch):
    monkeypatch.setenv("AGRISEA_REVIEWERS", "홍검토:r1")
    monkeypatch.delenv("AGRISEA_ADMINS", raising=False)
    monkeypatch.delenv("AGRISEA_GOV_GITHUB_REPO", raising=False)
    s = _tracked_store(tmp_path)
    c = TestClient(create_app(Settings(api_key="K", data_dir=tmp_path / "srv", serverless=True), s))
    assert c.get("/api/governance/status").json()["durable"] is False
    hen = next(x for x in c.get("/api/governance/queue").json()["items"] if x["ai_status"] == "done")
    r = c.post(f"/api/governance/cases/{hen['id']}/decision", headers=H("r1"),
               json={"action": "approve", "fingerprint": fp_of(c, hen["id"])})
    assert r.status_code == 503 and "임시 저장소" in r.json()["detail"]  # 사라질 증거로 결정하지 않음

    monkeypatch.delenv("AGRISEA_REVIEWERS")
    app = create_app(Settings(api_key="K", data_dir=tmp_path / "local"), s)  # 토큰 없는 로컬 개발 모드
    assert TestClient(app).get("/api/governance/status").json()["me"]["role"] == "admin"
    remote = TestClient(app, client=("10.0.0.5", 50000))  # 0.0.0.0 으로 열었을 때 다른 컴퓨터
    assert remote.get("/api/governance/status").json()["me"]["role"] == "viewer"


def test_run_is_recorded_when_ai_output_changes(client):
    c = client
    c.get("/api/followups")
    app = c.app
    runs = lambda: [e for e in app.state.ledger.events() if e["kind"] == "run"]
    assert len(runs()) == 1 and runs()[0]["payload"]["output_hash"]
    st = app.state.store
    d = st.kv_get("followups")
    st.kv_set("followups", {**d, "items": d["items"][:1]})  # 화면 수집 등으로 판정 결과가 바뀜
    c.get("/api/followups")
    assert len(runs()) == 2 and runs()[-1]["payload"]["cases"] == 1
    c.get("/api/followups")
    assert len(runs()) == 2  # 같은 결과면 다시 기록하지 않음
