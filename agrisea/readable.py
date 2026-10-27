"""회의록 원문을 읽기 쉽게: 질의 차례 묶기, 끊긴 낱말 잇기 사전, 용어집.

- 질의 차례(turns): 위원 한 명이 질의를 시작해 위원장이 다음 위원으로 넘길 때까지를 한 묶음으로 보고,
  위원 질문 → 정부 답변을 한 줄씩(질문 핵심 문장 → 답변 요지) 정리한다. '예.', '맞습니다' 같은 짧은 답은
  요지 대신 그대로 붙인다. 원문 발언 번호(idx)를 함께 돌려줘 화면에서 원문으로 바로 간다.
- 끊긴 낱말(rejoin): PDF 줄바꿈으로 '활 성화', '계 시나요'처럼 갈라진 채 남은 말을 말뭉치에서 찾아,
  붙인 형태가 훨씬 흔한 경우만 '읽기 쉽게' 보기에서 잇는다(원문은 그대로 둔다).
- 용어집(glossary): 기관 약칭 → 정식 명칭, 회의록에 나온 법률 이름(국가법령정보센터 연결).
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

from .lexicon import ORGANIZATIONS, QUESTION_PATTERNS, commitment_sentences, issue_label
from .nlp import truncate
from .store import Store

_SENT = re.compile(r"(?<=[.?!])\s+(?=\S)")
_ADDRESS = re.compile(r"^(?:(?:예|네|아|그|저|존경하는)\s*[,.]?\s*)*(?:[가-힣]{2,4}\s*)?(?:위원님|위원장님|장관님|청장님)\s*[,.]?\s*")
_GREETING = re.compile(r"안녕\s?하십니까|반갑습니다|감사(?:드립|합)니다|수고(?:가\s?)?많으십니다|고생(?:이\s?)?많으십니다")
_CUE_NEXT = re.compile(r"다음(?:은|으로)|질의(?:해\s*주|하여\s*주|를\s*시작)|수고(?:하셨|많으셨)")
_ANSWERERS = ("official", "witness", "reference")
SHORT_ANSWER = 30   # 이보다 짧은 답은 요지로 줄이지 않고 그대로('예, 맞습니다')
LINE = 150          # 질문·답변 한 줄 길이


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT.split(text or "") if s.strip()]


def key_question(texts: list[str]) -> str:
    """위원 발언(들)에서 질문의 핵심 문장: 물음 문장 > 수치가 든 문장 > 앞 문장."""
    sents = [s for t in texts for s in _sentences(t) if not _GREETING.search(s)] or \
        [s for t in texts for s in _sentences(t)]
    if not sents:
        return ""

    def score(i_s: tuple[int, str]) -> float:
        i, s = i_s
        q = 3 if "?" in s or QUESTION_PATTERNS.search(s[-8:]) else 0
        num = 1 if re.search(r"\d", s) else 0
        short = -2 if len(s) < 12 else 0
        return q + num + short - i * 0.05  # 같으면 앞 문장
    i, best = max(enumerate(sents), key=score)
    if len(best) < 25 and i > 0:  # '근본 대책이 있습니까?'처럼 짧은 물음은 바로 앞 문장(맥락)을 붙인다
        best = f"{sents[i - 1]} {best}"
    return truncate(best, LINE)


def answer_gist(texts: list[str]) -> tuple[str, bool]:
    """정부 답변의 요지와 '짧은 답' 여부. 약속이 있으면 약속 문장을 우선한다."""
    whole = " ".join(t.strip() for t in texts if t.strip())
    if len(whole) <= SHORT_ANSWER:
        return whole, True
    promise = next((s for t in texts for s in commitment_sentences(t)), "")
    if promise:
        return truncate(_ADDRESS.sub("", promise), LINE), False
    for s in _sentences(whole):
        s = _ADDRESS.sub("", s)
        if len(re.findall(r"[가-힣]", s)) >= 10:
            return truncate(s, LINE), False
    return truncate(_ADDRESS.sub("", whole), LINE), False


def _who(u: dict) -> str:
    return f"{u['speaker_role']} {u['speaker_name']}".strip()


def turns(utts: list[dict]) -> list[dict]:
    """회의 발언(순서대로) → 질의 차례 목록."""
    out: list[dict] = []
    cur: dict | None = None

    def close() -> None:
        nonlocal cur
        # 정부 답변을 들은 질의만(개회 인사·의견 진술은 전체 회의록에서)
        if cur and any(it["a_texts"] for it in cur["items"]):
            out.append(cur)
        cur = None

    for u in utts:
        st = u["speaker_type"]
        if st == "member":
            if cur is None or cur["member"] != u["speaker_name"]:
                close()
                cur = {"member": u["speaker_name"], "start_idx": u["idx"], "end_idx": u["idx"],
                       "agenda_idx": u.get("agenda_idx"), "items": [], "_issues": Counter()}
            last = cur["items"][-1] if cur["items"] else None
            if last and not last["a_texts"]:  # 답을 듣기 전에 이어서 말하면 한 질문으로
                last["q_texts"].append(u["text"])
                last["data_request"] |= bool(u.get("is_data_request"))
            else:
                cur["items"].append({"idx": u["idx"], "q_texts": [u["text"]], "a_texts": [], "answerer": "",
                                     "commitment": False, "data_request": bool(u.get("is_data_request"))})
            cur["_issues"].update(u.get("issues") or {})
            cur["end_idx"] = u["idx"]
        elif st in _ANSWERERS and cur and cur["items"]:
            it = cur["items"][-1]
            it["a_texts"].append(u["text"])
            it["answerer"] = it["answerer"] or _who(u)
            it["commitment"] |= bool(u.get("is_commitment"))
            cur["_issues"].update(u.get("issues") or {})
            cur["end_idx"] = u["idx"]
        elif st == "chair":
            # '1분만 더 쓰세요' 같은 짧은 끼어들기는 넘기고, 다음 위원 호명·긴 진행 발언에서 끊는다
            if cur and (len(u["text"]) > 80 or _CUE_NEXT.search(u["text"])):
                close()
        else:
            close()
    close()

    result = []
    for t in out:
        items = []
        for it in t["items"]:
            a, short = answer_gist(it["a_texts"]) if it["a_texts"] else ("", False)
            q_len = sum(len(x) for x in it["q_texts"])
            # '장관님.' → '예, 위원님.' 같은 호명·맞장구, 답 없는 짧은 말('좋아요.')은 줄로 세우지 않는다
            if (q_len < 12 and (short or not a)) or (not a and q_len < 30):
                continue
            items.append({"idx": it["idx"], "q": key_question(it["q_texts"]), "a": a, "short": short,
                          "answerer": it["answerer"], "commitment": it["commitment"],
                          "data_request": it["data_request"]})
        result.append({
            "member": t["member"], "start_idx": t["start_idx"], "end_idx": t["end_idx"], "agenda_idx": t["agenda_idx"],
            "issues": [issue_label(i) for i, _ in t["_issues"].most_common(2)],
            "questions": len(items), "answered": sum(1 for x in items if x["a"]),
            "commitments": sum(1 for x in items if x["commitment"]),
            "data_requests": sum(1 for x in items if x["data_request"]), "items": items,
        })
    return result


def meeting_turns(store: Store, meeting_id: str) -> list[dict]:
    return turns(store.utterances(meeting_id=meeting_id))


# ── 끊긴 낱말 잇기 사전 ─────────────────────────────────────
_HANGUL = re.compile(r"[가-힣]+")
_SPLIT = re.compile(r"(?<![가-힣])([가-힣]{1,3}) ([가-힣]{2})")
# 홀로 쓰이는 흔한 한 글자 낱말: 뒤 말과 붙이면 오히려 틀린다('이 지역', '할 수', '번 식물', '만 원')
_WORDS = set("이 그 저 한 두 세 네 수 것 할 잘 못 안 명 번 건 억 만 원 년 월 일 개 곳 전 후 좀 더 또 각 제 총 약 및 등 게 때 데 줄 거 걸 뭐 왜 꼭 참 막 다 또 새 첫 큰 각 본 당 해 내 네".split())


def rejoin_map(texts: list[str], min_joined: int = 20, ratio: int = 10) -> dict[str, str]:
    """'활 성화' → '활성화' 같은 잇기 후보. 앞 조각이 홀로 거의 쓰이지 않고(조각), 붙인 형태로 시작하는
    낱말이 말뭉치에 충분히 많으며 갈라진 형태보다 ratio배 이상 흔할 때만 잇는다(정확도 우선:
    드문 낱말이 갈라진 경우는 근거가 모자라 그대로 둔다)."""
    tokens = Counter(w for t in texts for w in _HANGUL.findall(t))
    prefix: Counter = Counter()
    for w, n in tokens.items():
        for k in range(2, min(6, len(w) + 1)):
            prefix[w[:k]] += n
    split: Counter = Counter()
    for t in texts:
        for a, b in _SPLIT.findall(t):
            split[(a, b)] += 1
    out = {}
    for (a, b), n in split.items():
        joined = prefix[a + b]
        if (a not in _WORDS and joined >= min_joined and joined >= ratio * n
                and tokens[a] <= max(3, joined // 50)):
            out[f"{a} {b}"] = a + b
    return out


def glossary(store: Store) -> dict[str, Any]:
    """'읽기 쉽게' 보기용: 끊긴 낱말 사전, 기관 약칭, 회의록에 나온 법률 이름."""
    from .followup import law_names
    texts = [r[0] for r in store.conn.execute("SELECT text FROM utterances")]
    laws = Counter(n for t in texts if "법" in t for n in law_names(t))
    return {
        "rejoin": rejoin_map(texts),
        "orgs": {a: name for name, aliases in ORGANIZATIONS.items() for a in aliases if len(a) >= 2 and a != name},
        # 앞에 한 글자가 붙어 잘못 잡힌 이름('항한국해양진흥공사법')은 뺀다
        "laws": sorted((n for n, c in laws.items() if c >= 2 and len(n) >= 4 and n[1:] not in laws),
                       key=len, reverse=True),
    }
