"""FastAPI 웹 서비스: 맞춤 매칭·자격 판정 API, 정책 레이더 API, 단일 페이지 UI."""
from __future__ import annotations

import re
import time
from datetime import date
from pathlib import Path
from typing import Any

import requests
from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from .. import __version__
from ..catalog import Catalog
from ..config import COLLECT_SINCE, Settings, get_settings
from ..eligibility import CATEGORIES, REGIONS, STAGES, Profile, evaluate, match
from ..kstartup import OPS, KStartupError, mask_key, status
from ..policy import QUADRANTS, background, radar, topic_detail
from ..topics import TOPICS

STATIC = Path(__file__).parent / "static"
REFRESH_COOLDOWN = 600   # 화면에서 공고를 새로 받는 간격(초): 인증키 사용량 보호


def create_app(settings: Settings | None = None, catalog: Catalog | None = None) -> FastAPI:
    settings = settings or get_settings()
    startup_error = ""
    if catalog is None:
        try:
            catalog = Catalog.load(settings)
        except Exception as e:  # 데이터를 못 읽어도 화면·진단은 뜨도록
            startup_error = f"{type(e).__name__}: {e}"
            catalog = Catalog()
    app = FastAPI(title="지원사업 레이더", version=__version__)
    app.state.catalog = catalog
    last_refresh = {"at": 0.0}

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/health")
    def health():
        return {"ok": not startup_error, "startup_error": startup_error, "version": __version__,
                "api_key": bool(settings.api_key), "proxy": bool(settings.proxy_url),
                "llm": settings.llm_enabled, "serverless": settings.serverless,
                "announcements": len(catalog.announcements), "signals": catalog.signals is not None}

    @app.get("/api/meta")
    def meta():
        return {**catalog.stats(), "regions": REGIONS, "categories": CATEGORIES, "stages": STAGES,
                "topics": [{"id": t, "label": v[0], "desc": v[1]} for t, v in TOPICS.items()],
                "quadrants": {k: {"label": v[0], "desc": v[1]} for k, v in QUADRANTS.items()},
                "can_refresh": bool(settings.api_key or settings.proxy_url),
                "today": date.today().isoformat()}

    # ── 맞춤 매칭 ────────────────────────────────────
    @app.post("/api/match")
    def match_api(profile: dict[str, Any] = Body(...), limit: int = Query(200, le=1000)):
        p = Profile.from_dict(profile)
        out = match(catalog.announcements, p, limit=limit)
        sig = catalog.signals or {}
        r = {row["id"]: row for row in radar(sig, catalog.announcements)["topics"]} if sig else {}
        for res in out["results"]:   # 공고 주제의 국회 신호(공백·활발 등)
            res["signals"] = [{"topic": t, "label": r[t]["label"], "quadrant": r[t]["quadrant"],
                               "momentum": r[t]["momentum"]} for t in res["topics"] if t in r]
        return out

    # ── 공고 ──────────────────────────────────────────
    @app.get("/api/announcements")
    def announcements(q: str = "", category: str = "", topic: str = "", region: str = "",
                      state: str = Query("open", pattern="^(open|upcoming|closed|all)$"),
                      limit: int = Query(50, le=500), offset: int = 0):
        rows = catalog.announcements
        if state != "all":
            rows = [a for a in rows if status(a) == state]
        if q:
            terms = [t.lower() for t in q.split()]
            rows = [a for a in rows if all(t in f"{a['title']} {a.get('program', '')} {a.get('org', '')} "
                                                 f"{a.get('content', '')}".lower() for t in terms)]
        if category:
            rows = [a for a in rows if category.split("(")[0] in a.get("category", "")]
        if topic:
            rows = [a for a in rows if topic in a.get("topics", [])]
        if region:
            rows = [a for a in rows if region in a.get("region", "") or "전국" in a.get("region", "")]
        rows = sorted(rows, key=lambda a: (a.get("end") or "9999") if state == "open"
                      else a.get("start", ""), reverse=state != "open")
        keys = ("id", "title", "org", "category", "region", "start", "end", "url", "topics")
        return {"total": len(rows),
                "items": [{**{k: a.get(k, "") for k in keys}, "status": status(a)}
                          for a in rows[offset:offset + limit]]}

    @app.get("/api/announcements/{aid}")
    def announcement(aid: str, request: Request):
        a = catalog.by_id.get(aid)
        if not a:
            raise HTTPException(404, "공고를 찾을 수 없습니다.")
        out = {**a, "status": status(a), "background": background(catalog.signals, a)}
        if request.query_params:   # 프로필을 쿼리로 주면 자격 판정 포함
            out["evaluation"] = evaluate(a, Profile.from_dict(dict(request.query_params)))
        return out

    # ── 정책 레이더 ───────────────────────────────────
    @app.get("/api/radar")
    def radar_api():
        return radar(catalog.signals, catalog.announcements)

    @app.get("/api/radar/{tid}")
    def radar_topic(tid: str):
        if tid not in TOPICS:
            raise HTTPException(404, "알 수 없는 주제입니다.")
        return topic_detail(catalog.signals, catalog.announcements, tid)

    # ── 수집 ──────────────────────────────────────────
    @app.post("/api/admin/refresh")
    def refresh(full: bool = False):
        """K-Startup에서 접수 중 공고(full=true면 국회 데이터와 같은 기간 전체)를 새로 받는다."""
        if not (settings.api_key or settings.proxy_url):
            raise HTTPException(400, "KSTARTUP_API_KEY가 설정되지 않았습니다.")
        wait = REFRESH_COOLDOWN - (time.time() - last_refresh["at"])
        if wait > 0:
            raise HTTPException(429, f"방금 갱신했습니다. {int(wait)}초 뒤 다시 시도하세요.")
        last_refresh["at"] = time.time()
        try:
            result = catalog.refresh(settings, since=COLLECT_SINCE if full else "",
                                     open_only=not full, max_pages=40 if settings.serverless else 400)
        except KStartupError as e:
            raise HTTPException(502, str(e))
        try:
            catalog.write(settings.catalog_path)
        except OSError as e:
            result["save_error"] = str(e)
        return result

    @app.get("/api/kstartup/{op}")
    def kstartup_proxy(op: str, request: Request):
        """자동 수집용 중계: 공공데이터포털이 해외(GitHub Actions) 접속을 막을 때 서울 리전에서 대신 받는다.
        조회 오퍼레이션 두 개만, 인증키는 서버에서 붙인다."""
        if op not in OPS.values():
            raise HTTPException(404, "지원하지 않는 오퍼레이션입니다.")
        if not settings.api_key:
            raise HTTPException(400, "KSTARTUP_API_KEY가 설정되지 않았습니다.")
        params = {k: v for k, v in request.query_params.items()
                  if k in ("page", "perPage", "returnType") or re.fullmatch(r"cond\[[a-z_]+::[A-Z]+\]", k)}
        if int(params.get("perPage", "10") or 10) > 500:
            params["perPage"] = "500"
        params["serviceKey"] = settings.api_key
        try:
            r = requests.get(f"{settings.api_url.rstrip('/')}/{op}", params=params, timeout=40)
        except requests.RequestException as e:
            return JSONResponse({"error": mask_key(str(e), settings.api_key)}, status_code=502)
        return Response(r.content, status_code=r.status_code,
                        media_type=r.headers.get("content-type", "application/json"))

    return app
