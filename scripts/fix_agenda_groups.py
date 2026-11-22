"""데이터 이전: 일괄 상정 안건 묶음을 채우고, 앞부분이 같은 안건 줄의 발언 안건 자리와 발언 본문에 남은
안건 제목 조각(줄바꿈된 제목 끝·아래 항목·목차 점선)과 첫 발언에 끼어 있던 표지를 바로잡는다. 여러 번 돌려도 결과가 같다.

회의록 텍스트(PDF에서 뽑은 것, 파일 이름 = 회의 ID)를 새 파서로 다시 읽어
- 회의의 agenda_groups_json(한꺼번에 상정한 안건 묶음)을 채우고,
- 저장된 발언이 다시 뽑은 발언과 같은 순서면(이름 95% 이상 일치) 발언의 agenda_idx 를 새 파서 값으로 바꾼다
  (같은 법률 개정안 여러 건처럼 제목 앞부분이 같은 안건 줄이 모두 첫 안건으로 잡히던 것을 번호로 가림),
- 같은 순서일 때 새 파서가 본문에서 떼어 낸 것만 있으면(띄어쓰기를 뺀 글자 비교에서 지운 것뿐) 본문을 새 파서
  값으로 바꾸고 띄어쓰기 교정 표시를 지워 다음 사전 계산에서 다시 교정되게 한다.

    python scripts/fix_agenda_groups.py var/agrisea.sqlite3 회의록텍스트폴더
"""
from __future__ import annotations

import difflib
import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agrisea.parser import align_agendas, parse_minutes
from agrisea.store import Store


_LEADERS = re.compile(r"(?:\s*[·…]){3,}[\s\d]*")   # 목차 점선(저장된 본문에는 이미 없다)


def _only_deleted(old: str, new: str) -> bool:
    """띄어쓰기·목차 점선을 뺀 글자로 비교해 new 가 old 에서 일부를 지운 것뿐인가(지운 것이 있을 때만 참)."""
    a, b = (re.sub(r"\s+", "", _LEADERS.sub(" ", x)) for x in (old, new))
    if a == b:
        return False
    ops = difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
    return all(op in ("equal", "delete") for op, *_ in ops)


def main(db: str, texts: str) -> None:
    Store(db, fts=False).conn.close()   # 새 열(agenda_groups_json)을 만든다
    conn = sqlite3.connect(db)
    grouped = moved = skipped = cleaned = lifted = 0
    for path in sorted(Path(texts).glob("*.txt")):
        mid = path.stem
        row = conn.execute("SELECT agendas_json FROM meetings WHERE id=?", (mid,)).fetchone()
        if not row:
            continue
        doc = parse_minutes(path.read_text(encoding="utf-8"))
        stored = json.loads(row[0] or "[]")
        agendas_match = len(doc.agendas) == len(stored)
        no_idx = conn.execute("SELECT COUNT(*) FROM utterances WHERE meeting_id=? AND agenda_idx IS NOT NULL",
                              (mid,)).fetchone()[0] == 0
        if doc.agendas and no_idx:
            # 표지가 첫 발언 뒤에 끼어 있어 안건 목록을 못 읽었던 회의: 저장된 목록은 API 안건 목록이므로
            # 회의록 순서 + API 표기로 바꾸고 발언 안건 자리를 채운다
            conn.execute("UPDATE meetings SET agendas_json=? WHERE id=?",
                         (json.dumps(align_agendas(doc.agendas, stored), ensure_ascii=False), mid))
            agendas_match = True
            lifted += 1
        if agendas_match:   # 안건 목록이 저장된 것과 다르면 자리가 맞지 않아 안건 묶음·자리는 건드리지 않는다
            conn.execute("UPDATE meetings SET agenda_groups_json=? WHERE id=?", (json.dumps(doc.agenda_groups), mid))
            grouped += bool(doc.agenda_groups)
        else:
            skipped += 1
        rows = conn.execute("SELECT id, speaker_name, agenda_idx, text FROM utterances WHERE meeting_id=? "
                            "ORDER BY idx", (mid,)).fetchall()
        fresh = doc.utterances
        same = sum(r[1] == u.speaker_name for r, u in zip(rows, fresh))
        if agendas_match and len(rows) == len(fresh) and same >= 0.95 * len(rows):
            for (uid, _, ai, _text), u in zip(rows, fresh):
                if u.agenda_idx != ai:
                    conn.execute("UPDATE utterances SET agenda_idx=? WHERE id=?", (u.agenda_idx, uid))
                    moved += 1
        # 본문: 발언자 순서로 짝을 맞춘 발언(PDF 추출 차이로 발언 수가 조금 달라도)에서 새 파서가 떼어 낸 것만 있을 때
        sm = difflib.SequenceMatcher(None, [r[1] for r in rows], [u.speaker_name for u in fresh], autojunk=False)
        for op, i1, i2, j1, j2 in sm.get_opcodes():
            if op != "equal":
                continue
            for (uid, _, _ai, text), u in zip(rows[i1:i2], fresh[j1:j2]):
                new = re.sub(r"\s{2,}", " ", _LEADERS.sub(" ", u.text)).strip()
                if new and _only_deleted(text, new):
                    conn.execute("UPDATE utterances SET text=?, spacing=0 WHERE id=?", (new, uid))
                    cleaned += 1
    conn.execute("DELETE FROM kv")
    conn.execute("DELETE FROM summaries WHERE kind='rule'")
    conn.commit()
    print(f"일괄 상정 묶음이 있는 회의 {grouped}건, 안건 자리를 옮긴 발언 {moved}건, "
          f"안건 제목 조각·표지를 뗀 발언 {cleaned}건, 표지에서 안건 목록을 새로 읽은 회의 {lifted}건, "
          f"안건 목록이 달라 묶음을 건너뛴 회의 {skipped}건")


if __name__ == "__main__":
    main(*sys.argv[1:3])
