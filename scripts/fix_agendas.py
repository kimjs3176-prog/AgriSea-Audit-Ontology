"""한 번만 돌리는 데이터 이전: 회의 안건 목록을 회의록 첫머리 순서로 바로잡는다.

발언의 agenda_idx 는 회의록 첫머리 안건 목록의 자리인데, 회의 안건(agendas_json)에는 순서가 다르고 일부가 빠진
API 안건 목록이 들어 있어 화면·요약·지식그래프에서 발언에 다른 안건 제목이 붙었다. 회의록 텍스트(PDF에서 뽑은 것,
파일 이름 = 회의 ID)를 읽어 안건 목록을 다시 뽑고(중복 정리 포함), 예전 목록의 자리를 새 목록의 자리로 옮겨
agenda_idx 를 고친 뒤, 안건 목록을 align_agendas(회의록 순서 + API 표기)로 바꾼다.

    python scripts/fix_agendas.py var/agrisea.sqlite3 회의록텍스트폴더
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agrisea.parser import (SPEAKER_MARK, _LEADER_TAIL, _extract_agendas, _norm,  # noqa: E402
                            align_agendas, clean_text, parse_minutes, split_appendix)

# 이전 파서의 안건 줄(두 자리 번호까지만)
OLD_AGENDA_LINE = re.compile(r"^\s*(\d{1,2})\s*\.\s*(.{4,120}?)\s*(?:[·.…]{3,}\s*\d+)?\s*$")


def old_agendas(preamble: str) -> list[str]:
    """이전 파서의 안건 목록(저장된 agenda_idx 가 가리키는 목록)."""
    out: list[str] = []
    for line in preamble.split("\n"):
        m = OLD_AGENDA_LINE.match(line)
        if m:
            title = re.sub(r"\s+", " ", m.group(2)).strip(" ·.")
            if title and title not in out:
                out.append(title)
    return out


def main(db: str, texts: str) -> None:
    conn = sqlite3.connect(db)
    key = lambda t: _norm(_LEADER_TAIL.sub("", t).strip(" ·."))  # noqa: E731
    fixed = moved = remapped = 0
    for path in sorted(Path(texts).glob("*.txt")):
        mid = path.stem
        row = conn.execute("SELECT agendas_json FROM meetings WHERE id=?", (mid,)).fetchone()
        if not row:
            continue
        text, _ = split_appendix(clean_text(path.read_text(encoding="utf-8")))
        preamble = SPEAKER_MARK.split(text)[0]
        old, new = old_agendas(preamble), _extract_agendas(preamble)
        if not new:
            continue
        rows = conn.execute("SELECT id, speaker_name, agenda_idx FROM utterances WHERE meeting_id=? "
                            "ORDER BY idx", (mid,)).fetchall()
        fresh = parse_minutes(path.read_text(encoding="utf-8")).utterances
        same = sum(r[1] == u.speaker_name for r, u in zip(rows, fresh))
        if len(rows) == len(fresh) and same >= 0.95 * len(rows):
            # 발언이 다시 뽑은 것과 같은 순서면(이름 표기 보정 차이만 허용) 새 파서의 안건 자리를 그대로 쓴다
            # (예전 파서가 못 알아본 100번 이상 안건 줄·점선이 붙은 안건 줄 뒤의 발언도 바로잡힌다)
            target = [u.agenda_idx for u in fresh]
        else:  # PDF 추출 차이로 발언 수가 다르면 예전 목록 자리 → 새 목록 자리로만 옮긴다
            new_keys = [_norm(a) for a in new]
            remap = {i: new_keys.index(key(a)) for i, a in enumerate(old) if key(a) in new_keys}
            target = [remap.get(r[2], r[2]) if r[2] is not None else None for r in rows]
            remapped += 1
        for (uid, _, ai), t in zip(rows, target):
            if t != ai:
                conn.execute("UPDATE utterances SET agenda_idx=? WHERE id=?", (t, uid))
                moved += 1
        aligned = align_agendas(new, json.loads(row[0] or "[]"))
        conn.execute("UPDATE meetings SET agendas_json=? WHERE id=?",
                     (json.dumps(aligned, ensure_ascii=False), mid))
        fixed += 1
    conn.execute("DELETE FROM kv")
    conn.commit()
    print(f"안건 목록을 바로잡은 회의 {fixed}건(대응표로 옮긴 회의 {remapped}건), 안건 자리를 옮긴 발언 {moved}건")


if __name__ == "__main__":
    main(*sys.argv[1:3])
