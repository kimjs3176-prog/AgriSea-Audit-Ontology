"""SQLite 저장소 + 전문검색(FTS5 trigram, 2글자 이하 검색어는 LIKE 보조)."""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .lexicon import match_issues, match_organizations
from .parser import MinutesDoc

SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
    id TEXT PRIMARY KEY,
    dae TEXT, title TEXT, committee TEXT, date TEXT, class_name TEXT,
    session_no INTEGER, conf_no INTEGER,
    pdf_url TEXT, link_url TEXT, vod_url TEXT,
    agendas_json TEXT DEFAULT '[]',
    raw_json TEXT,
    text_status TEXT DEFAULT 'pending',   -- pending | parsed | failed | metadata_only
    is_sample INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS utterances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id TEXT REFERENCES meetings(id) ON DELETE CASCADE,
    idx INTEGER, speaker_name TEXT, speaker_role TEXT, speaker_type TEXT, org TEXT,
    agenda_idx INTEGER, text TEXT,
    is_question INTEGER, is_commitment INTEGER, is_data_request INTEGER,
    issues_json TEXT, orgs_json TEXT,
    spacing INTEGER DEFAULT 0   -- 띄어쓰기 교정 규칙 버전(0: 교정 전)
);
CREATE INDEX IF NOT EXISTS ix_utt_meeting ON utterances(meeting_id, idx);
CREATE INDEX IF NOT EXISTS ix_utt_speaker ON utterances(speaker_name);
CREATE TABLE IF NOT EXISTS summaries (
    meeting_id TEXT, kind TEXT, payload TEXT, PRIMARY KEY (meeting_id, kind)
);
-- 지원사업 공고(K-Startup, 농림·해양수산 관련만). issues_json: 회의록과 같은 쟁점 체계
CREATE TABLE IF NOT EXISTS programs (
    id TEXT PRIMARY KEY, title TEXT, program TEXT, org TEXT, supervisor TEXT, agency TEXT,
    category TEXT, target TEXT, region TEXT, start_date TEXT, end_date TEXT, url TEXT, content TEXT,
    issues_json TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_prog_start ON programs(start_date);
-- 집계 결과 캐시(쟁점·기관·관계망 등). 데이터가 바뀌면 비운다.
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""

# 전문검색 인덱스(FTS5 trigram). 런타임 SQLite가 지원하지 않으면 LIKE 검색으로 대체한다.
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS utterances_fts USING fts5(
    text, content='utterances', content_rowid='id', tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS utt_ai AFTER INSERT ON utterances BEGIN
    INSERT INTO utterances_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS utt_ad AFTER DELETE ON utterances BEGIN
    INSERT INTO utterances_fts(utterances_fts, rowid, text) VALUES('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS utt_au AFTER UPDATE OF text ON utterances BEGIN
    INSERT INTO utterances_fts(utterances_fts, rowid, text) VALUES('delete', old.id, old.text);
    INSERT INTO utterances_fts(rowid, text) VALUES (new.id, new.text);
END;
"""


class _Rows(list):
    """질의 결과(미리 다 읽어 둔 행): 커서처럼 fetchone/fetchall/반복을 지원."""

    def fetchone(self):
        return self[0] if self else None

    def fetchall(self):
        return list(self)


class SerialConnection:
    """연결 하나를 여러 스레드(웹 요청 스레드 풀)가 나눠 쓸 때 질의를 한 번에 하나씩 실행한다.

    sqlite3 연결은 동시에 쓰면 'bad parameter or other API misuse'로 깨지므로, 결과를 잠금 안에서
    모두 읽어 돌려준다. 트랜잭션(with conn: …)은 끝날 때까지 잠금을 쥔다.
    """

    def __init__(self, conn: sqlite3.Connection):
        self.raw = conn
        self.lock = threading.RLock()

    def execute(self, sql: str, args: Any = ()) -> _Rows:
        with self.lock:
            return _Rows(self.raw.execute(sql, args).fetchall())

    def executemany(self, sql: str, rows: Any) -> None:
        with self.lock:
            self.raw.executemany(sql, rows)

    def executescript(self, sql: str) -> None:
        with self.lock:
            self.raw.executescript(sql)

    def __enter__(self):
        self.lock.acquire()
        self.raw.__enter__()
        return self

    def __exit__(self, *exc):
        try:
            return self.raw.__exit__(*exc)
        finally:
            self.lock.release()

    def __getattr__(self, name: str):  # backup, close 등은 원래 연결로
        return getattr(self.raw, name)


class Store:
    def __init__(self, path: Path | str, fts: bool = True):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        raw = sqlite3.connect(self.path, check_same_thread=False)
        raw.row_factory = sqlite3.Row
        self.conn = SerialConnection(raw)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(utterances)")}
        if "spacing" not in cols:  # 이전 형식 DB
            with self.conn:
                self.conn.execute("ALTER TABLE utterances ADD COLUMN spacing INTEGER DEFAULT 0")
        self.fts = False
        if fts:
            existed = self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='utterances_fts'").fetchone()
            try:
                self.conn.executescript(FTS_SCHEMA)
                self.fts = True
            except sqlite3.OperationalError:
                pass
            if self.fts and not existed:
                # 인덱스 없이 만들어진 DB(초기 데이터 등)를 열면 기존 발언으로 인덱스를 채운다
                with self.conn:
                    self.conn.execute("INSERT INTO utterances_fts(utterances_fts) VALUES('rebuild')")

    @contextmanager
    def tx(self) -> Iterator[SerialConnection]:
        with self.conn:
            yield self.conn

    # ── 쓰기 ─────────────────────────────────────────────
    def upsert_meeting(self, m: dict[str, Any], is_sample: bool = False) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM kv")
            c.execute("DELETE FROM summaries WHERE meeting_id=? AND kind='rule'", (m["meeting_id"],))
            c.execute(
                """INSERT INTO meetings (id, dae, title, committee, date, class_name, session_no,
                       conf_no, pdf_url, link_url, vod_url, agendas_json, raw_json, is_sample)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                       dae=excluded.dae, title=excluded.title, committee=excluded.committee,
                       date=excluded.date, class_name=excluded.class_name,
                       session_no=excluded.session_no, conf_no=excluded.conf_no,
                       pdf_url=excluded.pdf_url, link_url=excluded.link_url,
                       vod_url=excluded.vod_url, agendas_json=excluded.agendas_json,
                       raw_json=excluded.raw_json""",
                (m["meeting_id"], m.get("dae"), m.get("title"), m.get("committee"), m.get("date"),
                 m.get("class_name"), m.get("session_no"), m.get("conf_no"), m.get("pdf_url"),
                 m.get("link_url"), m.get("vod_url"),
                 json.dumps(m.get("agendas", []), ensure_ascii=False),
                 json.dumps(m.get("raw", {}), ensure_ascii=False), int(is_sample)),
            )

    def save_minutes(self, meeting_id: str, doc: MinutesDoc) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM kv")
            c.execute("DELETE FROM utterances WHERE meeting_id=?", (meeting_id,))
            c.execute("DELETE FROM summaries WHERE meeting_id=?", (meeting_id,))
            if doc.agendas:
                row = c.execute("SELECT agendas_json FROM meetings WHERE id=?",
                                (meeting_id,)).fetchone()
                existing = json.loads(row["agendas_json"]) if row else []
                if not existing:
                    c.execute("UPDATE meetings SET agendas_json=? WHERE id=?",
                              (json.dumps(doc.agendas, ensure_ascii=False), meeting_id))
            for u in doc.utterances:
                c.execute(
                    """INSERT INTO utterances (meeting_id, idx, speaker_name, speaker_role,
                           speaker_type, org, agenda_idx, text, is_question, is_commitment,
                           is_data_request, issues_json, orgs_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (meeting_id, u.idx, u.speaker_name, u.speaker_role, u.speaker_type, u.org,
                     u.agenda_idx, u.text, int(u.is_question), int(u.is_commitment),
                     int(u.is_data_request),
                     json.dumps(match_issues(u.text), ensure_ascii=False),
                     json.dumps(match_organizations(u.text), ensure_ascii=False)),
                )
            c.execute("UPDATE meetings SET text_status=? WHERE id=?",
                      ("parsed" if doc.utterances else "failed", meeting_id))

    def set_status(self, meeting_id: str, status: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE meetings SET text_status=? WHERE id=?", (status, meeting_id))

    def save_summary(self, meeting_id: str, kind: str, payload: dict) -> None:
        with self.tx() as c:
            c.execute("INSERT OR REPLACE INTO summaries VALUES (?,?,?)",
                      (meeting_id, kind, json.dumps(payload, ensure_ascii=False)))

    # ── 읽기 ─────────────────────────────────────────────
    @staticmethod
    def _meeting(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["agendas"] = json.loads(d.pop("agendas_json") or "[]")
        d.pop("raw_json", None)
        return d

    @staticmethod
    def _utt(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["issues"] = json.loads(d.pop("issues_json") or "{}")
        d["orgs"] = json.loads(d.pop("orgs_json") or "{}")
        for k in ("is_question", "is_commitment", "is_data_request"):
            d[k] = bool(d[k])
        return d

    def meetings(self, date_from: str = "", date_to: str = "", q: str = "",
                 status: str = "", session: int | None = None) -> list[dict]:
        sql, args = "SELECT * FROM meetings WHERE 1=1", []
        if session:
            sql += " AND session_no = ?"; args.append(session)
        if date_from:
            sql += " AND date >= ?"; args.append(date_from)
        if date_to:
            sql += " AND date <= ?"; args.append(date_to)
        if q:
            sql += " AND (title LIKE ? OR agendas_json LIKE ?)"; args += [f"%{q}%"] * 2
        if status:
            sql += " AND text_status = ?"; args.append(status)
        sql += " ORDER BY date DESC, id DESC"
        return [self._meeting(r) for r in self.conn.execute(sql, args)]

    def meeting(self, meeting_id: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM meetings WHERE id=?", (meeting_id,)).fetchone()
        return self._meeting(r) if r else None

    def utterances(self, meeting_id: str | None = None, speaker: str = "", org: str = "",
                   only_commitments: bool = False, only_questions: bool = False,
                   session: int | None = None, issue: str = "") -> list[dict]:
        sql = ("SELECT u.*, m.date AS meeting_date, m.title AS meeting_title, "
               "m.session_no AS session_no "
               "FROM utterances u JOIN meetings m ON m.id = u.meeting_id WHERE 1=1")
        args: list[Any] = []
        if session:
            sql += " AND m.session_no=?"; args.append(session)
        if issue:
            sql += " AND u.issues_json LIKE ?"; args.append(f'%"{issue}"%')
        if meeting_id:
            sql += " AND u.meeting_id=?"; args.append(meeting_id)
        if speaker:
            sql += " AND u.speaker_name=?"; args.append(speaker)
        if org:
            sql += " AND (u.org=? OR u.orgs_json LIKE ?)"; args += [org, f'%"{org}"%']
        if only_commitments:
            sql += " AND u.is_commitment=1"
        if only_questions:
            sql += " AND u.is_question=1"
        sql += " ORDER BY m.date, u.meeting_id, u.idx"
        return [self._utt(r) for r in self.conn.execute(sql, args)]

    @staticmethod
    def _like(term: str) -> str:
        """LIKE 패턴(검색어의 %·_ 는 글자 그대로). 공백을 빼고 비교하므로 검색어 공백도 뺀다."""
        t = term.replace(" ", "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return f"%{t}%"

    def _search_where(self, terms: list[str], speaker_type: str, date_from: str, date_to: str,
                      session: int | None) -> tuple[str, list[Any]]:
        sql, args = " FROM utterances u JOIN meetings m ON m.id = u.meeting_id WHERE 1=1", []
        if session:
            sql += " AND m.session_no=?"; args.append(session)
        long_terms = [t for t in terms if len(t) >= 3] if self.fts else []
        if long_terms:
            fts = " AND ".join('"' + t.replace('"', '""') + '"' for t in long_terms)
            sql += " AND u.id IN (SELECT rowid FROM utterances_fts WHERE utterances_fts MATCH ?)"
            args.append(fts)
        for t in terms:
            if t in long_terms:
                continue
            # PDF에서 복원한 띄어쓰기가 표기와 다를 수 있어('아프리카 돼지열병') 공백을 무시하고 찾는다
            sql += " AND replace(u.text, ' ', '') LIKE ? ESCAPE '\\'"; args.append(self._like(t))
        if speaker_type:
            sql += " AND u.speaker_type=?"; args.append(speaker_type)
        if date_from:
            sql += " AND m.date >= ?"; args.append(date_from)
        if date_to:
            sql += " AND m.date <= ?"; args.append(date_to)
        return sql, args

    def search(self, query: str, limit: int = 50, speaker_type: str = "",
               date_from: str = "", date_to: str = "", session: int | None = None) -> list[dict]:
        terms = re.sub(r"[\x00-\x1f]", " ", query).split()  # 제어문자(%00 등)는 구분자로
        if not terms:
            return []
        where, args = self._search_where(terms, speaker_type, date_from, date_to, session)
        # 넉넉히 가져와 관련도로 고른 뒤 최신순으로 보여 준다(위원장 사회 발언·짧은 언급보다
        # 검색어를 여러 번 다룬 위원 질의와 정부 답변이 먼저 뽑히도록)
        sql = ("SELECT u.*, m.date AS meeting_date, m.title AS meeting_title, m.session_no AS session_no"
               + where + " ORDER BY m.date DESC, u.idx LIMIT ?")
        rows = [self._utt(r) for r in self.conn.execute(sql, args + [max(limit * 20, 2000)])]
        weight = {"member": 1.0, "official": 0.9, "witness": 0.9, "reference": 0.8,
                  "staff": 0.5, "chair": 0.3}
        bare = [t.replace(" ", "") for t in terms]

        def score(u: dict) -> float:
            text = u["text"].replace(" ", "")
            hits = sum(text.count(t) for t in bare)
            return weight.get(u["speaker_type"], 0.6) * (min(hits, 5) + min(len(u["text"]) / 400, 1))
        top = sorted(rows, key=score, reverse=True)[:limit]
        return sorted(top, key=lambda u: (u["meeting_date"] or "", -u["idx"]), reverse=True)

    def search_stats(self, query: str, speaker_type: str = "", date_from: str = "", date_to: str = "",
                     session: int | None = None) -> dict:
        """검색 결과 전체의 건수와 회기·발언자 유형별 분포(목록은 관련도 상위만 돌려주므로 따로 센다)."""
        terms = re.sub(r"[\x00-\x1f]", " ", query).split()  # 제어문자(%00 등)는 구분자로
        if not terms:
            return {"total": 0, "by_session": {}, "by_type": {}}
        where, args = self._search_where(terms, speaker_type, date_from, date_to, session)
        by_session = {r[0]: r[1] for r in self.conn.execute(
            "SELECT m.session_no, COUNT(*)" + where + " GROUP BY m.session_no", args) if r[0]}
        by_type = {r[0]: r[1] for r in self.conn.execute(
            "SELECT u.speaker_type, COUNT(*)" + where + " GROUP BY u.speaker_type", args)}
        return {"total": sum(by_type.values()), "by_session": by_session, "by_type": by_type}

    # ── 지원사업 공고 ───────────────────────────────────────
    def save_programs(self, rows: list[dict]) -> int:
        """공고를 받은 그대로 덮어쓴다(바뀐 게 있으면 집계 캐시를 비움). 반환: 저장 건수."""
        new = [(a["id"], a["title"], a["program"], a["org"], a["supervisor"], a["agency"], a["category"],
                a["target"], a["region"], a["start"], a["end"], a["url"], a["content"],
                json.dumps(a["issues"], ensure_ascii=False)) for a in rows]
        old = {r[0]: tuple(r) for r in self.conn.execute(
            "SELECT id, title, program, org, supervisor, agency, category, target, region, start_date, end_date, "
            "url, content, issues_json FROM programs")}
        if {r[0]: r for r in new} == old:
            return len(new)
        with self.tx() as c:
            c.execute("DELETE FROM programs")
            c.executemany("INSERT INTO programs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", new)
            c.execute("DELETE FROM kv")
        return len(new)

    def programs(self, issue: str = "", agency: str = "", since: str = "", limit: int | None = None) -> list[dict]:
        """공고(최신 접수 순). issue는 그 쟁점과 하위 쟁점, agency는 피감기관 정규명."""
        sql, args = "SELECT * FROM programs WHERE 1=1", []
        if agency:
            sql += " AND agency=?"; args.append(agency)
        if since:
            sql += " AND start_date >= ?"; args.append(since)
        sql += " ORDER BY start_date DESC, id DESC"
        rows = []
        for r in self.conn.execute(sql, args):
            d = dict(r)
            d["issues"] = json.loads(d.pop("issues_json") or "{}")
            d["start"], d["end"] = d.pop("start_date"), d.pop("end_date")
            rows.append(d)
        if issue:
            from .lexicon import ISSUES
            ids = {issue} | {k for k, v in ISSUES.items() if v[1] == issue}
            rows = [d for d in rows if ids & set(d["issues"])]
        return rows[:limit] if limit else rows

    def kv_get(self, key: str) -> Any:
        r = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(r["value"]) if r else None

    def kv_set(self, key: str, value: Any) -> None:
        with self.tx() as c:
            c.execute("INSERT OR REPLACE INTO kv VALUES (?,?)",
                      (key, json.dumps(value, ensure_ascii=False)))

    def summary(self, meeting_id: str, kind: str) -> dict | None:
        r = self.conn.execute("SELECT payload FROM summaries WHERE meeting_id=? AND kind=?",
                              (meeting_id, kind)).fetchone()
        return json.loads(r["payload"]) if r else None

    def sessions(self) -> list[dict]:
        """회기 목록(최근 순). 정기회는 매년 9월 1일 소집되므로 첫 회의가 9월인 회기를 정기회로 본다."""
        rows = self.conn.execute(
            """SELECT m.session_no, MAX(m.dae) AS dae, MIN(m.date) AS date_from,
                      MAX(m.date) AS date_to, COUNT(*) AS meetings,
                      SUM(m.title LIKE '%소위원회%') AS subcommittee_meetings,
                      (SELECT COUNT(*) FROM utterances u JOIN meetings m2 ON m2.id=u.meeting_id
                        WHERE m2.session_no=m.session_no) AS utterances
               FROM meetings m WHERE m.session_no IS NOT NULL
               GROUP BY m.session_no ORDER BY m.session_no DESC""").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["kind"] = "정기회" if (d["date_from"] or "")[5:7] == "09" else "임시회"
            d["label"] = (f"제{d['session_no']}회 {d['kind']} "
                          f"({(d['date_from'] or '')[:10].replace('-', '.')}~"
                          f"{(d['date_to'] or '')[5:10].replace('-', '.')})")
            out.append(d)
        return out

    def speakers(self) -> list[dict]:
        sql = """SELECT speaker_name, speaker_role, speaker_type, org,
                        COUNT(*) AS n_utts, COUNT(DISTINCT meeting_id) AS n_meetings,
                        SUM(is_question) AS n_questions, SUM(is_commitment) AS n_commitments
                 FROM utterances GROUP BY speaker_name, speaker_type
                 ORDER BY n_utts DESC"""
        return [dict(r) for r in self.conn.execute(sql)]

    def content_hash(self) -> str:
        """수집 데이터 내용 해시(가상 예시 제외). 변경 여부 판단용."""
        import hashlib
        h = hashlib.sha256()
        for r in self.conn.execute(
                "SELECT id, title, date, agendas_json, text_status FROM meetings "
                "WHERE is_sample=0 ORDER BY id"):
            h.update(repr(tuple(r)).encode())
        for r in self.conn.execute(
                "SELECT u.meeting_id, u.idx, u.speaker_name, u.text FROM utterances u "
                "JOIN meetings m ON m.id=u.meeting_id WHERE m.is_sample=0 "
                "ORDER BY u.meeting_id, u.idx"):
            h.update(repr(tuple(r)).encode())
        for r in self.conn.execute("SELECT id, title, start_date, end_date, issues_json FROM programs ORDER BY id"):
            h.update(repr(tuple(r)).encode())
        return h.hexdigest()

    def stats(self) -> dict:
        c = self.conn
        return {
            "meetings": c.execute("SELECT COUNT(*) FROM meetings").fetchone()[0],
            "parsed": c.execute("SELECT COUNT(*) FROM meetings WHERE text_status='parsed'").fetchone()[0],
            "utterances": c.execute("SELECT COUNT(*) FROM utterances").fetchone()[0],
            "commitments": c.execute("SELECT COUNT(*) FROM utterances WHERE is_commitment=1").fetchone()[0],
            "questions": c.execute("SELECT COUNT(*) FROM utterances WHERE is_question=1").fetchone()[0],
            "samples": c.execute("SELECT COUNT(*) FROM meetings WHERE is_sample=1").fetchone()[0],
            "programs": c.execute("SELECT COUNT(*) FROM programs").fetchone()[0],
            "date_min": c.execute("SELECT MIN(date) FROM meetings").fetchone()[0],
            "date_max": c.execute("SELECT MAX(date) FROM meetings").fetchone()[0],
        }
