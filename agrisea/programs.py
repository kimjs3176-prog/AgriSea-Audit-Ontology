"""지원사업 공고(창업진흥원 K-Startup 조회서비스, 공공데이터포털)를 회의록 온톨로지에 잇는다.

공고 전체를 최신순으로 받아(서버가 조건 검색을 무시하므로 받은 뒤 거른다) 농림·해양수산과 관련된 공고만
`programs` 표에 저장한다. 관련 여부와 쟁점은 회의록과 같은 쟁점 체계(lexicon.ISSUES)로 정하고,
분야는 창업 지원 공고에 맞춘 말(TOPICS: 푸드테크·그린바이오 등)로 정한다. 회의록용 쟁점 말('R&D'·'관세'·'물류')은
창업 공고에서는 너무 넓어 쓰지 않는다.
저장한 공고는 쟁점 상세(국회 관심 대비 공고 공급), 기관 브리핑, 대시보드,
지식그래프(ag:SupportProgram)에서 쓴다.
"""
from __future__ import annotations

import json
import re
import time
from datetime import date
from typing import Any, Callable

import defusedxml.ElementTree as ET  # 표준 xml 대신: 외부 엔티티(XXE)·XML 폭탄 차단
import requests

from .api_client import normalize_date
from .config import TERM_START, Settings
from .lexicon import ISSUES, ORGANIZATIONS

API_URL = "https://apis.data.go.kr/B552735/kisedKstartupService01/getAnnouncementInformation01"
PAGE_SIZE = 500
MAX_PAGES = 120          # 전체 3만여 건 / 500
CONTENT_LIMIT = 3000     # 공고 본문은 앞부분만 보관(seed 크기)

FIELDS: dict[str, tuple[str, ...]] = {
    "id": ("pbanc_sn", "pbancSn"),
    "title": ("biz_pbanc_nm", "bizPbancNm"),
    "program": ("intg_pbanc_biz_nm", "intgPbancBizNm"),
    "org": ("pbanc_ntrp_nm", "pbancNtrpNm"),
    "supervisor": ("sprv_inst", "sprvInst"),
    "category": ("supt_biz_clsfc", "suptBizClsfc"),
    "target": ("aply_trgt", "aplyTrgt"),
    "region": ("supt_regin", "suptRegin"),
    "start": ("pbanc_rcpt_bgng_dt", "pbancRcptBgngDt"),
    "end": ("pbanc_rcpt_end_dt", "pbancRcptEndDt"),
    "url": ("detl_pg_url", "detlPgUrl", "biz_gdnc_url"),
    "content": ("pbanc_ctnt", "pbancCtnt"),
}

# 창업 지원 분야 말 → 회의록 쟁점. 공고는 창업 전반이라 넓은 말('수출'·'AI')은 농식품 맥락이 붙은 표현만 쓴다.
TOPICS: dict[str, tuple[str, tuple[str, ...]]] = {
    "smartagri": ("스마트농업·R&D", ("스마트팜", "스마트농업", "수직농장", "식물공장", "정밀농업", "농업용 드론",
                                  "농업 로봇", "자율주행 농기계", "애그테크", "어그테크", "농업 AI", "농업 인공지능",
                                  "농업 데이터", "디지털 농업", "그린바이오", "종자산업", "곤충산업", "동물용의약품",
                                  "디지털육종", "농식품 기술", "농업기술")),
    "foodsafety": ("식품·통상·검역", ("푸드테크", "대체식품", "대체육", "배양육", "식물성 단백질", "간편식",
                                   "밀키트", "식품 스타트업", "식품산업", "K-푸드", "K푸드", "케이푸드",
                                   "농식품 수출", "식품 수출", "농산물 수출", "전통주", "외식산업")),
    "rural": ("농촌소멸·농업인력", ("청년농", "청년 농업인", "청년농업인", "후계농", "영농창업", "농업 창업",
                                  "농식품 창업", "농산업 창업", "귀농", "귀촌", "농촌 융복합", "농촌융복합",
                                  "6차산업", "농촌관광", "농촌 창업")),
    "price": ("농산물 가격·유통", ("온라인도매시장", "농산물 유통", "산지유통", "로컬푸드", "농산물 직거래",
                                "농산물 판로", "농식품 판로")),
    "fishery": ("수산업·어촌", ("블루푸드", "스마트양식", "수산식품", "양식산업", "해양수산 창업", "수산 스타트업",
                             "해양수산 스타트업", "어촌 창업", "수산물 수출", "해양바이오")),
    "disaster": ("기후위기·농업재해", ("영농형 태양광", "농업 에너지", "저탄소 농업", "농업 탄소")),
    "animal": ("동물복지·반려동물", ("반려동물", "펫푸드", "펫테크", "펫산업", "동물복지")),
    "forest": ("산림·산불", ("산림바이오", "임산물", "목재산업", "임업", "산림 창업")),
    "maritime": ("해운·항만·물류", ("해운 스타트업", "항만 스타트업", "스마트항만", "해양모빌리티")),
}
_SPACE = re.compile(r"\s+")
_TOPIC_KW = {iid: tuple(sorted({_SPACE.sub("", k) for k in kws}, key=len, reverse=True))
             for iid, (_, kws) in TOPICS.items()}
# 주관·전담 기관이 피감기관(별칭 포함)이면 그 기관 공고로 본다
_ORG_NAMES = sorted(((n, a) for n, al in ORGANIZATIONS.items() for a in (n, *al) if len(a) >= 3),
                    key=lambda x: -len(x[1]))


SEA_AGENCIES = {"해양수산부", "해양경찰청", "국립수산물품질관리원", "국립수산과학원", "수산업협동조합중앙회",
                "한국해양진흥공사", "해양환경공단", "한국수산자원공단", "한국해양조사협회", "한국어촌어항공단",
                "부산항만공사", "인천항만공사", "여수광양항만공사", "울산항만공사", "한국해양과학기술원",
                "한국해양수산개발원", "국립해양조사원"}


class ProgramError(RuntimeError):
    pass


def _pick(raw: dict[str, Any], keys: tuple[str, ...]) -> str:
    for k in keys:
        v = raw.get(k)
        if v not in (None, ""):
            return re.sub(r"\s+", " ", str(v)).strip()
    return ""


def agency_of(a: dict) -> str | None:
    """공고 기관(주관·전담)이 피감기관이면 그 정규명."""
    text = f"{a.get('org', '')} {a.get('supervisor', '')}"
    return next((name for name, alias in _ORG_NAMES if alias in text), None)


def classify(a: dict) -> dict[str, int]:
    """공고의 쟁점(회의록과 같은 체계). 제목·사업명에 나오면 바로, 본문에만 나오면 2번 이상일 때."""
    head = _SPACE.sub("", f"{a.get('title', '')} {a.get('program', '')}")
    body = _SPACE.sub("", a.get("content", ""))
    out: dict[str, int] = {}
    for iid, kws in _TOPIC_KW.items():
        h, b = sum(head.count(k) for k in kws), sum(body.count(k) for k in kws)
        if h or b >= 2:
            out[iid] = h * 3 + b
    return out


def normalize(raw: dict[str, Any]) -> dict[str, Any] | None:
    """API 레코드 → 저장 형식. 농림·해양수산과 관련 없는 공고는 None."""
    a = {k: _pick(raw, v) for k, v in FIELDS.items()}
    if not a["id"] or not a["title"]:
        return None
    a["start"], a["end"] = normalize_date(a["start"]), normalize_date(a["end"])
    a["content"] = a["content"][:CONTENT_LIMIT]
    a["agency"] = agency_of(a)
    a["issues"] = classify(a)
    if not (a["issues"] or a["agency"]):
        return None
    if a["agency"] and not a["issues"]:  # 피감기관 공고인데 분야 말이 없으면 기관 쪽 최상위 쟁점으로
        a["issues"] = {"sea" if a["agency"] in SEA_AGENCIES else "agri": 1}
    return a


def status(a: dict, today: str | None = None) -> str:
    """접수 상태: open(접수 중) · upcoming(예정) · closed(마감)."""
    today = today or date.today().isoformat()
    if a.get("end") and a["end"] < today:
        return "closed"
    if a.get("start") and a["start"] > today:
        return "upcoming"
    return "open" if a.get("end") else "closed"


def parse_kstartup(text: str) -> list[dict[str, Any]]:
    """K-Startup 응답의 레코드 목록. 공공데이터포털은 JSON(`data` 또는 `response.body.items.item`)과
    XML(`<item><col name=…>` 또는 `<item><필드>`)을 섞어 돌려준다."""
    body = (text or "").strip().lstrip("\ufeff")
    if not body:
        raise RuntimeError("K-Startup 빈 응답")
    if body.startswith("<"):
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            raise RuntimeError(f"K-Startup XML 해석 실패: {body[:120]}") from None
        auth = root.find(".//returnAuthMsg")  # 공공데이터포털 게이트웨이 오류(인증키 등)
        if auth is not None:
            raise RuntimeError(f"공공데이터포털 오류 {root.findtext('.//returnReasonCode') or ''}: {auth.text}")
        code = root.findtext(".//resultCode")
        if code and code not in ("00", "0", "200", "INFO-000"):
            raise RuntimeError(f"K-Startup 오류 {code}: {root.findtext('.//resultMsg') or ''}")
        rows = []
        for item in root.iter("item"):
            cols = item.findall("col")
            rows.append({c.get("name", ""): (c.text or "") for c in cols} if cols
                        else {child.tag: (child.text or "") for child in item})
        return rows
    try:
        doc = json.loads(body)
    except ValueError:
        raise RuntimeError(f"K-Startup 응답 해석 실패: {body[:120]}") from None
    if isinstance(doc, dict) and "data" in doc:
        data = doc["data"] or []
    elif isinstance(doc, dict) and "response" in doc:
        header = doc["response"].get("header", {})
        if header.get("resultCode") not in (None, "00", "0"):
            raise RuntimeError(f"K-Startup 오류 {header.get('resultCode')}: {header.get('resultMsg', '')}")
        items = doc["response"].get("body", {}).get("items") or {}
        data = items.get("item", []) if isinstance(items, dict) else items
        data = [data] if isinstance(data, dict) else data
    elif isinstance(doc, dict) and "code" in doc and "msg" in doc:
        raise RuntimeError(f"K-Startup 오류 {doc.get('code')}: {doc.get('msg')}")
    else:
        raise RuntimeError(f"K-Startup 응답 형식을 알 수 없음: {body[:120]}")
    return [d for d in data if isinstance(d, dict)]


def fetch(settings: Settings, since: str = TERM_START, http: requests.Session | None = None,
          progress: Callable[[str], None] = print, sleep: Callable[[float], None] = time.sleep) -> list[dict]:
    """since 이후 접수를 시작한 공고 중 농림·해양수산 관련 공고. 최신순으로 받다가 since보다 오래되면 멈춘다.

    공공데이터포털이 해외 접속을 막으면 KSTARTUP_PROXY(서울 리전 배포본의 /api/kstartup)를 거친다."""
    http = http or requests.Session()
    proxy = settings.kstartup_proxy.rstrip("/")
    if not (settings.kstartup_key or proxy):
        raise ProgramError("KSTARTUP_API_KEY(공공데이터포털 인증키)가 설정되지 않았습니다.")
    url = proxy or API_URL
    out: dict[str, dict] = {}
    seen = 0
    for page in range(1, MAX_PAGES + 1):
        params: dict[str, Any] = {"page": page, "perPage": PAGE_SIZE, "returnType": "json"}
        if not proxy:
            params["serviceKey"] = settings.kstartup_key
        rows = None
        for attempt in range(4):
            try:
                r = http.get(url, params=params, timeout=(10, 90))
                if r.status_code >= 500 or r.status_code == 429:
                    raise ProgramError(f"HTTP {r.status_code}")
                r.raise_for_status()
                rows = parse_kstartup(r.text)
                break
            except (requests.RequestException, ProgramError, RuntimeError) as e:
                if attempt == 3 or (isinstance(e, RuntimeError) and "오류" in str(e)):
                    msg = str(e).replace(settings.kstartup_key, "***") if settings.kstartup_key else str(e)
                    raise ProgramError(re.sub(r"(serviceKey=)[^&\s]+", r"\1***", f"{page}쪽 요청 실패: {msg}")) from None
                sleep(2 ** (attempt + 1))
        seen += len(rows)
        starts = []
        for raw in rows:
            a = normalize(raw)
            start = normalize_date(_pick(raw, FIELDS["start"]))
            starts.append(start)
            if a and (not a["start"] or a["start"] >= since):
                out[a["id"]] = a
        if page % 5 == 0:
            progress(f"공고 {seen}건 확인, 관련 {len(out)}건")
        valid = [d for d in starts if d]
        if len(rows) < PAGE_SIZE or (valid and max(valid) < since):  # 이 쪽 전체가 since 이전 → 끝
            break
    progress(f"공고 {seen}건 확인, 농림·해양수산 관련 {len(out)}건")
    return list(out.values())


def collect(store, settings: Settings, since: str = TERM_START, progress: Callable[[str], None] = print,
            http: requests.Session | None = None) -> int:
    rows = fetch(settings, since, http=http, progress=progress)
    return store.save_programs(rows)


# ── 화면·분석용 ─────────────────────────────────────────────
def brief(a: dict, today: str | None = None) -> dict:
    return {"id": a["id"], "title": a["title"], "org": a["org"], "agency": a["agency"], "start": a["start"],
            "end": a["end"], "url": a["url"], "category": a["category"], "region": a["region"],
            "status": status(a, today), "issues": list(a["issues"])}


# 공고 제목에 흔한 말이라 대상어로 맞춰도 근거가 안 되는 말
QUADRANTS = {
    "gap": ("공백·기회", "국회 관심은 높은데 공고가 적음 → 신규·확대 공고나 후속 조치를 물어볼 만함"),
    "active": ("활발", "국회 관심과 공고가 함께 많음"),
    "supply": ("공고 우위", "공고는 많지만 국회 논의는 적음"),
    "latent": ("잠재", "국회 관심·공고 모두 적음"),
    "nodata": ("공고 수집 전", "지원사업 공고를 받은 뒤 판정"),
}


def _rollup(counts: dict[str, int]) -> dict[str, int]:
    """하위 쟁점 수를 상위 쟁점에도 더한다(상위 쟁점 화면은 하위 전체를 본다)."""
    out = dict(counts)
    for iid, n in counts.items():
        parent = ISSUES.get(iid, (None, None))[1]
        if parent:
            out[parent] = out.get(parent, 0) + n
    return out


def supply_signals(store, today: str | None = None) -> dict:
    """쟁점별 국회 관심(최근 12개월 발언)과 공고 공급(최근 12개월 공고)을 견줘 공백·활발 등을 판정."""
    import json
    from datetime import timedelta
    today = today or date.today().isoformat()
    progs = store.programs()
    last = store.conn.execute("SELECT MAX(date) FROM meetings WHERE is_sample=0").fetchone()[0] or today
    since_talk = (date.fromisoformat(last) - timedelta(days=365)).isoformat()
    since_prog = (date.fromisoformat(today) - timedelta(days=365)).isoformat()
    talk: dict[str, int] = {}
    for (js,) in store.conn.execute(
            "SELECT u.issues_json FROM utterances u JOIN meetings m ON m.id=u.meeting_id "
            "WHERE m.date >= ? AND m.is_sample=0 AND u.issues_json != '{}'", (since_talk,)):
        for iid in json.loads(js):
            talk[iid] = talk.get(iid, 0) + 1
    talk = _rollup(talk)
    supply = _rollup({iid: sum(1 for a in progs if iid in a["issues"] and (a["start"] or "") >= since_prog)
                      for iid in ISSUES})
    total = _rollup({iid: sum(1 for a in progs if iid in a["issues"]) for iid in ISSUES})
    opened = _rollup({iid: sum(1 for a in progs if iid in a["issues"] and status(a, today) == "open")
                      for iid in ISSUES})
    leaves = [i for i in ISSUES if i != "gov" and not any(v[1] == i for v in ISSUES.values())]
    rank = lambda d: {i: r / max(1, len(leaves) - 1) for r, i in enumerate(sorted(leaves, key=lambda i: d.get(i, 0)))}
    tr, sr = rank(talk), rank(supply)
    out = {}
    for iid in ISSUES:
        if iid == "gov":
            continue
        kids = [i for i in leaves if i == iid or ISSUES[i][1] == iid]
        t = sum(tr[i] for i in kids) / len(kids)
        s = sum(sr[i] for i in kids) / len(kids)
        q = ("nodata" if not progs else "gap" if t > .5 >= s else "active" if t > .5 and s > .5
             else "supply" if s > .5 else "latent")
        out[iid] = {"talk_12m": talk.get(iid, 0), "programs_12m": supply.get(iid, 0),
                    "programs_total": total.get(iid, 0), "open": opened.get(iid, 0),
                    "quadrant": q, "label": QUADRANTS[q][0], "desc": QUADRANTS[q][1]}
    return {"as_of": today, "talk_since": since_talk, "issues": out}


def issue_view(store, issue_id: str, signals: dict | None = None, limit: int = 12) -> dict:
    """쟁점 상세의 지원사업 공고: 관심 대비 공급 신호와 최근 공고(접수 중 먼저)."""
    today = date.today().isoformat()
    rows = store.programs(issue=issue_id)
    briefs = [brief(a, today) for a in rows]
    briefs.sort(key=lambda b: (b["status"] != "open", b["status"] != "upcoming", ""), reverse=False)
    sig = (signals or supply_signals(store, today))["issues"].get(issue_id)
    return {"signal": sig, "total": len(rows), "items": briefs[:limit]}


def agency_view(store, agency: str, limit: int = 20) -> dict:
    """기관 브리핑의 지원사업 공고: 그 기관이 주관·전담한 공고."""
    today = date.today().isoformat()
    rows = store.programs(agency=agency)
    return {"total": len(rows), "open": sum(1 for a in rows if status(a, today) == "open"),
            "items": [brief(a, today) for a in rows[:limit]]}


def overview(store, limit: int = 6) -> dict:
    """대시보드: 접수 중 공고 수와 마감이 가까운 공고."""
    today = date.today().isoformat()
    rows = [brief(a, today) for a in store.programs()]
    open_ = sorted((b for b in rows if b["status"] == "open"), key=lambda b: b["end"] or "9999")
    return {"total": len(rows), "open": len(open_), "upcoming": sum(1 for b in rows if b["status"] == "upcoming"),
            "closing": open_[:limit], "latest": rows[:limit]}
