"""약속 이행의 외부 근거(실시간): 국회 의안 처리 현황, 지원사업 공고.

- 국회 의안: 열린국회정보 「국회의원 발의법률안」(nzmimeepazxkubdpn, ASSEMBLY_API_KEY와 같은 인증키).
  약속에 나온 법률 이름으로 찾아, 약속 뒤 발의된 개정안과 처리 결과(가결·계류·대안반영·폐기)를 근거로 삼는다.
- 지원사업 공고: 창업진흥원 K-Startup 조회서비스(공공데이터포털, KSTARTUP_API_KEY).
  약속의 대상어로 공고명을 찾아, 약속 뒤 접수를 시작한 공고를 근거로 삼는다.

두 API 모두 국내 접속만 허용하는 경우가 있어 서울 리전 배포본에서 호출한다. 인증키가 없거나 호출이 실패하면
그 출처만 건너뛰고(이유를 함께 돌려줌) 나머지 근거는 그대로 쓴다. 응답 필드명은 별칭까지 흡수한다.
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

import requests

from .api_client import AssemblyAPIError, AssemblyClient, normalize_date
from .config import Settings

BILL_API_URL = "https://open.assembly.go.kr/portal/openapi/nzmimeepazxkubdpn"
KSTARTUP_URL = "https://apis.data.go.kr/B552735/kisedKstartupService01/getAnnouncementInformation01"
TERM_22_START = "2024-05-30"

BILL_FIELDS: dict[str, tuple[str, ...]] = {
    "name": ("BILL_NAME", "BILL_NM"),
    "no": ("BILL_NO",),
    "proposer": ("PROPOSER", "RST_PROPOSER", "PPSR_NM"),
    "date": ("PROPOSE_DT", "PPSL_DT", "PROPOSE_DATE"),
    "result": ("PROC_RESULT", "PROC_RESULT_CD", "PROC_RSLT"),
    "committee": ("COMMITTEE", "CURR_COMMITTEE", "CMIT_NM"),
    "url": ("DETAIL_LINK", "LINK_URL", "BILL_URL"),
}
KSTARTUP_FIELDS: dict[str, tuple[str, ...]] = {
    "id": ("pbanc_sn", "pbancSn", "id"),
    "title": ("biz_pbanc_nm", "bizPbancNm", "pbanc_nm"),
    "org": ("pbanc_ntrp_nm", "pbancNtrpNm", "sprv_inst"),
    "start": ("pbanc_rcpt_bgng_dt", "pbancRcptBgngDt"),
    "end": ("pbanc_rcpt_end_dt", "pbancRcptEndDt"),
    "url": ("detl_pg_url", "detlPgUrl", "biz_gdnc_url"),
}


def _pick(row: dict[str, Any], keys: tuple[str, ...]) -> str:
    for k in keys:
        v = row.get(k)
        if v not in (None, ""):
            return str(v).strip()
    return ""


def _norm(row: dict[str, Any], fields: dict[str, tuple[str, ...]]) -> dict[str, str]:
    return {k: _pick(row, keys) for k, keys in fields.items()}


def _mask(text: str, *secrets: str) -> str:
    text = re.sub(r"((?:serviceKey|KEY)=)[^&\s]+", r"\1***", text)
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return text


# ── 국회 의안 ─────────────────────────────────────────────
def bill_kind(result: str) -> tuple[str, str]:
    """처리 결과 → (근거 종류, 설명). 가결 = 이행 단계, 계류·대안반영 = 진행, 폐기·철회·부결 = 참고."""
    r = result or ""
    if "가결" in r:
        return "done", f"{r} — 법 개정 가결"
    if not r or "대안반영" in r or "계류" in r or "심사" in r:
        return "progress", f"{r} — 위원회 대안에 반영" if "대안반영" in r else "계류 중(심사 진행)"
    return "related", r


def bills(settings: Settings, law: str, since: str, http: requests.Session | None = None) -> list[dict]:
    """since 이후 발의된, 이름에 law가 들어간 법률안."""
    age = "22" if since >= TERM_22_START else "21"
    client = AssemblyClient(replace(settings, api_url=BILL_API_URL), session=http, timeout=(5.0, 20.0), retries=1)
    page = client.fetch_page(1, 100, AGE=age, BILL_NAME=law)
    out = []
    for raw in page.rows:
        b = _norm(raw, BILL_FIELDS)
        b["date"] = normalize_date(b["date"])
        if law in b["name"] and b["date"] >= since:
            out.append(b)
    return sorted(out, key=lambda b: b["date"])


# ── 지원사업 공고(K-Startup) ───────────────────────────────
def parse_kstartup(text: str) -> list[dict[str, Any]]:
    """K-Startup 응답의 레코드 목록. 공공데이터포털은 JSON(`data` 또는 `response.body.items.item`)과
    XML(`<item><col name=…>` 또는 `<item><필드>`)을 섞어 돌려준다(support-radar/radar/kstartup.py와 같은 규칙)."""
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


def kstartup(api_key: str, keyword: str, since: str, http: requests.Session | None = None) -> list[dict]:
    """since 이후 접수를 시작한, 공고명에 keyword가 들어간 공고. 서버가 조건 검색을 무시해도 받은 뒤 다시 거른다."""
    http = http or requests.Session()
    params = {"serviceKey": api_key, "page": 1, "perPage": 50, "returnType": "json",
              "cond[biz_pbanc_nm::LIKE]": keyword,
              "cond[pbanc_rcpt_bgng_dt::GTE]": since.replace("-", "")}
    resp = http.get(KSTARTUP_URL, params=params, timeout=(5.0, 20.0))
    resp.raise_for_status()
    out = []
    for raw in parse_kstartup(resp.text):
        a = _norm(raw, KSTARTUP_FIELDS)
        a["start"], a["end"] = normalize_date(a["start"]), normalize_date(a["end"])
        if keyword in a["title"] and a["start"] >= since:
            out.append(a)
    return sorted(out, key=lambda a: a["start"])


def _search_terms(item: dict) -> list[str]:
    """공고 검색어: 세 글자 이상 대상어 우선 2개(두 글자 말로는 엉뚱한 공고가 많이 걸린다)."""
    kws = item.get("keywords", [])
    return ([k for k in kws if len(k) >= 3] or kws)[:2]


def external_evidence(settings: Settings, item: dict, http: requests.Session | None = None) -> dict[str, Any]:
    """약속 하나의 외부 근거. 반환: {fetched_at, sources:[{id,label,status,message,count}], evidence:[…]}"""
    sources, evidence = [], []
    since = item["date"]

    # 국회 의안
    src = {"id": "bill", "label": "국회 의안(열린국회정보)", "status": "ok", "message": "", "count": 0}
    if not item.get("laws"):
        src.update(status="skipped", message="약속에 법률 이름이 없어 찾지 않음")
    elif not settings.api_key:
        src.update(status="skipped", message="ASSEMBLY_API_KEY가 설정되지 않음")
    else:
        try:
            seen = set()
            for law in item["laws"][:2]:
                for b in bills(settings, law, since, http):
                    if b["no"] in seen:
                        continue
                    seen.add(b["no"])
                    kind, desc = bill_kind(b["result"])
                    evidence.append({"source": "bill", "kind": kind, "date": b["date"],
                                     "title": b["name"], "url": b["url"],
                                     "text": f"{b['proposer'] or '발의'} · {b['date']} 발의 · {desc}"})
            src["count"] = len(seen)
        except (AssemblyAPIError, requests.RequestException, ValueError) as e:
            src.update(status="error", message=_mask(str(e), settings.api_key))
    sources.append(src)

    # 지원사업 공고
    src = {"id": "kstartup", "label": "지원사업 공고(K-Startup)", "status": "ok", "message": "", "count": 0}
    terms = _search_terms(item)
    if item.get("action") not in ("budget", "improve", "review"):
        src.update(status="skipped", message="예산·지원·사업 약속이 아니어서 찾지 않음")
    elif not settings.kstartup_key:
        src.update(status="skipped", message="KSTARTUP_API_KEY가 설정되지 않음")
    elif not terms:
        src.update(status="skipped", message="검색할 대상어가 없음")
    else:
        try:
            seen = set()
            for kw in terms:
                for a in kstartup(settings.kstartup_key, kw, since, http):
                    if a["id"] in seen:
                        continue
                    seen.add(a["id"])
                    evidence.append({"source": "kstartup", "kind": "progress" if item["action"] == "budget" else "related",
                                     "date": a["start"], "title": a["title"], "url": a["url"],
                                     "text": f"{a['org'] or '공고 기관'} · 접수 {a['start']} ~ {a['end'] or '미정'} · 검색어 「{kw}」"})
            src["count"] = len(seen)
        except (requests.RequestException, RuntimeError, ValueError) as e:
            src.update(status="error", message=_mask(str(e), settings.kstartup_key))
    sources.append(src)

    evidence.sort(key=lambda e: e["date"] or "")
    return {"fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "sources": sources, "evidence": evidence[:12]}
