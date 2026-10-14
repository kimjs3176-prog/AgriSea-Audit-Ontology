"""수집 파이프라인: API 메타데이터 → 회의록 원문(PDF) → 파싱 → 저장 → 지식그래프."""
from __future__ import annotations

import html
import json
import logging
import re
from pathlib import Path
from typing import Any, Callable

import requests

from .api_client import AssemblyAPIError, AssemblyClient, normalize_row
from .config import ROOT, Settings
from .ontology import build_graph, save_graph
from .parser import extract_pdf_text, parse_minutes
from .store import Store

log = logging.getLogger(__name__)
SAMPLE_DIR = ROOT / "agrisea" / "data" / "sample"


def collect(store: Store, settings: Settings, params: dict[str, Any],
            only_target: bool = True, progress: Callable[[str], None] = print) -> list[dict]:
    client = AssemblyClient(settings)
    meetings = client.committee_meetings(only_target=only_target, **params)
    for m in meetings:
        store.upsert_meeting(m)
    progress(f"회의 {len(meetings)}건 메타데이터 저장 (조건: {params})")
    return meetings


STOP_AFTER_FAILS = 3  # 처음부터 연속으로 이만큼 날짜가 막히면 API 장애로 보고 중단


def collect_range(store: Store, settings: Settings, date_from: str, date_to: str,
                  params: dict[str, Any] | None = None, only_target: bool = True,
                  progress: Callable[[str], None] = print) -> list[dict]:
    """회의일자(CONF_DATE)를 하루씩 바꿔 가며 수집(API가 일자 인자를 요구하는 경우)."""
    from datetime import date, timedelta

    start, end = date.fromisoformat(date_from), date.fromisoformat(date_to)
    if end < start:
        raise ValueError("종료일이 시작일보다 빠릅니다.")
    client = AssemblyClient(settings)
    found: list[dict] = []
    failed: list[str] = []
    day = start
    while day <= end:
        q = {**(params or {}), "CONF_DATE": day.isoformat()}
        try:
            for m in client.committee_meetings(only_target=only_target, **q):
                store.upsert_meeting(m)
                found.append(m)
        except AssemblyAPIError as e:
            # 하루 조회가 막혀도(접속 시간 초과 등) 나머지 날짜와 이미 모은 회의는 살린다
            if e.code != "HTTP":
                raise
            failed.append(day.isoformat())
            progress(f"[조회 실패] {day.isoformat()}: {e.message}")
            # 처음부터 연속으로 막히면 국회 API 전체 장애: 남은 날짜도 모두 실패하므로(하루 수 분씩 낭비)
            # 일찍 멈추고 다음 예약 실행에 맡긴다. 중간에 한두 날만 막힌 경우는 계속해 나머지를 살린다
            if len(failed) >= STOP_AFTER_FAILS and len(failed) == (day - start).days + 1:
                progress(f"연속 {STOP_AFTER_FAILS}일 접속 실패: 국회 API가 막힌 것으로 보고 중단(다음 예약 실행에서 재시도)")
                raise AssemblyAPIError("HTTP", f"국회 API 접속 불가(연속 {STOP_AFTER_FAILS}일 실패)")
        day += timedelta(days=1)
    progress(f"{date_from}~{date_to}: 회의 {len(found)}건 메타데이터 저장"
             + (f" · 조회 실패 {len(failed)}일({', '.join(failed)})" if failed else ""))
    total_days = (end - start).days + 1
    if failed and len(failed) == total_days:
        raise AssemblyAPIError("HTTP", f"모든 날짜 조회 실패({total_days}일): 국회 API에 접속할 수 없습니다.")
    return found


def pending_count(store: Store) -> int:
    return sum(1 for m in store.meetings() if m["text_status"] == "pending")


def seed_store(settings: Settings) -> bool:
    """배포 번들의 초기 DB(gzip)를 쓰기 가능한 위치에 풀어 둔다(서버리스 콜드스타트용)."""
    import gzip
    import shutil

    from .config import SEED_DB

    if settings.db_path.exists() or not SEED_DB.exists():
        return False
    settings.ensure_dirs()
    tmp = settings.db_path.with_suffix(".tmp")
    with gzip.open(SEED_DB, "rb") as src, open(tmp, "wb") as dst:
        shutil.copyfileobj(src, dst)
    tmp.replace(settings.db_path)
    return True


def seed_meta() -> dict:
    from .config import SEED_META
    try:
        return json.loads(SEED_META.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def export_seed(store: Store) -> dict:
    """현재 DB를 배포용 초기 데이터로 내보낸다(가상 예시·전문검색 인덱스·원본 JSON 제외, gzip).

    내용 해시가 기존 초기 데이터와 같으면 파일을 다시 쓰지 않는다(불필요한 커밋 방지).
    """
    import gzip
    import sqlite3
    import tempfile
    from datetime import datetime, timezone

    from .config import SEED_DB, SEED_META
    from .ontology import KG_PATH
    from .precompute import PRECOMPUTE_VERSION, precompute

    digest = f"v{PRECOMPUTE_VERSION}:{store.content_hash()}"
    old = seed_meta()
    if old.get("hash") == digest and SEED_DB.exists() and KG_PATH.exists():
        return {**old, "changed": False}

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "seed.sqlite3"
        with sqlite3.connect(path) as dst:
            store.conn.backup(dst)
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA foreign_keys = ON")
        with conn:
            conn.execute("DELETE FROM meetings WHERE is_sample=1")
            conn.execute("DELETE FROM utterances WHERE meeting_id NOT IN (SELECT id FROM meetings)")
            for name in ("utt_ai", "utt_ad", "utt_au"):
                conn.execute(f"DROP TRIGGER IF EXISTS {name}")
            conn.execute("DROP TABLE IF EXISTS utterances_fts")
            conn.execute("UPDATE meetings SET raw_json=NULL")
            conn.execute("DELETE FROM kv")
            # 회의 요약은 요약 규칙이 바뀌어도 반영되도록 내보낼 때마다 새로 만든다
            conn.execute("DELETE FROM summaries WHERE kind='rule'")
        conn.close()
        # 가상 예시를 뺀 사본 기준으로 요약·집계·지식그래프를 미리 계산해 함께 싣는다
        exported = Store(path, fts=False)
        pre = precompute(exported, KG_PATH)
        stats = {k: v for k, v in exported.stats().items() if k != "samples"}
        exported.conn.close()
        conn = sqlite3.connect(path)
        conn.execute("VACUUM")
        conn.close()
        SEED_DB.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "rb") as src, open(SEED_DB, "wb") as raw, \
                gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
            gz.write(src.read())

    meta = {
        "hash": digest,
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "size_bytes": SEED_DB.stat().st_size,
        "kg_triples": pre.get("triples"),
        **stats,
    }
    SEED_META.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {**meta, "changed": True}


def html_to_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", raw)
    return html.unescape(re.sub(r"<[^>]+>", " ", raw))


def download_minutes_text(url: str, dest_dir: Path, name: str,
                          http: requests.Session | None = None) -> str:
    http = http or requests.Session()
    resp = http.get(url, timeout=60)
    resp.raise_for_status()
    data = resp.content
    if data[:4] == b"%PDF":
        dest_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^\w-]+", "_", name)
        (dest_dir / f"{safe}.pdf").write_bytes(data)
        return extract_pdf_text(data)
    text = resp.text
    # 뷰어 페이지 안에 PDF 링크가 있는 경우 한 번 더 따라감
    m = re.search(r"""(https?://[^"'\s>]+\.pdf[^"'\s>]*)""", text, re.I)
    if m and m.group(1) != url:
        return download_minutes_text(m.group(1), dest_dir, name, http)
    return html_to_text(text)


RECORD_PDF_URL = "https://record.assembly.go.kr/assembly/viewer/minutes/download/pdf.do?id={id}"
RECORD_ID_RE = re.compile(r"record\.assembly\.go\.kr/.*[?&]id=(\d{1,12})")


def minutes_text(url: str, settings: Settings, name: str,
                 http: requests.Session | None = None) -> str:
    """회의록 본문 텍스트. 회의록 PDF 서버는 해외 접속이 막혀 있어, AGRISEA_MINUTES_PROXY가 있으면
    국내 리전(Vercel icn1)의 /api/minutes-text 를 거쳐 받는다."""
    import os

    proxy = os.environ.get("AGRISEA_MINUTES_PROXY", "").strip()
    m = RECORD_ID_RE.search(url)
    if proxy and m:
        http = http or requests.Session()
        resp = http.get(proxy, params={"id": m.group(1)}, timeout=120)
        resp.raise_for_status()
        return resp.text
    return download_minutes_text(url, settings.pdf_dir, name, http)


def fetch_minutes(store: Store, settings: Settings, meeting_ids: list[str] | None = None,
                  limit: int | None = None, progress: Callable[[str], None] = print) -> int:
    retry = ("pending",) if limit else ("pending", "failed")  # 배치 모드에선 실패 건 무한 재시도 방지
    targets = [m for m in store.meetings()
               if (meeting_ids and m["id"] in meeting_ids)
               or (not meeting_ids and m["text_status"] in retry)]
    if limit:
        targets = targets[:limit]
    ok = 0
    http = requests.Session()
    for m in targets:
        url = m.get("pdf_url") or m.get("link_url")
        if not url:
            store.set_status(m["id"], "metadata_only")
            continue
        try:
            text = minutes_text(url, settings, m["id"], http)
            doc = parse_minutes(text)
            store.save_minutes(m["id"], doc)
            ok += bool(doc.utterances)
            progress(f"[{m['date']}] {m['title']}: 발언 {len(doc.utterances)}건")
        except Exception as e:  # 한 건 실패가 전체 수집을 멈추지 않도록
            store.set_status(m["id"], "failed")
            progress(f"[실패] {m['id']} {url}: {e}")
    return ok


def ingest_file(store: Store, path: Path, meta: dict[str, Any], is_sample: bool = False) -> int:
    """로컬 PDF/텍스트 회의록을 직접 적재(API 접근이 안 되는 환경용)."""
    data = path.read_bytes()
    text = extract_pdf_text(data) if data[:4] == b"%PDF" else data.decode("utf-8")
    row = normalize_row(meta)
    row["agendas"] = meta.get("agendas", [])
    store.upsert_meeting(row, is_sample=is_sample)
    doc = parse_minutes(text)
    store.save_minutes(row["meeting_id"], doc)
    return len(doc.utterances)


def load_sample(store: Store) -> int:
    index = json.loads((SAMPLE_DIR / "meetings.json").read_text(encoding="utf-8"))
    total = 0
    for item in index:
        total += ingest_file(store, SAMPLE_DIR / item.pop("file"), item, is_sample=True)
    return total


def rebuild_graph(store: Store, settings: Settings):
    g = build_graph(store)
    save_graph(g, settings.graph_path)
    return g
