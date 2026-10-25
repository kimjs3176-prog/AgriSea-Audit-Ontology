"""약속 이행의 외부 근거: 국회 의안 처리 현황(실시간), 지원사업 공고(수집해 둔 목록).

- 국회 의안: 열린국회정보 「국회의원 발의법률안」(nzmimeepazxkubdpn, ASSEMBLY_API_KEY와 같은 인증키).
  약속에 나온 법률 이름으로 찾아, 약속 뒤 발의된 개정안과 처리 결과(가결·계류·대안반영·폐기)를 근거로 삼는다.
  국내 접속만 허용하는 경우가 있어 서울 리전 배포본에서 호출한다.
- 지원사업 공고: 자동 수집이 공공데이터포털(K-Startup)에서 받아 둔 공고(programs.py)에서 약속의 대상어로 찾는다.

인증키가 없거나 호출이 실패하면 그 출처만 건너뛰고(이유를 함께 돌려줌) 나머지 근거는 그대로 쓴다.
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
TERM_22_START = "2024-05-30"
# 요청 한 번(서버리스 60초 제한) 안에 끝나도록 출처마다 읽는 쪽 수의 상한
BILL_PAGES, PAGE_SIZE = 3, 100

BILL_FIELDS: dict[str, tuple[str, ...]] = {
    "name": ("BILL_NAME", "BILL_NM"),
    "no": ("BILL_NO",),
    "proposer": ("PROPOSER", "RST_PROPOSER", "PPSR_NM"),
    "date": ("PROPOSE_DT", "PPSL_DT", "PROPOSE_DATE"),
    "result": ("PROC_RESULT", "PROC_RESULT_CD", "PROC_RSLT"),
    "committee": ("COMMITTEE", "CURR_COMMITTEE", "CMIT_NM"),
    "url": ("DETAIL_LINK", "LINK_URL", "BILL_URL"),
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
    rows: list[dict] = []
    for no in range(1, BILL_PAGES + 1):  # 개정안이 100건을 넘는 법률도 있어 여러 쪽을 읽는다
        page = client.fetch_page(no, PAGE_SIZE, AGE=age, BILL_NAME=law)
        rows += page.rows
        if len(page.rows) < PAGE_SIZE or len(rows) >= page.total:
            break
    out = []
    for raw in rows:
        b = _norm(raw, BILL_FIELDS)
        b["date"] = normalize_date(b["date"])
        if law in b["name"] and b["date"] >= since:
            out.append(b)
    return sorted(out, key=lambda b: b["date"])


# ── 지원사업 공고(K-Startup) ───────────────────────────────
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


def external_evidence(settings: Settings, item: dict, http: requests.Session | None = None,
                      programs: list[dict] | None = None) -> dict[str, Any]:
    """약속 하나의 외부 근거. 반환: {fetched_at, sources:[{id,label,status,message,count}], evidence:[…]}"""
    sources, evidence = [], []
    since = item["date"]

    # 국회 의안
    src = {"id": "bill", "label": "국회 의안(열린국회정보)", "status": "ok", "message": "", "count": 0}
    if not item.get("laws"):
        src.update(status="skipped", message="약속에 법률 이름이 없어 찾지 않음")
    elif not settings.api_key:
        src.update(status="skipped", message="이 서비스에서는 국회 의안 조회를 쓸 수 없음(인증키 미설정)")
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

    # 지원사업 공고: 자동 수집이 공공데이터포털(K-Startup)에서 받아 둔 공고 목록에서 찾는다(실시간 API·인증키 불필요)
    src = {"id": "program", "label": "지원사업 공고(공공데이터포털 K-Startup)", "status": "ok", "message": "", "count": 0}
    if not programs:
        src.update(status="skipped", message="지원사업 공고를 아직 수집하지 않음")
    elif item.get("action") not in ("budget", "improve", "review"):
        src.update(status="skipped", message="예산·지원·사업 약속이 아니어서 찾지 않음")
    else:
        from .programs import evidence_for
        found = evidence_for(item, programs)
        evidence += found
        src.update(count=len(found), message=f"수집한 공고 {len(programs)}건에서 찾음")
    sources.append(src)

    # 자르지 않는다: 뒤늦게 가결된 의안처럼 판정을 바꾸는 근거가 잘려 나가면 안 된다(출처별 조회 건수가 이미 상한)
    evidence.sort(key=lambda e: e["date"] or "")
    return {"fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "sources": sources, "evidence": evidence}
