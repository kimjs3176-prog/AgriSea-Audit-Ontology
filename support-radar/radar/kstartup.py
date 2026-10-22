"""창업진흥원 K-Startup 조회서비스(공공데이터포털 B552735/kisedKstartupService01) 클라이언트.

- 지원사업 공고: getAnnouncementInformation01
- 통합공고 지원사업 소개(예산 등): getBusinessInformation01

공공데이터포털 응답은 JSON(`{"data": [...], "totalCount": n}`)과 XML(`<item>` 또는 `<col name=…>`)
형식이 섞여 있어 둘 다 받아들이고, 필드명은 FIELD_ALIASES로 정규화한다.
조건 검색(cond[...])을 서버가 무시해도 결과가 같도록 기간·모집 여부는 받은 뒤 한 번 더 거른다.
"""
from __future__ import annotations

import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import date
from typing import Any, Callable, Iterator

import requests

OPS = {
    "announcements": "getAnnouncementInformation01",
    "programs": "getBusinessInformation01",
}

# 정규 필드: 실제 응답 필드명(별칭 포함)
ANNOUNCEMENT_FIELDS: dict[str, tuple[str, ...]] = {
    "id": ("pbanc_sn", "id", "pbancSn"),
    "title": ("biz_pbanc_nm", "bizPbancNm", "pbanc_nm"),
    "program": ("intg_pbanc_biz_nm", "intgPbancBizNm"),
    "integrated": ("intg_pbanc_yn", "intgPbancYn"),
    "content": ("pbanc_ctnt", "pbancCtnt"),
    "category": ("supt_biz_clsfc", "suptBizClsfc"),
    "target": ("aply_trgt", "aplyTrgt"),
    "target_text": ("aply_trgt_ctnt", "aplyTrgtCtnt"),
    "exclusion_text": ("aply_excl_trgt_ctnt", "aplyExclTrgtCtnt"),
    "preference": ("prfn_matr", "prfnMatr"),
    "region": ("supt_regin", "suptRegin"),
    "stages": ("biz_enyy", "bizEnyy"),
    "ages": ("biz_trgt_age", "bizTrgtAge"),
    "start": ("pbanc_rcpt_bgng_dt", "pbancRcptBgngDt"),
    "end": ("pbanc_rcpt_end_dt", "pbancRcptEndDt"),
    "org": ("pbanc_ntrp_nm", "pbancNtrpNm"),
    "supervisor": ("sprv_inst", "sprvInst"),
    "dept": ("biz_prch_dprt_nm", "bizPrchDprtNm"),
    "contact": ("prch_cnpl_no", "prchCnplNo"),
    "url": ("detl_pg_url", "detlPgUrl"),
    "guide_url": ("biz_gdnc_url", "bizGdncUrl"),
    "apply_url": ("biz_aply_url", "bizAplyUrl"),
    "recruiting": ("rcrt_prgs_yn", "rcrtPrgsYn"),
}

PROGRAM_FIELDS: dict[str, tuple[str, ...]] = {
    "id": ("id", "supt_biz_id"),
    "title": ("supt_biz_titl_nm", "suptBizTitlNm", "biz_nm"),
    "category": ("biz_category_cd", "bizCategoryCd", "supt_biz_clsfc"),
    "intro": ("supt_biz_intrd_info", "suptBizIntrdInfo"),
    "feature": ("supt_biz_chrct", "suptBizChrct"),
    "target": ("biz_supt_trgt_info", "bizSuptTrgtInfo"),
    "support": ("biz_supt_ctnt", "bizSuptCtnt"),
    "budget": ("biz_supt_bdgt_info", "bizSuptBdgtInfo"),
    "year": ("biz_yr", "bizYr"),
    "url": ("detl_pg_url", "detlPgUrl"),
}

CONTENT_LIMIT = 4000   # 스냅숏 크기를 줄이려고 공고 본문은 앞부분만 보관


class KStartupError(RuntimeError):
    pass


def mask_key(text: str, key: str) -> str:
    text = re.sub(r"(serviceKey=)[^&\s]+", r"\1***", text)
    return text.replace(key, "***") if key else text


def to_iso(value: Any) -> str:
    """'20241231' / '2024-12-31' / '2024.12.31 18:00' → '2024-12-31'."""
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return ""


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"[ \t ]+", " ", str(value)).strip()


def _pick(raw: dict[str, Any], aliases: tuple[str, ...]) -> str:
    lower = {k.lower(): v for k, v in raw.items()}
    for a in aliases:
        if a in raw and raw[a] not in (None, ""):
            return _clean(raw[a])
        if a.lower() in lower and lower[a.lower()] not in (None, ""):
            return _clean(lower[a.lower()])
    return ""


def normalize_announcement(raw: dict[str, Any]) -> dict[str, Any]:
    a = {k: _pick(raw, v) for k, v in ANNOUNCEMENT_FIELDS.items()}
    a["start"], a["end"] = to_iso(a["start"]), to_iso(a["end"])
    a["content"] = a["content"][:CONTENT_LIMIT]
    flag = a["recruiting"].upper()
    a["recruiting"] = True if flag == "Y" else False if flag == "N" else None
    a["integrated"] = a["integrated"].upper() == "Y"
    if not a["id"]:
        a["id"] = f"{a['title']}|{a['start']}"
    return a


def normalize_program(raw: dict[str, Any]) -> dict[str, Any]:
    p = {k: _pick(raw, v) for k, v in PROGRAM_FIELDS.items()}
    if not p["id"]:
        p["id"] = f"{p['title']}|{p['year']}"
    return p


def status(a: dict[str, Any], today: date | None = None) -> str:
    """접수 상태: open(접수 중) | upcoming(접수 예정) | closed(마감)."""
    today_s = (today or date.today()).isoformat()
    if a.get("end") and a["end"] < today_s:
        return "closed"
    if a.get("start") and a["start"] > today_s:
        return "upcoming"
    if a.get("recruiting") is False:
        return "closed"
    return "open" if (a.get("end") or a.get("recruiting")) else "closed"


def is_open(a: dict[str, Any], today: date | None = None) -> bool:
    return status(a, today) == "open"


# ── 응답 파싱 ───────────────────────────────────────────
def _xml_records(root: ET.Element) -> list[dict[str, str]]:
    records = []
    for item in root.iter("item"):
        cols = item.findall("col")
        if cols:   # <item><col name="pbanc_sn">…</col></item>
            records.append({c.get("name", ""): (c.text or "") for c in cols})
        else:      # <item><pbanc_sn>…</pbanc_sn></item>
            records.append({child.tag: (child.text or "") for child in item})
    return records


def _xml_int(root: ET.Element, *names: str) -> int | None:
    for n in names:
        el = root.find(f".//{n}")
        if el is None:
            el = next((c for c in root.iter("col") if c.get("name") == n), None)
        if el is not None and (el.text or "").strip().isdigit():
            return int(el.text.strip())
    return None


def parse_response(text: str) -> tuple[list[dict[str, Any]], int | None]:
    """(레코드 목록, 전체 건수) — 오류 응답이면 KStartupError."""
    body = text.strip().lstrip("﻿")
    if not body:
        raise KStartupError("빈 응답")
    if body.startswith("<"):
        try:
            root = ET.fromstring(body)
        except ET.ParseError as e:
            raise KStartupError(f"XML 해석 실패: {body[:200]}") from e
        # 공공데이터포털 게이트웨이 오류: <OpenAPI_ServiceResponse><cmmMsgHeader>…
        auth = root.find(".//returnAuthMsg")
        if auth is not None:
            code = root.findtext(".//returnReasonCode") or ""
            raise KStartupError(f"공공데이터포털 오류 {code}: {auth.text}")
        code = root.findtext(".//resultCode")
        if code and code not in ("00", "0", "200", "INFO-000"):
            raise KStartupError(f"API 오류 {code}: {root.findtext('.//resultMsg') or ''}")
        return _xml_records(root), _xml_int(root, "totalCount", "matchCount")
    try:
        doc = json.loads(body)
    except json.JSONDecodeError as e:
        raise KStartupError(f"응답 해석 실패: {body[:200]}") from e
    if isinstance(doc, dict) and "data" in doc:
        data = doc["data"] or []
        total = doc.get("totalCount", doc.get("matchCount"))
    elif isinstance(doc, dict) and "response" in doc:   # response.header / response.body.items.item
        header = doc["response"].get("header", {})
        if header.get("resultCode") not in (None, "00", "0"):
            raise KStartupError(f"API 오류 {header.get('resultCode')}: {header.get('resultMsg', '')}")
        bd = doc["response"].get("body", {})
        items = bd.get("items") or {}
        data = items.get("item", []) if isinstance(items, dict) else items
        data = [data] if isinstance(data, dict) else data
        total = bd.get("totalCount")
    elif isinstance(doc, dict) and ("code" in doc and "msg" in doc):
        raise KStartupError(f"API 오류 {doc.get('code')}: {doc.get('msg')}")
    else:
        raise KStartupError(f"알 수 없는 응답 형식: {body[:200]}")
    return [d for d in data if isinstance(d, dict)], (int(total) if total not in (None, "") else None)


# ── 클라이언트 ──────────────────────────────────────────
class KStartupClient:
    def __init__(self, api_key: str = "", base_url: str = "", proxy_url: str = "",
                 session: requests.Session | None = None, timeout: float = 20.0,
                 retries: int = 4, sleep: Callable[[float], None] = time.sleep):
        from .config import API_URL
        if not api_key and not proxy_url:
            raise KStartupError("KSTARTUP_API_KEY(공공데이터포털 인증키)가 설정되지 않았습니다.")
        self.api_key = api_key
        self.base_url = (base_url or API_URL).rstrip("/")
        self.proxy_url = proxy_url.rstrip("/")
        self.session = session or requests.Session()
        self.timeout = timeout
        self.retries = retries
        self.sleep = sleep

    def _url(self, op: str) -> str:
        return f"{self.proxy_url or self.base_url}/{op}"

    def fetch_page(self, op: str, page: int = 1, per_page: int = 100,
                   cond: dict[str, str] | None = None) -> tuple[list[dict], int | None]:
        params: dict[str, Any] = {"page": page, "perPage": per_page, "returnType": "json"}
        if not self.proxy_url:
            params["serviceKey"] = self.api_key
        for k, v in (cond or {}).items():
            params[f"cond[{k}]"] = v
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                r = self.session.get(self._url(op), params=params, timeout=self.timeout)
                if r.status_code >= 500 or r.status_code == 429:
                    raise KStartupError(f"HTTP {r.status_code}")
                if r.status_code >= 400:
                    raise KStartupError(f"HTTP {r.status_code}: {r.text[:200]}")
                return parse_response(r.text)
            except (requests.RequestException, KStartupError) as e:
                last = e
                # 인증·요청 오류는 다시 해도 같다
                if isinstance(e, KStartupError) and not str(e).startswith(("HTTP 5", "HTTP 429", "빈 응답")):
                    break
                if attempt < self.retries:
                    self.sleep(2 ** (attempt + 1))
        raise KStartupError(mask_key(f"{op} 요청 실패: {last}", self.api_key))

    def iter_records(self, op: str, cond: dict[str, str] | None = None, per_page: int = 100,
                     max_pages: int = 400) -> Iterator[dict]:
        page = 1
        while page <= max_pages:
            records, total = self.fetch_page(op, page, per_page, cond)
            yield from records
            if not records or len(records) < per_page or (total is not None and page * per_page >= total):
                return
            page += 1

    def announcements(self, since: str = "", open_only: bool = False,
                      max_pages: int = 400, per_page: int = 100) -> list[dict]:
        """공고 목록(정규화). since: 접수 시작일(YYYY-MM-DD) 이후 공고만."""
        cond: dict[str, str] = {}
        if since:
            cond["pbanc_rcpt_bgng_dt::GTE"] = since.replace("-", "")
        if open_only:
            cond["rcrt_prgs_yn::EQ"] = "Y"
        out: dict[str, dict] = {}
        for raw in self.iter_records(OPS["announcements"], cond, per_page, max_pages):
            a = normalize_announcement(raw)
            if since and a["start"] and a["start"] < since and status(a) == "closed":
                continue
            if open_only and not is_open(a):
                continue
            out[a["id"]] = a
        return list(out.values())

    def programs(self, since_year: int = 0, max_pages: int = 50) -> list[dict]:
        cond = {"biz_yr::GTE": str(since_year)} if since_year else None
        out: dict[str, dict] = {}
        for raw in self.iter_records(OPS["programs"], cond, max_pages=max_pages):
            p = normalize_program(raw)
            if since_year and p["year"][:4].isdigit() and int(p["year"][:4]) < since_year:
                continue
            out[p["id"]] = p
        return list(out.values())
