"""FastAPI 웹 서비스: 검색·요약·브리핑·온톨로지 탐색 API와 단일 페이지 UI."""
from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response

from .. import analysis, members, programs, readable
from ..config import Settings, get_settings
from ..lexicon import ISSUES
from ..ontology import KG_PATH, PRESET_QUERIES, kg_from_file, kg_from_store, run_sparql
from ..pipeline import seed_meta
from ..ontology import SCHEMA_PATH
from ..store import Store

STATIC = Path(__file__).parent / "static"
FORBIDDEN_SPARQL = re.compile(r"\b(SERVICE|LOAD|INSERT|DELETE|DROP|CLEAR|CREATE|COPY|MOVE|ADD)\b", re.I)


class KGCache:
    """SPARQL용 지식그래프(pyoxigraph). 미리 계산된 data/kg.nt.gz 를 우선 쓰고,
    화면에서 수집해 데이터가 바뀐 뒤에는 첫 질의 때 저장소에서 다시 만든다."""

    def __init__(self, store: Store):
        self.store = store
        self._kg = None
        self._dirty = False
        self._lock = threading.Lock()

    def get(self):
        with self._lock:
            if self._kg is None:
                self._kg = (kg_from_file(KG_PATH) if not self._dirty and KG_PATH.exists()
                            else kg_from_store(self.store))
            return self._kg

    def invalidate(self) -> None:
        with self._lock:
            self._kg, self._dirty = None, True


def create_app(settings: Settings | None = None, store: Store | None = None) -> FastAPI:
    settings = settings or get_settings()
    storage = "memory"
    startup_error = ""
    if store is None:
        from ..pipeline import seed_store
        try:
            storage = "seed" if seed_store(settings) else "local"
            settings.ensure_dirs()
            # 서버리스는 콜드스타트마다 인덱스를 새로 만들지 않도록 LIKE 검색 사용
            store = Store(settings.db_path, fts=not settings.serverless)
        except Exception as e:  # 저장소를 못 열어도 화면·진단은 뜨도록 메모리 DB로 대체
            startup_error = f"{type(e).__name__}: {e}"
            storage = "memory"
            store = Store(":memory:")
    if settings.serverless and storage != "memory":
        storage = "seed" if storage == "seed" else "ephemeral"

    graphs = KGCache(store)

    def cached(key: str):
        """사전 계산된 집계(kv)를 쓰고, 없으면 계산해 저장."""
        from ..precompute import CACHED_VIEWS
        value = store.kv_get(key)
        if value is None:
            value = CACHED_VIEWS[key](store)
            store.kv_set(key, value)
        return value
    app = FastAPI(title="국회 농해수위 온톨로지", version="0.1.0")
    app.state.store = store

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/health")
    def health():
        import platform
        import sqlite3
        return {
            "ok": not startup_error,
            "startup_error": startup_error,
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
            "fts": store.fts,
            "storage": storage,
            "serverless": settings.serverless,
            "data_dir": str(settings.data_dir),
            "static_index": (STATIC / "index.html").exists(),
            "api_key_configured": bool(settings.api_key),
            "llm_enabled": settings.llm_enabled,
        }

    @app.get("/api/stats")
    def stats():
        return {**store.stats(), "llm_enabled": settings.llm_enabled,
                "api_key_configured": bool(settings.api_key),
                "storage": storage, "serverless": settings.serverless,
                "data_updated": seed_meta().get("exported_at")}

    @app.get("/api/meetings")
    def meetings(date_from: str = "", date_to: str = "", q: str = "", session: int | None = None):
        return store.meetings(date_from, date_to, q, session=session)

    @app.get("/api/sessions")
    def sessions():
        """회기 목록(최근 순): 회기 번호, 정기회/임시회, 기간, 회의 수."""
        return cached("sessions")

    @app.get("/api/overview")
    def overview():
        """대시보드: 최근 회의와 최근 정부 약속."""
        return cached("overview")

    @app.get("/api/issue-trend")
    def issue_trend():
        """회기 × 쟁점 언급량(히트맵)."""
        return cached("issue_by_session")

    @app.get("/api/meetings/{meeting_id}")
    def meeting(meeting_id: str):
        m = store.meeting(meeting_id)
        if not m:
            raise HTTPException(404, "회의를 찾을 수 없습니다.")
        return {"meeting": m, "utterances": store.utterances(meeting_id=meeting_id)}

    @app.get("/api/meetings/{meeting_id}/summary")
    def summary(meeting_id: str, llm: bool = False):
        if not store.meeting(meeting_id):
            raise HTTPException(404, "회의를 찾을 수 없습니다.")
        base = store.summary(meeting_id, "rule")
        if base is None or "key_point_items" not in base:  # 없거나 이전 형식이면 새로 만든다
            base = analysis.summarize_meeting(store, meeting_id)
            if base["meeting"]["text_status"] == "parsed":
                store.save_summary(meeting_id, "rule", base)
        cached = store.summary(meeting_id, "llm")
        if llm and not cached:
            if not settings.llm_enabled:
                raise HTTPException(400, "ANTHROPIC_API_KEY가 설정되지 않아 LLM 요약을 사용할 수 없습니다.")
            from ..llm import llm_summarize
            try:
                cached = llm_summarize(store, meeting_id, settings)
            except Exception as e:
                raise HTTPException(502, f"LLM 요약 실패: {e}") from e
        base["llm"] = cached
        return base

    @app.get("/api/meetings/{meeting_id}/export.md")
    def meeting_export(meeting_id: str, request: Request, download: bool = False):
        """외부 AI에 넣어 분석할 회의록 전문(Markdown)."""
        base = str(request.base_url).rstrip("/")
        md = analysis.meeting_markdown(store, meeting_id, base)
        if md is None:
            raise HTTPException(404, "회의를 찾을 수 없습니다.")
        headers = {"Content-Disposition": f'attachment; filename="minutes-{meeting_id}.md"'} if download else {}
        return PlainTextResponse(md, media_type="text/markdown; charset=utf-8", headers=headers)

    @app.get("/api/search")
    def search(q: str = Query(..., min_length=1), speaker_type: str = "", date_from: str = "",
               date_to: str = "", limit: int = Query(50, ge=1, le=500), session: int | None = None):
        rows = store.search(q, limit, speaker_type, date_from, date_to, session=session)
        # 목록은 관련도 상위 limit건, 전체 건수·회기 분포는 일치하는 발언 전체 기준
        return {"query": q, "count": len(rows), "results": rows,
                **store.search_stats(q, speaker_type, date_from, date_to, session=session),
                "issues": analysis._issue_rank(rows, 6)}

    @app.get("/api/issues")
    def issues():
        return cached("issues")

    @app.get("/api/issues/{issue_id}")
    def issue(issue_id: str, session: int | None = None):
        """쟁점 상세: 회기별 추이, 관련 기관·위원, 대표 질의, 정부 약속."""
        try:
            d = analysis.issue_detail(store, issue_id, session)
        except KeyError:
            raise HTTPException(404, "쟁점을 찾을 수 없습니다.") from None
        # 이 쟁점의 지원사업 공고(공공데이터포털 K-Startup)와 국회 관심 대비 공급 신호
        d["programs"] = programs.issue_view(store, issue_id, cached("program_signals"))
        return d

    @app.get("/api/meetings/{meeting_id}/turns")
    def meeting_turns(meeting_id: str):
        """질의 차례: 위원별로 묶은 질문 핵심 문장 → 정부 답변 요지(원문 발언 번호 포함)."""
        if not store.meeting(meeting_id):
            raise HTTPException(404, "회의를 찾을 수 없습니다.")
        return {"meeting_id": meeting_id, "turns": readable.meeting_turns(store, meeting_id)}

    @app.get("/api/readable")
    def readable_glossary():
        """'읽기 쉽게' 보기용 사전: 끊긴 낱말 잇기, 기관 약칭 → 정식 명칭, 회의록에 나온 법률 이름."""
        return cached("readable")

    @app.get("/api/programs/overview")
    def programs_overview():
        """대시보드용 지원사업 공고 요약: 접수 중·예정 수, 마감이 가까운 공고, 최근 공고(접수 상태는 오늘 기준)."""
        return programs.overview(store)

    def member_scope(session: int | None, meetings: str) -> dict:
        """위원별 분석 범위: 고른 회의들 > 회기 > 전체(전체·회기 결과는 저장해 재사용)."""
        ids = [m for m in meetings.split(",") if m.strip()][:60] if meetings else []
        if ids:
            return members.member_profiles(store, meeting_ids=ids)
        if not session:
            return cached("members_all")
        key = f"members:s{session}"
        value = store.kv_get(key)
        if value is None:
            value = members.member_profiles(store, session=session)
            store.kv_set(key, value)
        return value

    @app.get("/api/members")
    def member_list(session: int | None = None, meetings: str = ""):
        """위원별 관심 쟁점·질의 관점·핵심어 요약(질의 많은 순). meetings=회의ID,회의ID 로 일부 회의만."""
        p = member_scope(session, meetings)
        return {"scope": p["scope"], "members": members.summary_rows(p)}

    @app.get("/api/members/{name}")
    def member_detail(name: str, session: int | None = None, meetings: str = ""):
        """위원 상세: 관심 쟁점, 질의 관점, 핵심어, 상대 기관, 예상 질문, 대표 질의응답, 받아낸 약속."""
        p = member_scope(session, meetings)
        if name not in p["members"]:
            raise HTTPException(404, "이 범위에서 해당 위원의 발언을 찾을 수 없습니다.")
        return {"scope": p["scope"], **p["members"][name]}

    # ── 정부 약속 이행 추적 + 운영 관문(G1~G7) ──
    from .. import governance as gov
    from ..followup import STATUS as FU_STATUS
    from ..followup import track_all
    FINAL_STATUSES = {**{k: v[0] for k, v in FU_STATUS.items()}, "excluded": "추적 제외(약속 아님)"}
    EXCLUDED_DESC = "약속으로 보기 어려운 발언이라 추적에서 제외"
    ledger = gov.ledger_from_env(settings.data_dir, settings.serverless)
    roles = gov.Roles.from_env(settings.serverless)
    app.state.ledger, app.state.roles = ledger, roles
    fu_lock = threading.Lock()

    runs_seen: set[tuple] = set()

    def followups_data() -> dict:
        """이행 판정(G1 산출물). 정책 버전이 바뀌면(G7 제도 반영) 새 정책으로 다시 판정한다.
        (판정 결과, 정책 버전)마다 AI 실행 기록이 원장에 없으면 한 번 남긴다(자동 수집이 이미 남겼으면 생략).
        판정 결과는 건·지문의 해시라, 화면에서 수집해 데이터가 바뀌어도 새 실행으로 기록된다."""
        events = ledger.events()
        pol = gov.active_policy(events)
        d = store.kv_get("followups")
        trigger = "판정 불러오기"
        if d is None or d.get("policy_version", 1) != pol["version"]:
            with fu_lock:
                d = store.kv_get("followups")
                if d is None or d.get("policy_version", 1) != pol["version"]:
                    trigger = "정책 적용 재판정" if d is not None else "판정"
                    d = track_all(store, pol)
                    store.kv_set("followups", d)
        data_hash = seed_meta().get("hash") or "local"
        out_hash = gov.items_hash(d["items"])
        key = (out_hash, pol["version"])
        if key not in runs_seen:
            runs_seen.add(key)
            if not gov.has_run(events, out_hash, pol["version"]):
                try:
                    ledger.append("run", "AI 판정", "system", run_payload(d, pol, data_hash, trigger))
                except Exception as e:  # 원장 기록이 실패해도 판정은 보여 준다(상태 화면에 오류 표시)
                    ledger.error = f"{type(e).__name__}: {e}"
        return d

    def run_payload(d: dict, pol: dict, data_hash: str, trigger: str) -> dict:
        return gov.run_payload(d, pol, data_hash, trigger)

    def followup_item(cid: int) -> dict:
        item = next((x for x in followups_data()["items"] if x["id"] == cid), None)
        if item is None:
            raise HTTPException(404, "약속을 찾을 수 없습니다.")
        return item

    def followup_view(item: dict) -> dict:
        out = dict(item)
        out["status_desc"] = FU_STATUS[out["status"]][1]
        return out

    def governed(items: list[dict], events: list[dict] | None = None) -> list[dict]:
        """판정마다 관문 상태를 붙이고, 공개 판정은 숙련자 결정(정정)을 따른다. AI 판정은 ai_status로 남긴다."""
        events = ledger.events() if events is None else events
        pol, stacks = gov.active_policy(events), gov.decision_stacks(events)
        out = []
        for x in items:
            v = gov.case_view(x, pol, stacks.get(gov.case_id(x), []))
            fin = v["final_status"]
            desc = FU_STATUS[fin][1] if fin in FU_STATUS else EXCLUDED_DESC
            out.append({**x, "ai_status": x["status"], "ai_status_label": x["status_label"],
                        "status": fin, "status_label": FINAL_STATUSES[fin], "status_desc": desc, "gov": v})
        return out

    def current_items() -> list[dict]:
        return [followup_view(x) for x in followups_data()["items"]]

    @app.get("/api/followups")
    def followup_list(q: str = "", org: str = "", action: str = "", session: int | None = None):
        """약속 목록(최근 순)과 상태별 개수. 상태 거르기는 화면에서(개수는 다른 조건만 반영)."""
        from collections import Counter
        d = followups_data()
        items = governed(current_items())
        if org:
            items = [x for x in items if x["org"] == org]
        if action:
            items = [x for x in items if x["action"] == action]
        if session:
            items = [x for x in items if x["session"] == session]
        if q:
            items = [x for x in items if q in x["text"] or q in ((x["question"] or {}).get("text") or "")
                     or any(q in k for k in x["keywords"] + x["laws"])]
        counts = Counter(x["status"] for x in items)
        orgs = Counter(x["org"] for x in d["items"] if x["org"])
        keep = ("id", "date", "session", "org", "speaker", "text", "status", "status_label", "ai_status", "action",
                "action_label", "keywords", "laws", "issues")
        statuses = {**d["statuses"], **({"excluded": {"label": FINAL_STATUSES["excluded"], "desc": EXCLUDED_DESC}}
                                        if counts.get("excluded") else {})}
        return {"as_of": d["as_of"], "vague": d["vague"], "statuses": statuses, "actions": d["actions"],
                "policy_version": d.get("policy_version", 1),
                "counts": {k: counts.get(k, 0) for k in statuses}, "total": len(items),
                "orgs": [{"name": n, "count": c} for n, c in orgs.most_common()],
                "items": [{**{k: x[k] for k in keep}, "n_evidence": len(x["evidence"]),
                           "gov": {k: x["gov"][k] for k in ("path", "path_label", "state", "state_label")}
                           | {"risk": x["gov"]["risk"]["score"]}} for x in items]}

    @app.get("/api/followups/{cid}")
    def followup_detail(cid: int):
        """약속 상세: 앞선 질의, 대상어, 근거 타임라인, 관문 상태."""
        item = followup_view(followup_item(cid))
        return governed([item])[0]

    # ── 운영 관문 API ──
    loopback = {"127.0.0.1", "::1", "localhost", "testclient"}

    def me(request: Request) -> dict:
        who = roles.who(request.headers.get("x-agrisea-token"))
        # 개발용 개방 모드는 이 컴퓨터에서 온 요청만(0.0.0.0 으로 열어도 다른 컴퓨터가 관리자가 되지 않게)
        if who["open"] and (request.client is None or request.client.host not in loopback):
            return {"name": "", "role": "viewer", "open": False}
        return who

    def require(request: Request, role: str) -> dict:
        """결정·롤백·제도 반영의 공통 관문: 역할 확인 → 원장이 영구 보관·무결해야 기록."""
        who = me(request)
        if role == "admin" and who["role"] != "admin" or role == "reviewer" and who["role"] not in ("reviewer", "admin"):
            need = "관리자(제도 승인권자)" if role == "admin" else "검토자(숙련자)"
            raise HTTPException(403, f"{need} 권한이 필요합니다. X-Agrisea-Token 헤더로 인증 토큰을 보내세요.")
        if not ledger.sink.durable:
            raise HTTPException(503, "운영 증거 원장이 임시 저장소라 결정을 기록하지 않습니다. "
                                     "AGRISEA_GOV_GITHUB_REPO·AGRISEA_GOV_GITHUB_TOKEN 을 설정하세요.")
        chain = gov.verify_chain(ledger.all_events())
        if not chain["ok"]:
            raise HTTPException(503, f"원장 해시 연결이 {chain['broken_at']}번 이벤트에서 깨져 기록을 멈췄습니다. 원장을 복구하세요.")
        return who

    def append(*args, **kw) -> dict:
        try:
            return ledger.append(*args, **kw)
        except gov.LedgerConflict as e:
            raise HTTPException(409, str(e)) from None
        except gov.LedgerBroken as e:
            raise HTTPException(503, str(e)) from None

    def gov_status(request: Request | None = None) -> dict:
        return {"storage": ledger.sink.kind, "durable": ledger.sink.durable, "error": ledger.error,
                "chain": gov.verify_chain(ledger.all_events()), "policy": gov.active_policy(ledger.events()),
                "roles": {"configured": roles.configured, "open_mode": roles.open_mode},
                "me": me(request) if request else None, "gates": gov.GATES, "reasons": gov.REASONS,
                "final_statuses": FINAL_STATUSES, "paths": gov.PATHS, "states": gov.STATES}

    @app.get("/api/governance/status")
    def governance_status(request: Request):
        """원장 보관 방식(영구/임시)·해시 연결 검증·현재 정책·내 역할."""
        return gov_status(request)

    @app.get("/api/governance/overview")
    def governance_overview(request: Request):
        """관문별 운영 지표(G1~G7)와 상태."""
        items = current_items()  # 판정을 먼저(정책이 바뀌었으면 재판정·실행 기록) 한 뒤 원장을 읽는다
        events = ledger.events()
        pol = gov.active_policy(events)
        runs = [e for e in events if e["kind"] == "run"]
        return {**gov_status(request), "metrics": gov.metrics(items, events, pol),
                "last_run": runs[-1] if runs else None,
                "proposals": len(gov.proposals(items, events, pol)),
                "judged_with_policy": followups_data().get("policy_version", 1)}

    @app.get("/api/governance/queue")
    def governance_queue(view: str = "pending", org: str = ""):
        """검토 대기열: 숙련자 이관·표본 검증·재검토 필요 건(위험 높은 순). view=all|reviewed|auto 도 가능."""
        items = governed(current_items())
        want = {"pending": ("pending_review", "pending_sample", "stale"), "reviewed": ("approved", "corrected"),
                "auto": ("auto_published",)}.get(view)
        rows = [x for x in items if (want is None or x["gov"]["state"] in want) and (not org or x["org"] == org)]
        order = {"stale": 0, "pending_review": 1, "pending_sample": 2}
        rows.sort(key=lambda x: (order.get(x["gov"]["state"], 3), -x["gov"]["risk"]["score"], x["date"]), reverse=False)
        return {"view": view, "count": len(rows), "items": [{
            "id": x["id"], "date": x["date"], "org": x["org"], "speaker": x["speaker"], "text": x["text"],
            "ai_status": x["ai_status"], "ai_status_label": x["ai_status_label"], "status": x["status"],
            "status_label": x["status_label"], "action_label": x["action_label"],
            "gov": {k: x["gov"][k] for k in ("path", "path_label", "state", "state_label", "risk")}} for x in rows]}

    @app.get("/api/governance/cases/{cid}")
    def governance_case(cid: int):
        """건 상세: AI 판정·근거·위험 요인·경로·결정 이력(원장 이벤트)."""
        x = followup_detail(cid)
        x["history"] = [e for e in ledger.all_events() if e.get("case") == gov.case_id(x)]
        return x

    @app.post("/api/governance/cases/{cid}/decision")
    def governance_decide(cid: int, request: Request, payload: dict[str, Any] = Body(...)):
        """G4 숙련자 검토: approve(AI 판정 확정) 또는 reject(정정 판정·사유 코드·문제 대상어)."""
        who = require(request, "reviewer")
        x = followup_detail(cid)
        g = x["gov"]
        if not payload.get("fingerprint"):
            raise HTTPException(400, "검토한 판정의 지문(fingerprint)을 함께 보내세요.")
        if payload["fingerprint"] != g["fingerprint"]:
            raise HTTPException(409, "그사이 AI 판정이 바뀌었습니다. 화면을 새로 고친 뒤 다시 검토하세요.")
        action = payload.get("action")
        if action not in ("approve", "reject"):
            raise HTTPException(400, "action은 approve 또는 reject 입니다.")
        final, reason = x["ai_status"], None
        kws = []
        if action == "reject":
            final, reason = payload.get("final_status"), payload.get("reason")
            if final not in FINAL_STATUSES or final == x["ai_status"]:
                raise HTTPException(400, "거부할 때는 AI 판정과 다른 올바른 판정을 고르세요.")
            if reason not in gov.REASONS:
                raise HTTPException(400, "거부 사유 코드를 고르세요.")
            kws = [k for k in payload.get("keywords") or [] if k in x["keywords"]]
            if reason == "keyword" and not kws:
                raise HTTPException(400, "대상어 오인이면 잘못 쓰인 대상어를 하나 이상 고르세요.")
            if reason == "keyword" and not x["evidence"]:
                raise HTTPException(400, "근거가 없는 판정에는 대상어 오인 사유를 쓸 수 없습니다.")
        if g["decision"] and g["state"] != "stale":
            raise HTTPException(409, "이미 결정된 건입니다. 바꾸려면 먼저 롤백하세요.")
        cs = gov.case_id(x)

        def body(events: list[dict]) -> dict:
            # 다른 인스턴스가 그사이 같은 건을 결정했으면 기록하지 않는다(최신 원장 기준으로 다시 검사)
            stack = gov.decision_stacks(events).get(cs, [])
            if stack and stack[-1]["payload"].get("fingerprint") == g["fingerprint"]:
                raise gov.LedgerConflict("다른 검토자가 먼저 결정했습니다. 화면을 새로 고치세요.")
            return {"action": action, "ai_status": x["ai_status"], "final_status": final, "reason": reason,
                    "keywords": kws, "note": str(payload.get("note") or "")[:500], "fingerprint": g["fingerprint"],
                    "path": g["path"], "risk_score": g["risk"]["score"],
                    "risk_factors": [f["code"] for f in g["risk"]["factors"]],
                    "policy_version": gov.active_policy(events)["version"],
                    "subject": {"org": x["org"], "date": x["date"], "text": x["text"][:120]}}
        ev = append("decision", who["name"], who["role"], body, case=cs)
        return {"event": ev, "case": governance_case(cid)}

    @app.post("/api/governance/cases/{cid}/rollback")
    def governance_rollback(cid: int, request: Request, payload: dict[str, Any] = Body(...)):
        """G5 예외처리: 직전 결정을 취소(사유 필수). 검토자는 자기 결정만, 관리자는 모든 결정을."""
        who = require(request, "reviewer")
        reason = str(payload.get("reason") or "").strip()
        if not reason:
            raise HTTPException(400, "롤백 사유를 적으세요.")
        cid_s = f"followup:{cid}"
        stack = gov.decision_stacks(ledger.events()).get(cid_s, [])
        if not stack:
            raise HTTPException(409, "되돌릴 결정이 없습니다.")
        target = stack[-1]
        if who["role"] != "admin" and target["actor"] != who["name"]:
            raise HTTPException(403, "다른 검토자의 결정은 관리자만 롤백할 수 있습니다.")

        def body(events: list[dict]) -> dict:
            latest = gov.decision_stacks(events).get(cid_s, [])
            if not latest or latest[-1]["seq"] != target["seq"]:
                raise gov.LedgerConflict("그사이 이 건의 결정이 바뀌었습니다. 화면을 새로 고치세요.")
            return {"target": target["seq"], "target_action": target["payload"]["action"], "reason": reason[:500]}
        ev = append("rollback", who["name"], who["role"], body, case=cid_s)
        return {"event": ev, "case": governance_case(cid)}

    @app.get("/api/governance/ledger")
    def governance_ledger(kind: str = "", case: str = "", limit: int = Query(200, le=2000)):
        """운영 증거 원장(최근 순, 저장된 그대로)과 해시 연결 검증."""
        events = ledger.all_events()
        rows = [e for e in events if (not kind or e["kind"] == kind) and (not case or e.get("case") == case)]
        return {"chain": gov.verify_chain(events), "count": len(rows), "events": list(reversed(rows))[:limit]}

    @app.get("/api/governance/metrics")
    def governance_metrics():
        """G6 평가 지표: 자동화율·이관율·AI–숙련자 일치율(경로별·정책 버전별)·혼동 행렬·거부 사유."""
        items = current_items()
        events = ledger.events()
        return gov.metrics(items, events, gov.active_policy(events))

    @app.get("/api/governance/dataset.{fmt}")
    def governance_dataset(fmt: str):
        """G6 평가 데이터 내려받기(JSONL·CSV): 유효 결정 한 건이 한 줄."""
        import csv
        import io
        import json as _json
        items = current_items()
        events = ledger.events()
        rows = gov.dataset(items, events, gov.active_policy(events))
        headers = {"Content-Disposition": f'attachment; filename="governance-dataset.{fmt}"'}
        if fmt == "jsonl":
            return PlainTextResponse("".join(_json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                                     media_type="application/x-ndjson; charset=utf-8", headers=headers)
        if fmt == "csv":
            buf = io.StringIO()
            cols = ["case", "decision_seq", "decided_at", "reviewer", "path", "risk_score", "policy_version",
                    "ai_status", "final_status", "agree", "reason", "flagged_keywords", "outcome", "rollback_reason",
                    "stale", "removed", "org", "action", "promise_date"]
            w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow({**r, "flagged_keywords": " ".join(r["flagged_keywords"] or [])})
            return PlainTextResponse("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8", headers=headers)
        raise HTTPException(404, "jsonl 또는 csv")

    @app.get("/api/governance/proposals")
    def governance_proposals():
        """G7 제도 반영: 예외에서 나온 규칙 개정 제안과 정책 버전 이력."""
        items = current_items()
        events = ledger.events()
        pol = gov.active_policy(events)
        return {"policy": pol, "proposals": gov.proposals(items, events, pol),
                "history": gov.policy_history(events),
                "dismissed": [e for e in events if e["kind"] == "proposal_dismissed"],
                "judged_with_policy": followups_data().get("policy_version", 1)}

    def find_proposal(pid: str) -> tuple[dict, dict]:
        items = current_items()
        events = ledger.events()
        pol = gov.active_policy(events)
        p = next((x for x in gov.proposals(items, events, pol) if x["id"] == pid), None)
        if p is None:
            raise HTTPException(404, "제안을 찾을 수 없습니다(이미 처리됐거나 근거가 바뀜).")
        return p, pol

    @app.post("/api/governance/proposals/{pid}/adopt")
    def governance_adopt(pid: str, request: Request, payload: dict[str, Any] = Body(default={})):
        """관리자가 개정안을 채택 → 새 정책 버전. 다음 판정(조회)부터 새 정책으로 다시 판정된다."""
        who = require(request, "admin")
        p, _ = find_proposal(pid)

        def body(events: list[dict]) -> dict:
            # 다른 관리자가 그사이 다른 개정을 채택했으면 그 최신 정책 위에 다시 얹는다(덮어쓰지 않게)
            if p["id"] in gov.closed_proposals(events):
                raise gov.LedgerConflict("이미 처리된 제안입니다.")
            latest = gov.active_policy(events)
            return {"policy": gov.adopt(latest, p), "previous_version": latest["version"], "proposal": p["id"],
                    "change": p["title"], "rationale": p["rationale"], "support": p["support"],
                    "note": str(payload.get("note") or "")[:500]}
        ev = append("policy", who["name"], who["role"], body)
        return {"event": ev, "policy": ev["payload"]["policy"]}

    @app.post("/api/governance/proposals/{pid}/dismiss")
    def governance_dismiss(pid: str, request: Request, payload: dict[str, Any] = Body(default={})):
        who = require(request, "admin")
        p, _ = find_proposal(pid)
        reason = str(payload.get("reason") or "").strip()
        if not reason:
            raise HTTPException(400, "기각 사유를 적으세요.")
        def body(events: list[dict]) -> dict:
            if p["id"] in gov.closed_proposals(events):
                raise gov.LedgerConflict("이미 처리된 제안입니다.")
            return {"proposal": p["id"], "title": p["title"], "reason": reason[:500]}
        ev = append("proposal_dismissed", who["name"], who["role"], body)
        return {"event": ev}

    @app.get("/api/governance/report.md")
    def governance_report():
        """모든 관문의 운영 증거 보고서(Markdown)."""
        items = current_items()
        return PlainTextResponse(gov.evidence_report(items, ledger.all_events(), gov_status()),
                                 media_type="text/markdown; charset=utf-8")

    @app.get("/api/orgs")
    def orgs():
        return cached("orgs")

    @app.get("/api/speakers")
    def speakers():
        return cached("speakers")

    @app.get("/api/briefing")
    def briefing(org: str = "", issue: str = "", keyword: str = "", member: str = "",
                 date_from: str = "", date_to: str = "", format: str = "json",
                 session: int | None = None):
        # 조건별 결과를 저장해 둔다(데이터가 바뀌면 kv가 비워져 다시 계산). 넓은 조건은 수 초씩 걸린다.
        key = analysis.briefing_cache_key(org, issue, keyword, member, date_from, date_to, session)
        b = store.kv_get(key)
        if b is None:
            try:
                b = analysis.briefing(store, org, issue, keyword, member, date_from, date_to,
                                      session=session)
            except KeyError as e:
                raise HTTPException(404, e.args[0]) from None
            # 화면의 선택 상자로 고르는 조건(기관·쟁점·위원·회기)만 저장: 임의 입력으로 캐시가 늘지 않게
            if not (keyword or date_from or date_to) and b["counts"]["utterances"]:
                store.kv_set(key, b)
        if org:  # 기관 브리핑: 그 기관이 주관·전담한 지원사업 공고
            b = {**b, "programs": programs.agency_view(store, org)}
        if format == "md":
            return PlainTextResponse(analysis.briefing_markdown(b),
                                     media_type="text/markdown; charset=utf-8")
        return b

    @app.get("/api/taxonomy")
    def taxonomy():
        return [{"id": k, "label": v[0], "parent": v[1], "keywords": list(v[2])}
                for k, v in ISSUES.items()]

    @app.get("/api/sparql/presets")
    def presets():
        return PRESET_QUERIES

    @app.post("/api/sparql")
    def sparql(payload: dict[str, Any] = Body(...)):
        query = str(payload.get("query", ""))
        if FORBIDDEN_SPARQL.search(query):
            raise HTTPException(400, "읽기 전용 SELECT/ASK/CONSTRUCT 질의만 허용됩니다.")
        try:
            return run_sparql(graphs.get(), query)
        except Exception as e:
            raise HTTPException(400, f"SPARQL 오류: {e}") from e

    @app.get("/api/graph")
    def graph():
        """쟁점–기관–위원 관계망(시각화용)."""
        return cached("graph")

    @app.get("/api/minutes-text")
    def minutes_text(id: str = Query(..., pattern=r"^\d{1,12}$")):
        """국회 회의록 PDF(record.assembly.go.kr)를 받아 텍스트로 반환.

        회의록 PDF 서버가 해외 접속을 막아 GitHub Actions 자동 수집이 이 엔드포인트(서울 리전)를 거친다.
        고정된 국회 회의록 주소만 조회하므로 임의 URL 프록시로 쓰일 수 없다.
        """
        from ..pipeline import RECORD_PDF_URL, download_minutes_text
        try:
            text = download_minutes_text(RECORD_PDF_URL.format(id=id), settings.pdf_dir, id)
        except Exception as e:
            raise HTTPException(502, f"회의록을 가져오지 못했습니다: {e}") from e
        return PlainTextResponse(text, media_type="text/plain; charset=utf-8")

    @app.get("/api/kstartup")
    def kstartup_relay(page: int = Query(1, ge=1, le=200), perPage: int = Query(500, ge=1, le=1000),
                       returnType: str = "json"):
        """자동 수집용 중계: 공공데이터포털이 해외(GitHub Actions) 접속을 막을 때 서울 리전에서 대신 받는다.
        공고 조회 오퍼레이션 하나만, 인증키는 서버에서 붙인다(임의 URL 프록시로 쓰일 수 없음)."""
        import requests
        if not settings.kstartup_key:
            raise HTTPException(400, "이 서비스에는 지원사업 공고 인증키가 설정되지 않았습니다.")
        try:
            r = requests.get(programs.API_URL, params={"page": page, "perPage": perPage, "returnType": returnType,
                                                       "serviceKey": settings.kstartup_key}, timeout=(10, 50))
        except requests.RequestException as e:
            raise HTTPException(502, "공공데이터포털에 접속하지 못했습니다: "
                                + str(e).replace(settings.kstartup_key, "***")) from None
        return Response(r.content, status_code=r.status_code,
                        media_type=r.headers.get("content-type", "application/json"))

    @app.get("/api/ontology.ttl")
    def ontology_ttl():
        """온톨로지 스키마(클래스·속성 정의). 인스턴스 전체는 저장소의 data/kg.nt.gz."""
        return FileResponse(SCHEMA_PATH, media_type="text/turtle; charset=utf-8")

    @app.post("/api/admin/collect")
    def admin_collect(payload: dict[str, Any] = Body(...)):
        """회의 메타데이터 수집. date_from/date_to를 주면 일자별로 순회한다(본문은 fetch-minutes로)."""
        from ..pipeline import collect, collect_range, pending_count
        if not settings.api_key:
            raise HTTPException(400, "ASSEMBLY_API_KEY가 설정되지 않았습니다.")
        if not (payload.get("date") or payload.get("date_from")):
            raise HTTPException(400, "회의일자를 입력하세요. 예: 2026-09-01 (기간은 31일 이내)")
        extra = {str(k): str(v) for k, v in (payload.get("params") or {}).items()}
        base = {"DAE_NUM": str(payload.get("dae") or "22"), **extra}
        log: list[str] = []
        if settings.serverless and payload.get("date_from"):
            from datetime import date
            try:
                span = (date.fromisoformat(payload.get("date_to") or payload["date_from"])
                        - date.fromisoformat(payload["date_from"])).days
            except ValueError as e:
                raise HTTPException(400, f"날짜 형식 오류: {e}") from e
            if span > 31:
                raise HTTPException(400, "배포 환경에서는 실행시간 제한 때문에 한 번에 31일까지만 수집할 수 있습니다. "
                                         "긴 기간은 GitHub Actions 「회의록 데이터 갱신」을 사용하세요.")
        try:
            if payload.get("date_from"):
                ms = collect_range(store, settings, payload["date_from"],
                                   payload.get("date_to") or payload["date_from"],
                                   base, progress=log.append)
            else:
                ms = collect(store, settings, {**base, "CONF_DATE": payload.get("date")},
                             progress=log.append)
        except Exception as e:
            raise HTTPException(502, f"수집 실패: {e}") from e
        graphs.invalidate()
        return {"meetings": len(ms), "pending": pending_count(store), "log": log}

    @app.post("/api/admin/fetch-minutes")
    def admin_fetch_minutes(limit: int = Query(3, ge=1, le=20)):
        """본문 미수집 회의를 limit건씩 처리(서버리스 실행시간 제한 대응). pending이 0이 될 때까지 반복 호출."""
        from ..pipeline import fetch_minutes, pending_count
        log: list[str] = []
        ok = fetch_minutes(store, settings, limit=limit, progress=log.append)
        if ok:  # 처리한 회의가 없으면 그래프를 다시 만들 이유가 없다(다음 SPARQL이 수십 초 걸림)
            graphs.invalidate()
        return {"parsed": ok, "pending": pending_count(store), "log": log}

    @app.post("/api/admin/sample")
    def admin_sample():
        """가상 예시 데이터(시연용). 실제 회의록이 있는 서비스에는 섞지 않는다(공개 판정·통계가 오염됨)."""
        from ..pipeline import load_sample
        st = store.stats()
        if st["meetings"] - st["samples"] > 0:
            raise HTTPException(409, "실제 회의록이 있어 가상 예시 데이터를 넣지 않습니다(데이터가 없는 환경의 시연용).")
        n = load_sample(store)
        graphs.invalidate()
        return {"utterances": n}

    return app
