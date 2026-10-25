"""FastAPI 웹 서비스: 검색·요약·브리핑·온톨로지 탐색 API와 단일 페이지 UI."""
from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse

from .. import analysis, members
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
               date_to: str = "", limit: int = Query(50, le=500), session: int | None = None):
        rows = store.search(q, limit, speaker_type, date_from, date_to, session=session)
        return {"query": q, "count": len(rows), "results": rows,
                "issues": analysis._issue_rank(rows, 6)}

    @app.get("/api/issues")
    def issues():
        return cached("issues")

    @app.get("/api/issues/{issue_id}")
    def issue(issue_id: str, session: int | None = None):
        """쟁점 상세: 회기별 추이, 관련 기관·위원, 대표 질의, 정부 약속."""
        try:
            return analysis.issue_detail(store, issue_id, session)
        except KeyError:
            raise HTTPException(404, "쟁점을 찾을 수 없습니다.") from None

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
    from ..followup import finalize as fu_finalize
    from ..followup import track_all
    FOLLOWUP_EXT_TTL = 24 * 3600  # 외부 근거(국회 의안·지원사업 공고)는 하루 동안 다시 묻지 않는다
    FINAL_STATUSES = {**{k: v[0] for k, v in FU_STATUS.items()}, "excluded": "추적 제외(약속 아님)"}
    ledger = gov.ledger_from_env(settings.data_dir, settings.serverless)
    roles = gov.Roles.from_env(settings.serverless)
    app.state.ledger, app.state.roles = ledger, roles
    fu_lock = threading.Lock()

    runs_seen: set[tuple] = set()

    def followups_data() -> dict:
        """이행 판정(G1 산출물). 정책 버전이 바뀌면(G7 제도 반영) 새 정책으로 다시 판정한다.
        (데이터 버전, 정책 버전)마다 AI 실행 기록이 원장에 없으면 한 번 남긴다(자동 수집이 이미 남겼으면 생략)."""
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
        key = (data_hash, pol["version"])
        if key not in runs_seen:
            runs_seen.add(key)
            if not any(e["kind"] == "run" and e["payload"].get("data_hash") == data_hash
                       and e["payload"].get("policy_version") == pol["version"] for e in events):
                try:
                    ledger.append("run", "AI 판정", "system", run_payload(d, pol, data_hash, trigger))
                except Exception as e:  # 원장 기록이 실패해도 판정은 보여 준다(상태 화면에 오류 표시)
                    ledger.error = f"{type(e).__name__}: {e}"
        return d

    def run_payload(d: dict, pol: dict, data_hash: str, trigger: str) -> dict:
        paths = __import__("collections").Counter(
            gov.route(gov.case_id(x), gov.assess(x, pol), pol) for x in d["items"])
        return {"trigger": trigger, "data_hash": data_hash, "as_of": d["as_of"], "policy_version": pol["version"],
                "cases": len(d["items"]), "vague": d["vague"], "paths": dict(paths), "statuses": d["counts"]}

    def followup_item(cid: int) -> dict:
        item = next((x for x in followups_data()["items"] if x["id"] == cid), None)
        if item is None:
            raise HTTPException(404, "약속을 찾을 수 없습니다.")
        return item

    def followup_view(item: dict, ext: dict | None) -> dict:
        """사전 계산한 내부 근거에 외부 근거를 더해 다시 판정."""
        as_of = followups_data()["as_of"]
        out = fu_finalize(item, item["evidence"] + (ext or {}).get("evidence", []), as_of) if ext else dict(item)
        out["status_desc"] = FU_STATUS[out["status"]][1]
        out["external"] = ext
        return out

    def governed(items: list[dict], events: list[dict] | None = None) -> list[dict]:
        """판정마다 관문 상태를 붙이고, 공개 판정은 숙련자 결정(정정)을 따른다. AI 판정은 ai_status로 남긴다."""
        events = ledger.events() if events is None else events
        pol, stacks = gov.active_policy(events), gov.decision_stacks(events)
        out = []
        for x in items:
            v = gov.case_view(x, pol, stacks.get(gov.case_id(x), []))
            fin = v["final_status"]
            desc = FU_STATUS[fin][1] if fin in FU_STATUS else "숙련자가 약속이 아니라고 정정해 추적에서 제외"
            out.append({**x, "ai_status": x["status"], "ai_status_label": x["status_label"],
                        "status": fin, "status_label": FINAL_STATUSES[fin], "status_desc": desc, "gov": v})
        return out

    def current_items() -> list[dict]:
        """외부 근거를 확인한 약속은 그 근거까지 더해 다시 판정한 상태(상세 화면과 같게)."""
        d = followups_data()
        return [followup_view(x, store.kv_get(f"followup_ext:{x['id']}")) for x in d["items"]]

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
        statuses = {**d["statuses"], **({"excluded": {"label": FINAL_STATUSES["excluded"], "desc": "숙련자가 약속이 아니라고 정정"}}
                                        if counts.get("excluded") else {})}
        return {"as_of": d["as_of"], "vague": d["vague"], "statuses": statuses, "actions": d["actions"],
                "policy_version": d.get("policy_version", 1),
                "counts": {k: counts.get(k, 0) for k in statuses}, "total": len(items),
                "orgs": [{"name": n, "count": c} for n, c in orgs.most_common()],
                "items": [{**{k: x[k] for k in keep}, "n_evidence": len(x["evidence"]),
                           "checked": x.get("external") is not None,
                           "gov": {k: x["gov"][k] for k in ("path", "path_label", "state", "state_label")}
                           | {"risk": x["gov"]["risk"]["score"]}} for x in items]}

    @app.get("/api/followups/{cid}")
    def followup_detail(cid: int):
        """약속 상세: 앞선 질의, 대상어, 근거 타임라인(외부 근거를 확인한 적이 있으면 함께), 관문 상태."""
        item = followup_view(followup_item(cid), store.kv_get(f"followup_ext:{cid}"))
        return governed([item])[0]

    @app.get("/api/followups/{cid}/external")
    def followup_external(cid: int, refresh: bool = False):
        """국회 의안·지원사업 공고 API로 외부 근거를 찾는다(하루 동안 저장해 재사용)."""
        import time

        from ..external import external_evidence
        item = followup_item(cid)
        key = f"followup_ext:{cid}"
        ext = store.kv_get(key)
        if refresh or not ext or time.time() - ext.get("_ts", 0) > FOLLOWUP_EXT_TTL:
            ext = {**external_evidence(settings, item), "_ts": time.time()}
            store.kv_set(key, ext)
        return governed([followup_view(item, ext)])[0]

    # ── 운영 관문 API ──
    def me(request: Request) -> dict:
        return roles.who(request.headers.get("x-agrisea-token"))

    def require(request: Request, role: str) -> dict:
        who = me(request)
        if role == "admin" and who["role"] != "admin" or role == "reviewer" and who["role"] not in ("reviewer", "admin"):
            need = "관리자(제도 승인권자)" if role == "admin" else "검토자(숙련자)"
            raise HTTPException(403, f"{need} 권한이 필요합니다. 운영 관문 화면에서 인증 토큰을 입력하세요.")
        return who

    def gov_status(request: Request | None = None) -> dict:
        events = ledger.events()
        return {"storage": ledger.sink.kind, "durable": ledger.sink.durable, "error": ledger.error,
                "chain": gov.verify_chain(events), "policy": gov.active_policy(events),
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
        events = ledger.events()
        x["history"] = [e for e in events if e.get("case") == gov.case_id(x)]
        return x

    @app.post("/api/governance/cases/{cid}/decision")
    def governance_decide(cid: int, request: Request, payload: dict[str, Any] = Body(...)):
        """G4 숙련자 검토: approve(AI 판정 확정) 또는 reject(정정 판정·사유 코드·문제 대상어)."""
        who = require(request, "reviewer")
        x = followup_detail(cid)
        g = x["gov"]
        if payload.get("fingerprint") and payload["fingerprint"] != g["fingerprint"]:
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
        if g["decision"] and g["state"] != "stale":
            raise HTTPException(409, "이미 결정된 건입니다. 바꾸려면 먼저 롤백하세요.")
        pol = gov.active_policy(ledger.events())
        ev = ledger.append("decision", who["name"], who["role"], {
            "action": action, "ai_status": x["ai_status"], "final_status": final, "reason": reason,
            "keywords": kws, "note": str(payload.get("note") or "")[:500], "fingerprint": g["fingerprint"],
            "path": g["path"], "risk_score": g["risk"]["score"], "risk_factors": [f["code"] for f in g["risk"]["factors"]],
            "policy_version": pol["version"], "subject": {"org": x["org"], "date": x["date"], "text": x["text"][:120]},
        }, case=gov.case_id(x))
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
        ev = ledger.append("rollback", who["name"], who["role"],
                           {"target": target["seq"], "target_action": target["payload"]["action"], "reason": reason[:500]},
                           case=cid_s)
        return {"event": ev, "case": governance_case(cid)}

    @app.get("/api/governance/ledger")
    def governance_ledger(kind: str = "", case: str = "", limit: int = Query(200, le=2000)):
        """운영 증거 원장(최근 순)과 해시 연결 검증."""
        events = ledger.events()
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
                    "ai_status", "final_status", "agree", "reason", "flagged_keywords", "stale", "org", "action", "promise_date"]
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
        p, pol = find_proposal(pid)
        new = gov.adopt(pol, p)
        ev = ledger.append("policy", who["name"], who["role"], {
            "policy": new, "previous_version": pol["version"], "proposal": p["id"], "change": p["title"],
            "rationale": p["rationale"], "support": p["support"], "note": str(payload.get("note") or "")[:500]})
        return {"event": ev, "policy": new}

    @app.post("/api/governance/proposals/{pid}/dismiss")
    def governance_dismiss(pid: str, request: Request, payload: dict[str, Any] = Body(default={})):
        who = require(request, "admin")
        p, _ = find_proposal(pid)
        reason = str(payload.get("reason") or "").strip()
        if not reason:
            raise HTTPException(400, "기각 사유를 적으세요.")
        ev = ledger.append("proposal_dismissed", who["name"], who["role"],
                           {"proposal": p["id"], "title": p["title"], "reason": reason[:500]})
        return {"event": ev}

    @app.get("/api/governance/report.md")
    def governance_report():
        """모든 관문의 운영 증거 보고서(Markdown)."""
        items = current_items()
        events = ledger.events()
        return PlainTextResponse(gov.evidence_report(items, events, gov_status()),
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
        b = analysis.briefing(store, org, issue, keyword, member, date_from, date_to,
                              session=session)
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
        graphs.invalidate()
        return {"parsed": ok, "pending": pending_count(store), "log": log}

    @app.post("/api/admin/sample")
    def admin_sample():
        from ..pipeline import load_sample
        n = load_sample(store)
        graphs.invalidate()
        return {"utterances": n}

    return app
