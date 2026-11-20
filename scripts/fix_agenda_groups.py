"""한 번만 돌리는 데이터 이전: 일괄 상정 안건 묶음을 채우고, 앞부분이 같은 안건 줄의 발언 안건 자리를 바로잡는다.

회의록 텍스트(PDF에서 뽑은 것, 파일 이름 = 회의 ID)를 새 파서로 다시 읽어
- 회의의 agenda_groups_json(한꺼번에 상정한 안건 묶음)을 채우고,
- 저장된 발언이 다시 뽑은 발언과 같은 순서면(이름 95% 이상 일치) 발언의 agenda_idx 를 새 파서 값으로 바꾼다
  (같은 법률 개정안 여러 건처럼 제목 앞부분이 같은 안건 줄이 모두 첫 안건으로 잡히던 것을 번호로 가림).

    python scripts/fix_agenda_groups.py var/agrisea.sqlite3 회의록텍스트폴더
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agrisea.parser import parse_minutes
from agrisea.store import Store


def main(db: str, texts: str) -> None:
    Store(db, fts=False).conn.close()   # 새 열(agenda_groups_json)을 만든다
    conn = sqlite3.connect(db)
    grouped = moved = skipped = 0
    for path in sorted(Path(texts).glob("*.txt")):
        mid = path.stem
        row = conn.execute("SELECT agendas_json FROM meetings WHERE id=?", (mid,)).fetchone()
        if not row:
            continue
        doc = parse_minutes(path.read_text(encoding="utf-8"))
        if len(doc.agendas) != len(json.loads(row[0] or "[]")):
            skipped += 1   # 안건 목록이 저장된 것과 다르면 자리가 맞지 않아 건드리지 않는다
            continue
        conn.execute("UPDATE meetings SET agenda_groups_json=? WHERE id=?", (json.dumps(doc.agenda_groups), mid))
        grouped += bool(doc.agenda_groups)
        rows = conn.execute("SELECT id, speaker_name, agenda_idx FROM utterances WHERE meeting_id=? "
                            "ORDER BY idx", (mid,)).fetchall()
        fresh = doc.utterances
        same = sum(r[1] == u.speaker_name for r, u in zip(rows, fresh))
        if len(rows) == len(fresh) and same >= 0.95 * len(rows):
            for (uid, _, ai), u in zip(rows, fresh):
                if u.agenda_idx != ai:
                    conn.execute("UPDATE utterances SET agenda_idx=? WHERE id=?", (u.agenda_idx, uid))
                    moved += 1
    conn.execute("DELETE FROM kv")
    conn.execute("DELETE FROM summaries WHERE kind='rule'")
    conn.commit()
    print(f"일괄 상정 묶음이 있는 회의 {grouped}건, 안건 자리를 옮긴 발언 {moved}건, 안건 목록이 달라 건너뛴 회의 {skipped}건")


if __name__ == "__main__":
    main(*sys.argv[1:3])
