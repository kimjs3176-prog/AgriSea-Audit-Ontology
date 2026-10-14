"""열린국회정보 '위원회 회의록' Open API 클라이언트.

응답 구조(열린국회정보 공통):
    {"<서비스명>": [
        {"head": [{"list_total_count": N},
                  {"RESULT": {"CODE": "INFO-000", "MESSAGE": "정상 처리되었습니다."}}]},
        {"row": [ {...}, ... ]}
    ]}
오류/데이터 없음:
    {"RESULT": {"CODE": "INFO-200", "MESSAGE": "해당하는 데이터가 없습니다."}}

출력 필드명은 API 버전에 따라 조금씩 다를 수 있어 `normalize_row`에서 별칭을 흡수한다.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

import requests

from .config import COMMITTEE_ALIASES, Settings

# 정규화 필드 → 원본 API 후보 필드명(우선순위 순)
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "meeting_id": ("CONFER_NUM", "CONF_ID", "CONFER_ID", "MINTS_ID", "MINUTES_ID"),
    "title": ("TITLE", "CONF_TITLE", "CONF_NAME", "MEETING_NAME"),
    "committee": ("COMM_NAME", "CMIT_NM", "CMIT_NAME", "COMMITTEE_NAME", "CLASS_NAME2"),
    "date": ("CONF_DATE", "MEETING_DATE", "CONF_DT"),
    "dae": ("DAE_NUM", "ERACO", "UNIT_CD"),
    "class_name": ("CLASS_NAME", "CONF_KIND", "MEETING_KIND"),
    "agenda": ("SUB_NAME", "AGENDA", "AGENDA_NM", "SUBJECT", "BILL_NAME"),
    "pdf_url": ("PDF_LINK_URL", "PDF_URL", "DOWN_URL", "FILE_URL"),
    "link_url": ("CONF_LINK_URL", "LINK_URL", "MINTS_URL", "URL"),
    "vod_url": ("VOD_LINK_URL", "VOD_URL"),
}

SESSION_RE = re.compile(r"제\s*(\d+)\s*회")
CONF_NO_RE = re.compile(r"제\s*(\d+)\s*차")


class AssemblyAPIError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


@dataclass
class Page:
    total: int
    rows: list[dict[str, Any]] = field(default_factory=list)


def _pick(row: dict[str, Any], keys: tuple[str, ...]) -> str:
    for k in keys:
        v = row.get(k)
        if v not in (None, ""):
            return str(v).strip()
    return ""


def normalize_date(value: str) -> str:
    """'20241007', '2024-10-07', '2024.10.07' → '2024-10-07'."""
    digits = re.sub(r"\D", "", value or "")
    if len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return value or ""


def normalize_conf_date(value: str) -> str:
    """요청인자 CONF_DATE 정규화. API는 'YYYY', 'YYYY-MM', 'YYYY-MM-DD'(접두 일치)만 받는다
    ('20241007'처럼 구분자 없는 형식은 결과가 비어 있음)."""
    v = (value or "").strip()
    parts = re.split(r"[.\-/\s]+", v) if re.search(r"[.\-/\s]", v) else None
    if parts is None:
        if not v.isdigit() or len(v) not in (4, 6, 8):
            raise ValueError(f"회의일자 형식 오류: {value!r} (예: 2024, 2024-10, 2024-10-07)")
        parts = [v[:4], v[4:6], v[6:8]][: {4: 1, 6: 2, 8: 3}[len(v)]]
    parts = [p for p in parts if p]
    if not 1 <= len(parts) <= 3 or not all(p.isdigit() for p in parts) or len(parts[0]) != 4:
        raise ValueError(f"회의일자 형식 오류: {value!r} (예: 2024, 2024-10, 2024-10-07)")
    return "-".join([parts[0]] + [p.zfill(2) for p in parts[1:]])


def normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    out = {k: _pick(row, keys) for k, keys in FIELD_ALIASES.items()}
    out["date"] = normalize_date(out["date"])
    title = out["title"]
    m = SESSION_RE.search(title)
    out["session_no"] = int(m.group(1)) if m else None
    m = CONF_NO_RE.search(title)
    out["conf_no"] = int(m.group(1)) if m else None
    if not out["meeting_id"]:
        # 회의 식별자가 없으면 날짜+제목으로 합성
        out["meeting_id"] = re.sub(r"\W+", "_", f"{out['date']}_{title}")[:120]
    out["raw"] = row
    return out


def is_target_committee(row: dict[str, Any], aliases: tuple[str, ...] = COMMITTEE_ALIASES) -> bool:
    hay = f"{row.get('committee', '')} {row.get('title', '')}"
    return any(a in hay for a in aliases)


class AssemblyClient:
    def __init__(self, settings: Settings, session: requests.Session | None = None,
                 timeout: float | tuple[float, float] = (10.0, 60.0), sleep: float = 0.2, retries: int = 4):
        if not settings.api_key:
            raise ValueError("ASSEMBLY_API_KEY 환경변수(또는 .env)에 인증키를 설정하세요.")
        self.settings = settings
        self.http = session or requests.Session()
        self.timeout = timeout
        self.sleep = sleep
        self.retries = retries
        self.service = settings.api_url.rstrip("/").rsplit("/", 1)[-1]

    def fetch_page(self, p_index: int = 1, p_size: int = 100, **params: Any) -> Page:
        query = {"KEY": self.settings.api_key, "Type": "json",
                 "pIndex": p_index, "pSize": p_size}
        query.update({k: v for k, v in params.items() if v not in (None, "")})
        for attempt in range(self.retries + 1):
            try:
                resp = self.http.get(self.settings.api_url, params=query, timeout=self.timeout)
                resp.raise_for_status()
                data = resp.json()
                break
            except (requests.RequestException, ValueError) as e:
                if attempt < self.retries:  # 일시적 오류는 잠시 후 재시도(2·4·8·16초, 장기간 수집 보호)
                    time.sleep(2 ** (attempt + 1))
                    continue
                # 예외 메시지에 요청 URL(인증키 포함)이 들어가므로 가린 뒤 전달
                raise AssemblyAPIError("HTTP", self._redact(str(e))) from None
        return self.parse_response(data)

    def _redact(self, text: str) -> str:
        key = self.settings.api_key
        return text.replace(key, "***") if key else text

    def parse_response(self, data: dict[str, Any]) -> Page:
        if "RESULT" in data and self.service not in data:
            code = data["RESULT"].get("CODE", "")
            if code == "INFO-200":  # 해당하는 데이터가 없습니다
                return Page(total=0)
            raise AssemblyAPIError(code, data["RESULT"].get("MESSAGE", ""))
        blocks = data.get(self.service) or next(
            (v for v in data.values() if isinstance(v, list)), [])
        total, rows = 0, []
        for block in blocks:
            if "head" in block:
                for h in block["head"]:
                    if "list_total_count" in h:
                        total = int(h["list_total_count"])
                    if "RESULT" in h and h["RESULT"].get("CODE") not in ("INFO-000", None):
                        raise AssemblyAPIError(h["RESULT"]["CODE"], h["RESULT"].get("MESSAGE", ""))
            if "row" in block:
                rows.extend(block["row"])
        return Page(total=total, rows=rows)

    def iter_rows(self, p_size: int = 100, max_pages: int = 1000, **params: Any) -> Iterator[dict]:
        page_no = 1
        seen = 0
        while page_no <= max_pages:
            page = self.fetch_page(page_no, p_size, **params)
            if not page.rows:
                return
            for row in page.rows:
                yield normalize_row(row)
            seen += len(page.rows)
            if seen >= page.total:
                return
            page_no += 1
            time.sleep(self.sleep)

    def committee_meetings(self, only_target: bool = True, **params: Any) -> list[dict]:
        """행(대개 안건 단위)을 회의 단위로 묶어 반환한다.

        필수 요청인자: DAE_NUM(대수), CONF_DATE(회의일자, 'YYYY'·'YYYY-MM'·'YYYY-MM-DD').
        """
        if not params.get("DAE_NUM"):
            raise ValueError("국회 대수(DAE_NUM)는 필수입니다. 예: 22")
        if not params.get("CONF_DATE"):
            raise ValueError("회의일자(CONF_DATE)는 필수입니다. 예: 2024, 2024-10, 2024-10-07")
        params["CONF_DATE"] = normalize_conf_date(str(params["CONF_DATE"]))
        meetings: dict[str, dict] = {}
        for row in self.iter_rows(**params):
            if only_target and not is_target_committee(row):
                continue
            m = meetings.setdefault(row["meeting_id"], {**row, "agendas": []})
            if row["agenda"] and row["agenda"] not in m["agendas"]:
                m["agendas"].append(row["agenda"])
            for k in ("pdf_url", "link_url", "vod_url"):
                if not m.get(k) and row.get(k):
                    m[k] = row[k]
        return sorted(meetings.values(), key=lambda m: (m["date"], m["meeting_id"]))
