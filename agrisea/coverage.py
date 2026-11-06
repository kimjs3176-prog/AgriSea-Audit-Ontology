"""데이터 현황: 무엇이 수록됐고 무엇이 빠졌는지, 얼마나 최신인지, 품질 점검 결과를 한곳에 모은다.

대시보드 「데이터 현황」(주소 #/data)과 /api/data-status 에서 쓴다. 가벼운 집계라서 사전 계산하지 않고
요청 때 계산한다(오늘 날짜에 따라 경과일·접수 중 공고가 바뀌므로).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from .store import Store

KST = timezone(timedelta(hours=9))
# .github/workflows/refresh-data.yml 의 예약 시각(한국시간)
REFRESH_SCHEDULE = ("07:17", "13:17", "19:17")
STALE_DAYS = 14   # 최신 회의가 이보다 오래되면 '새 회의록 없음'을 알린다


def _months(first: str, last: str) -> list[str]:
    y, m = int(first[:4]), int(first[5:7])
    ly, lm = int(last[:4]), int(last[5:7])
    out = []
    while (y, m) <= (ly, lm):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def data_status(store: Store, meta: dict | None = None, today: date | None = None) -> dict:
    meta = meta or {}
    today = today or datetime.now(KST).date()
    c = store.conn
    s = store.stats()
    one = lambda sql, *a: c.execute(sql, a).fetchone()[0] or 0  # noqa: E731

    sub = one("SELECT COUNT(*) FROM meetings WHERE title LIKE '%소위원회%'")
    audit = one("SELECT COUNT(*) FROM meetings WHERE title LIKE '%국정감사%' OR class_name LIKE '%국정감사%'")
    sessions = one("SELECT COUNT(DISTINCT session_no) FROM meetings WHERE session_no IS NOT NULL")

    by_month = {r[0]: (r[1], r[2]) for r in c.execute(
        "SELECT substr(date,1,7), COUNT(*), SUM(title LIKE '%소위원회%') FROM meetings "
        "WHERE date IS NOT NULL AND date != '' GROUP BY 1")}
    months = []
    if s["date_min"]:
        for mo in _months(s["date_min"][:7], max(s["date_max"][:7], today.isoformat()[:7])):
            n, n_sub = by_month.get(mo, (0, 0))
            months.append({"month": mo, "meetings": n, "subcommittee": n_sub or 0})
    last_month = (s["date_max"] or "")[:7]
    empty_months = [m["month"] for m in months if not m["meetings"] and m["month"] < last_month]

    text_status = {r[0] or "none": r[1] for r in c.execute(
        "SELECT text_status, COUNT(*) FROM meetings GROUP BY 1")}
    pending = [dict(r) for r in c.execute(
        "SELECT id, date, title, text_status AS status FROM meetings "
        "WHERE COALESCE(text_status, '') != 'parsed' ORDER BY date DESC LIMIT 20")]

    other = one("SELECT COUNT(*) FROM utterances WHERE speaker_type='other'")
    other_roles = [{"role": r[0], "count": r[1]} for r in c.execute(
        "SELECT speaker_role, COUNT(*) FROM utterances WHERE speaker_type='other' "
        "GROUP BY 1 ORDER BY 2 DESC LIMIT 5")]

    t = today.isoformat()
    programs = {
        "total": s["programs"],
        "open": one("SELECT COUNT(*) FROM programs WHERE start_date <= ? AND end_date >= ?", t, t),
        "latest_start": c.execute("SELECT MAX(start_date) FROM programs").fetchone()[0],
    }

    days_since = None
    if s["date_max"]:
        try:
            days_since = (today - date.fromisoformat(s["date_max"][:10])).days
        except ValueError:
            pass

    report = store.kv_get("ontology_validation")
    quality = None
    if report:
        quality = {
            "as_of": report.get("as_of"),
            "counts": report.get("counts", {}),
            "issues": [{k: ch.get(k) for k in ("group", "title", "severity", "count", "total")}
                       for ch in report.get("checks", []) if ch.get("severity") != "pass"],
        }

    # 질의 분류(지식그래프 사전 계산 요약): 기관 대상 질의 / 회의 진행 질의 / 진술인·참고인 질의
    cls = {c.get("id"): c.get("count", 0) for c in (store.kv_get("ontology") or {}).get("classes", [])}
    questions = None
    if cls.get("Question"):
        proc, expert = cls.get("ProceduralQuestion", 0), cls.get("ExpertQuestion", 0)
        unresolved = next((ch.get("count") for ch in (report or {}).get("checks", [])
                           if ch.get("title", "").startswith("질의는 어느 기관을 향한다")), None)
        props = {p.get("id"): p.get("count", 0) for p in (store.kv_get("ontology") or {}).get("properties", [])}
        questions = {"total": cls["Question"], "procedural": proc, "expert": expert,
                     "agency": cls["Question"] - proc - expert, "agency_unresolved": unresolved,
                     "agency_estimated": props.get("likelyAddressedTo", 0)}

    gaps = []
    if s["samples"]:
        gaps.append({"level": "warn", "title": f"가상 예시 회의록 {s['samples']}건 포함",
                     "detail": "시연용 데이터가 섞여 있습니다. 실제 분석에는 쓰지 마세요."})
    if s["meetings"] and not audit:
        gaps.append({"level": "warn", "title": "국정감사 회의록은 아직 수록되지 않았습니다",
                     "detail": "열린국회정보 「위원회 회의록」 API로 상임위·소위원회 회의만 모읍니다. "
                               "국정감사 회의록은 별도 서비스라 국감 당일 질의·답변은 국회 회의록 시스템에서 확인하세요."})
    if pending:
        gaps.append({"level": "warn", "title": f"본문을 아직 받지 못한 회의 {len(pending)}건",
                     "detail": "목록만 있고 발언이 없어 검색·요약·이행 추적에서 빠집니다. 다음 자동 수집 때 다시 받습니다."})
    if days_since is not None and days_since >= STALE_DAYS:
        gaps.append({"level": "info", "title": f"최근 {days_since}일간 새 회의록 없음",
                     "detail": f"자동 수집은 매일 {'·'.join(REFRESH_SCHEDULE)}에 확인합니다. "
                               "회의록은 회의 뒤 정리를 거쳐 공개되므로 반영이 늦을 수 있습니다."})
    if empty_months:
        gaps.append({"level": "info", "title": f"회의가 수록되지 않은 달 {len(empty_months)}개",
                     "detail": ", ".join(m.replace("-", ".") for m in empty_months)
                               + " — 휴회 등으로 회의가 없었을 수 있습니다."})
    if other:
        gaps.append({"level": "info", "title": f"발언자 유형을 정하지 못한 발언 {other:,}건",
                     "detail": "직함: " + ", ".join(f"{r['role']} {r['count']:,}건" for r in other_roles)})
    if not s["programs"]:
        gaps.append({"level": "info", "title": "지원사업 공고가 아직 없습니다",
                     "detail": "공공데이터포털 인증키(KSTARTUP_API_KEY)가 있어야 수집합니다."})

    return {
        "as_of": t,
        "range": {
            "date_min": s["date_min"], "date_max": s["date_max"], "sessions": sessions,
            "meetings": s["meetings"], "plenary": s["meetings"] - sub, "subcommittee": sub,
            "audit": audit, "parsed": s["parsed"], "samples": s["samples"],
            "utterances": s["utterances"], "questions": s["questions"], "commitments": s["commitments"],
        },
        "freshness": {
            "latest_meeting": s["date_max"], "days_since": days_since,
            "data_updated": meta.get("exported_at"), "schedule": list(REFRESH_SCHEDULE),
        },
        "months": months,
        "text_status": text_status,
        "pending": pending,
        "speakers": {"other": other, "other_roles": other_roles},
        "programs": programs,
        "kg": {"triples": meta.get("kg_triples"), "inferred": meta.get("kg_inferred")},
        "quality": quality,
        "questions": questions,
        "gaps": gaps,
    }
