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
from ..lexicon import ISSUES, ORGANIZATIONS
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
        if org in ORGANIZATIONS and not (issue or keyword or member or date_from or date_to
                                         or session):
            # 기관만 고른 전체 회기 브리핑은 가장 무거워(발언 1만여 건) 저장해 재사용한다
            from ..precompute import briefing_key
            b = store.kv_get(briefing_key(org))
            if b is None:
                b = analysis.briefing(store, org)
                store.kv_set(briefing_key(org), b)
        else:
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
