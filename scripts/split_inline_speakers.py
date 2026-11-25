"""데이터 이전: 앞 위원의 발언에 붙어 버린 정부·기관 관계자의 답변을 떼어 제 발언으로 나눈다. 여러 번 돌려도 결과가 같다.

예전 파서는 '농업협동조합 중앙회 상무조청래'(기관명 띄어쓰기), '…전무이사지준섭'·'국립농업박물관장황수철'(목록에
없던 직위)처럼 읽지 못한 발언자 머리를 앞 발언에 이어 붙였다. 그래서 답변이 위원의 질의 속에 들어가 질의 대상
기관을 찾지 못했다. 저장된 본문에서 문장 끝 뒤에 오는 그런 머리(새로 읽게 된 직위나 띄어 쓴 기관명이고, 정부
관계자이며 소속 기관이 정해지는 것만)를 찾아 그 자리에서 발언을 나누고 회의의 발언 순서를 다시 매긴다.
발언자 유형·소속·질의 표시는 다음 사전 계산(reclassify)이 다시 계산한다.

    python scripts/split_inline_speakers.py var/agrisea.sqlite3
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agrisea.lexicon import classify_role, org_from_role
from agrisea.parser import SPEAKER_PATTERNS
from agrisea.store import Store

_OFFICIAL = SPEAKER_PATTERNS[-3:]          # 기관+직위 이름(붙여 쓴 것 둘, 띄어 쓴 기관명 하나)
_NEW_TITLE = re.compile(r"(전무이사|상무|관장)$")   # 예전 파서가 몰랐던 직위
_BOUNDARY = re.compile(r"(?<=[.?!…다요죠] )(?=[가-힣])")


def find_header(text: str, start: int = 0) -> tuple[int, int, str, str] | None:
    """본문에서 start 이후 첫 '붙어 버린 발언자 머리' → (머리 시작, 본문 시작, 직위, 이름)."""
    for b in _BOUNDARY.finditer(text, start):
        p = b.start()
        head = text[p:p + 80]
        for pat in _OFFICIAL:
            m = pat.match(head)
            if not m:
                continue
            raw, name = m.group(1), m.group(2)
            role = re.sub(r"\s+", "", raw)
            if (classify_role(role) == "official" and org_from_role(role)
                    and (_NEW_TITLE.search(role) or " " in raw.strip())):
                return p, p + m.end(), role, name
            break
    return None


def split_text(text: str) -> list[tuple[str | None, str | None, str]]:
    """본문 → [(직위, 이름, 조각)]. 첫 조각은 원래 발언자(None, None)."""
    out: list[tuple[str | None, str | None, str]] = []
    role = name = None
    pos = 0
    while (h := find_header(text, pos)) is not None:
        out.append((role, name, text[pos:h[0]].strip()))
        pos, role, name = h[1], h[2], h[3]
    out.append((role, name, text[pos:].strip()))
    return [o for o in out if o[2]]


def main(db: str) -> None:
    store = Store(db)
    c = store.conn
    cols = [r[1] for r in c.execute("PRAGMA table_info(utterances)")]
    keep = [k for k in cols if k not in ("id", "idx")]
    changed: list[str] = []
    for (mid,) in c.execute("SELECT DISTINCT meeting_id FROM utterances").fetchall():
        rows = c.execute("SELECT * FROM utterances WHERE meeting_id=? ORDER BY idx", (mid,)).fetchall()
        new: list[dict] = []
        split = False
        for r in rows:
            parts = split_text(r["text"])
            if len(parts) <= 1:
                new.append({k: r[k] for k in keep})
                continue
            split = True
            for role, name, body in parts:
                d = {k: r[k] for k in keep}
                d["text"] = body
                if "spacing" in d:
                    d["spacing"] = None   # 띄어쓰기 교정은 다음 사전 계산에서 다시 한다
                if role is not None:
                    d.update(speaker_name=name, speaker_role=role, speaker_type="official",
                             org=org_from_role(role), is_question=0, is_commitment=0, is_data_request=0)
                new.append(d)
        if not split:
            continue
        changed.append(f"{mid}: 발언 {len(rows)} → {len(new)}")
        with store.tx() as tx:
            tx.execute("DELETE FROM kv")
            tx.execute("DELETE FROM summaries WHERE meeting_id=?", (mid,))
            tx.execute("DELETE FROM utterances WHERE meeting_id=?", (mid,))
            for i, d in enumerate(new):
                tx.execute(f"INSERT INTO utterances (idx, {', '.join(keep)}) VALUES (?{', ?' * len(keep)})",
                           (i, *[d[k] for k in keep]))
    print("\n".join(changed) or "나눌 발언 없음")


if __name__ == "__main__":
    main(sys.argv[1])
