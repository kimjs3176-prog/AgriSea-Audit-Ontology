"""국회 전체 위원회 회의록 → 주제 발췌(정책 신호의 원천).

농해수위 회의록은 저장소 루트의 회의록 서비스(agrisea)가 이미 모아 두므로 그대로 쓰고,
나머지 위원회(산자중기위·과방위·기재위 등) 회의록을 같은 국회 API·파서로 모은다.
전체 위원회의 본문을 다 보관하면 저장소가 커지므로, 레이더 주제 키워드가 들어간 발언의
해당 문장만 발췌해 data/assembly_extracts.json.gz 에 쌓는다(회의별 처리 상태 포함).

- 회의 목록: 열린국회정보 위원회 회의록 API를 월 단위(CONF_DATE=YYYY-MM)로 조회, 위원회 거르지 않음
- 본문: 회의록 PDF → agrisea 파서(발언자·질의·이행약속 판별). 한 번에 limit건씩 이어서 처리
- 주제 사전(topics.py)이 바뀌면 --refetch 로 이미 처리한 회의를 다시 받아 발췌를 새로 만든다
"""
from __future__ import annotations

import gzip
import json
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import config
from .policy import _sentences
from .topics import TOPICS, squash, topic_hits, topic_keywords

EXTRACTS_PATH = config.ROOT / "data" / "assembly_extracts.json.gz"
MAX_ATTEMPTS = 3          # 본문을 이만큼 실패하면 건너뛴다
RECHECK_DAYS = 7          # 회의록은 며칠 늦게 올라오므로 마지막 조회월 7일 전부터 다시 조회
_ALL_KW = tuple(sorted({k for t in TOPICS for k in topic_keywords(t)}, key=len, reverse=True))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _months(date_from: str, date_to: str) -> list[str]:
    y, m = int(date_from[:4]), int(date_from[5:7])
    out = []
    while (y, m) <= (int(date_to[:4]), int(date_to[5:7])):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def excerpt(text: str) -> str:
    """주제 키워드가 들어간 문장만 남긴다(신호 계산에 필요한 부분)."""
    return " ".join(s for s in _sentences(text) if any(k in squash(s) for k in _ALL_KW))


class Extracts:
    def __init__(self, meetings: dict[str, dict] | None = None, utterances: list[dict] | None = None,
                 meta: dict[str, Any] | None = None):
        self.meetings = meetings or {}
        self.utterances = utterances or []
        self.meta = meta or {}

    @classmethod
    def load(cls, path: Path = EXTRACTS_PATH) -> "Extracts":
        if not path.exists():
            return cls()
        doc = json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
        return cls(doc.get("meetings"), doc.get("utterances"), doc.get("meta"))

    def save(self, path: Path = EXTRACTS_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        doc = {"meta": self.meta, "meetings": dict(sorted(self.meetings.items())),
               "utterances": sorted(self.utterances, key=lambda u: (u["date"], u["meeting_id"], u["idx"]))}
        data = json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        path.write_bytes(gzip.compress(data, mtime=0))   # 내용이 같으면 파일도 같게

    def stats(self) -> dict[str, Any]:
        st: dict[str, int] = {}
        for m in self.meetings.values():
            st[m["status"]] = st.get(m["status"], 0) + 1
        committees = {m.get("committee") for m in self.meetings.values() if m["status"] == "parsed"}
        return {"meetings": len(self.meetings), "status": st, "committees": len(committees - {None, ""}),
                "utterances": len(self.utterances), **{k: self.meta.get(k) for k in ("collected_to", "updated_at")}}

    # ── 본문 반영 ───────────────────────────────────
    def add_parsed(self, meeting_id: str, utterances: list[dict],
                   fix: Callable[[str], str] | None = None) -> int:
        """파싱된 발언 중 주제 발언만 발췌해 넣는다(fix: 띄어쓰기 교정). 넣은 건수."""
        m = self.meetings[meeting_id]
        self.utterances = [u for u in self.utterances if u["meeting_id"] != meeting_id]
        added = 0
        for i, u in enumerate(utterances):
            if not topic_hits(u["text"]):
                continue
            text = excerpt(u["text"])
            self.utterances.append({
                "meeting_id": meeting_id, "idx": u.get("idx", i), "date": m["date"], "title": m["title"],
                "committee": m.get("committee", ""), "speaker_name": u["speaker_name"],
                "speaker_role": u["speaker_role"], "speaker_type": u["speaker_type"], "org": u.get("org") or "",
                "is_question": bool(u["is_question"]), "is_commitment": bool(u["is_commitment"]),
                "orgs_json": json.dumps(u.get("orgs") or {}, ensure_ascii=False), "text": fix(text) if fix else text,
            })
            added += 1
        m.update(status="parsed" if utterances else "empty", utterances=len(utterances))
        return added


def spacing_fixer(agrisea_db: Path | None) -> Callable[[str], str] | None:
    """PDF에서 사라진 띄어쓰기 교정: 회의록 서비스의 말뭉치로 학습(약 10초)."""
    if not agrisea_db or not Path(agrisea_db).exists():
        return None
    import sqlite3

    from agrisea.spacing import SpacingModel
    conn = sqlite3.connect(f"file:{agrisea_db}?mode=ro", uri=True)
    texts = [r[0] for r in conn.execute("SELECT text FROM utterances")]
    conn.close()
    return SpacingModel.train(texts).correct if texts else None


def _agrisea_ids(agrisea_db: Path | None) -> set[str]:
    if not agrisea_db or not Path(agrisea_db).exists():
        return set()
    import sqlite3
    conn = sqlite3.connect(f"file:{agrisea_db}?mode=ro", uri=True)
    ids = {r[0] for r in conn.execute("SELECT id FROM meetings WHERE COALESCE(is_sample,0)=0")}
    conn.close()
    return ids


def collect_meetings(ext: Extracts, client: Any, date_from: str, date_to: str, dae: str = "22",
                     skip_ids: set[str] = frozenset(), progress: Callable[[str], None] = print) -> int:
    """회의 목록(전체 위원회)을 월 단위로 조회해 새 회의를 대기(pending)로 등록. 새로 등록한 건수."""
    from agrisea.api_client import AssemblyAPIError

    new, failed = 0, []
    months = _months(date_from, date_to)
    for ym in months:
        try:
            meetings = client.committee_meetings(only_target=False, DAE_NUM=dae, CONF_DATE=ym)
        except AssemblyAPIError as e:
            if e.code != "HTTP":
                raise
            failed.append(ym)
            progress(f"[조회 실패] {ym}: {e.message}")
            continue
        for m in meetings:
            if not (date_from <= m["date"] <= date_to):
                continue
            mid = m["meeting_id"]
            if mid in ext.meetings:
                continue
            ext.meetings[mid] = {
                "date": m["date"], "title": m["title"], "committee": m.get("committee", ""),
                "pdf_url": m.get("pdf_url", ""), "link_url": m.get("link_url", ""),
                # 농해수위 회의는 회의록 서비스 데이터를 쓰므로 받지 않는다
                "status": "agrisea" if mid in skip_ids else "pending", "attempts": 0,
            }
            new += 1
        progress(f"{ym}: 회의 {len(meetings)}건 조회")
    if failed and len(failed) == len(months):
        raise AssemblyAPIError("HTTP", "모든 달 조회 실패: 국회 API에 접속할 수 없습니다.")
    return new


def fetch_pending(ext: Extracts, settings: Any, limit: int = 200,
                  progress: Callable[[str], None] = print,
                  fix: Callable[[str], str] | None = None,
                  checkpoint: Callable[[], None] | None = None) -> dict[str, int]:
    """대기 중 회의의 본문을 limit건 받아 파싱·발췌(최근 회의부터).
    checkpoint: 20건마다 호출(작업 시간 초과로 중단돼도 받은 만큼 저장)."""
    from agrisea.lexicon import match_organizations
    from agrisea.parser import parse_minutes
    from agrisea.pipeline import minutes_text

    import requests
    targets = sorted((mid for mid, m in ext.meetings.items()
                      if m["status"] in ("pending", "failed") and m.get("attempts", 0) < MAX_ATTEMPTS),
                     key=lambda mid: ext.meetings[mid]["date"], reverse=True)[:limit]
    http = requests.Session()
    done = {"parsed": 0, "failed": 0, "extracted": 0}
    for i, mid in enumerate(targets, 1):
        if checkpoint and i % 20 == 0:
            checkpoint()
        m = ext.meetings[mid]
        url = m.get("pdf_url") or m.get("link_url")
        if not url:
            m["status"] = "metadata_only"
            continue
        try:
            doc = parse_minutes(minutes_text(url, settings, mid, http))
            utts = [{"idx": u.idx, "speaker_name": u.speaker_name, "speaker_role": u.speaker_role,
                     "speaker_type": u.speaker_type, "org": u.org, "text": u.text,
                     "is_question": u.is_question, "is_commitment": u.is_commitment,
                     "orgs": match_organizations(u.text)}
                    for u in doc.utterances]
            n = ext.add_parsed(mid, utts, fix)
            done["parsed"] += 1
            done["extracted"] += n
            progress(f"[{m['date']}] {m['committee']} {m['title']}: 발언 {len(utts)} · 주제 발언 {n}")
        except Exception as e:  # 한 건 실패가 전체를 멈추지 않도록
            m["attempts"] = m.get("attempts", 0) + 1
            m["status"] = "failed"
            done["failed"] += 1
            progress(f"[실패 {m['attempts']}/{MAX_ATTEMPTS}] {mid} {m['title']}: {e}")
    return done


def update(ext: Extracts, limit: int = 200, date_from: str = "", date_to: str = "",
           agrisea_db: Path | None = None, refetch: bool = False,
           progress: Callable[[str], None] = print,
           checkpoint: Callable[[], None] | None = None) -> dict[str, Any]:
    """회의 목록 갱신 + 대기 본문 처리."""
    import os

    from agrisea.api_client import AssemblyClient
    from agrisea.config import Settings as AgriSettings

    from datetime import timedelta
    today = date.today().isoformat()
    if not date_from:
        last = ext.meta.get("collected_to")
        date_from = ((date.fromisoformat(last) - timedelta(days=RECHECK_DAYS)).isoformat()
                     if last else config.COLLECT_SINCE)
    date_to = date_to or today
    if refetch:
        for m in ext.meetings.values():
            if m["status"] in ("parsed", "empty", "failed"):
                m.update(status="pending", attempts=0)
    with tempfile.TemporaryDirectory() as d:
        settings = AgriSettings(api_key=os.environ.get("ASSEMBLY_API_KEY", ""), data_dir=Path(d))
        client = AssemblyClient(settings)
        new = collect_meetings(ext, client, date_from, date_to,
                               skip_ids=_agrisea_ids(agrisea_db), progress=progress)
        ext.meta["collected_to"] = max(ext.meta.get("collected_to") or "", date_to)
        if checkpoint:
            checkpoint()   # 회의 목록만이라도 먼저 저장
        done = fetch_pending(ext, settings, limit, progress,
                             spacing_fixer(agrisea_db) if limit else None, checkpoint)
    ext.meta["updated_at"] = _now()
    return {"new_meetings": new, **done, "range": [date_from, date_to], **ext.stats()}
