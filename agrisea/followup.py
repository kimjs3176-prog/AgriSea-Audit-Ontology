"""정부 약속 이행 추적: 회의에서 정부·기관이 한 약속이 그 뒤 실제로 이행됐는지 근거를 모아 판정한다.

약속마다 '무엇을 하겠다는지'(앞선 위원 질의와 약속 문장의 핵심 명사, 법률명, 조치 유형)를 뽑고,
그 뒤의 기록에서 근거를 찾는다.

- 후속 회의록(사전 계산): 같은 기관이 뒤 회의에서 그 일을 '마련했다·개정했다'고 보고(이행 보고),
  '추진 중·검토 중'이라 답함(진행 중), 위원이 같은 문제를 다시 따짐(재지적)
- 후속 안건(사전 계산): 약속에 나온 법률의 개정법률안이 뒤 회의 안건으로 올라옴(진행 중)
- 국회 의안·지원사업 공고(실시간, external.py): 약속 뒤 발의된 같은 법률 개정안과 처리 결과,
  약속 뒤 게시된 같은 주제의 지원사업 공고

판정은 기록에서 찾은 '신호'다. 공식 이행 여부는 정부의 서면 답변·시정조치 결과보고서로 확인해야 한다.
"""
from __future__ import annotations

import json
import math
import re
from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import date
from typing import Any

from .analysis import _question_text
from .lexicon import ORGANIZATIONS, commitment_sentences, issue_label
from .members import _nouns, vocab
from .nlp import SENT_SPLIT, WORD, strip_josa, truncate
from .store import Store

# 조치 유형: 약속 문장에서 무엇을 하겠다는지(위에서부터 먼저 맞는 것)
ACTIONS: list[tuple[str, str, re.Pattern]] = [
    ("law", "법·제도 개정", re.compile(r"법률|법령|법을|법안|시행령|시행규칙|고시|입법|개정")),
    ("budget", "예산·지원", re.compile(r"예산|편성|증액|국비|기금|보조|지원")),
    ("report", "자료 제출·보고", re.compile(r"제출|자료|서면|보고")),
    ("inspect", "점검·조사", re.compile(r"점검|조사|감사|감독|실태|파악|확인")),
    ("improve", "제도·운영 개선", re.compile(r"개선|보완|마련|정비|강화|도입|구축|시정|반영")),
    ("review", "검토·협의", re.compile(r".")),
]
ACTION_LABEL = {k: label for k, label, _ in ACTIONS}

STATUS = {
    "done": ("이행 보고", "뒤 회의에서 같은 기관이 조치를 마쳤다고 보고했거나 관련 법률안이 가결됨"),
    "progress": ("진행 중", "뒤 회의에서 추진·검토 중이라고 답했거나 관련 안건·의안·공고가 이어짐"),
    "reraised": ("재지적", "뒤 회의에서 위원이 같은 문제를 다시 지적(이후 진행 근거 없음) → 미이행 의심"),
    "early": ("확인 기간 부족", "약속한 지 120일이 지나지 않아 후속 기록을 판단하기 이름"),
    "none": ("근거 미확인", "뒤 회의록·안건에서 관련 근거를 찾지 못함 → 국감에서 이행 여부를 물어볼 대상"),
}
STATUS_ORDER = list(STATUS)
EARLY_DAYS = 120

# 지난 일로 말한 조치(이행 보고): '마련했습니다', '개정되었습니다', '반영돼 있습니다'
DONE = re.compile(r"(완료|마련|시행|개정|반영|편성|확보|도입|개선|조치|제출|발표|설치|확대|착수|구축|배정|지급|"
                  r"정비|공포|수립|신설|보완|증액)(을|를|이|가|도)?\s?(했습니다|하였습니다|했고|하였고|했으며|하였으며|"
                  r"됐습니다|되었습니다|됐고|되었고|돼\s?있|되어\s?있)")
PROGRESS = re.compile(r"(추진|진행|검토|협의|준비|마련|개정|연구|용역|논의|작업|조율|협의)\s?(중|하고\s?있|을\s?하고\s?있|를\s?하고\s?있)"
                      r"|입법예고|계획(입니다|이고|을\s?세우)")
# 위원이 같은 문제를 다시 따질 때 쓰는 말: '아직도', '지난번에 약속하셨는데', '작년 국감 때'
RERAISE = re.compile(r"아직|여전히|지금까지|그대로|약속(하셨|했|을\s?하|을\s?했)|지난번|지난해|작년|국정감사\s?때|국감\s?때|"
                     r"이행(이|을|하지|되지|이\s?안)|개선(이)?\s?안|안\s?되고|안\s?됐|진척|달라진\s?게|바뀐\s?게")

# 대상을 드러내지 않는 말: 키워드에서 뺀다(조치 동사·일반어)
_GENERIC = frozenset("""
제도 제도개선 개선 보완 검토 협의 추진 마련 반영 지원 확대 강화 예산 사업 정책 조치 점검 조사 확인 파악 보고 제출 자료
시정 지적 취지 공감 말씀 의견 사항 부분 내용 문제 방안 대책 계획 관리 운영 기준 대상 현장 필요 상황 결과 과정 이후 향후
정부 부처 기관 위원 위원님 장관 차관 청장 국회 국정감사 결산 심사 소위 법안 법률 개정 시행 관련 저희 저희들 우리 지금
법안소위 법안심사 서비스 뒷받침 우리나라 대통령 국정과제 드리고 마지막 특성 전략 주변 공간 민생 인프라 개소 운동 전개
업무보고 추경 추경안 정부안 행동 모델 중점 비전 과제들 주요 국민들 현장에서 앞으로도
""".split())
# 두 글자 말 중 대상이 아닌 것(지시어·정도·일반 명사): 두 글자 말은 이 목록에 없고 드물 때만 대상어로 쓴다
_FILLER2 = frozenset("""
군데 중간 그쪽 이쪽 저쪽 차원 같아 않아 반대 인정 염려 인식 기능 체계 제고 활력 과제 소관 지역 농민 규정 조항 확보 증액
설계 수준 단계 분야 측면 상태 효과 성과 실적 목표 범위 규모 구조 형태 시기 기간 일정 금액 비용 재정 대응 강구 국민 현실
조치 지침 원칙 근거 기존 신규 추가 별도 일부 전체 대부분 처음 마지막 나중 순간 당장 시점 의미 취지 원인 이유 상당 상황
""".split())
_NOT_LAW = frozenset({"방법", "불법", "위법", "편법", "해법", "적법", "합법", "입법", "사법", "어법", "문법", "탈법",
                      "수법", "기법", "용법", "화법", "작법", "필법", "세법상",
                      # 어느 법인지 드러나지 않는 말: 이 말로 찾으면 모든 기본법·특별법이 걸린다
                      "기본법", "특별법", "특례법", "개정법", "관련법", "해당법", "현행법", "상위법", "하위법", "일반법",
                      "모법", "본법", "신법", "구법", "이법", "그법", "법률법", "시행법", "제정법", "실정법"})
_ORG_WORDS = frozenset(x for n, al in ORGANIZATIONS.items() for x in (n, *al))
_ANSWERERS = ("official", "witness")


# 명사 사전에 섞여 든 활용형('그러셨잖아', '것같아', '있으므')
_VERBISH = re.compile(r"(잖아|같아|아요|어요|으므|니까|는데|거든|때문|없나|셨|었|았|겠|하)$")


def _strong(n: str, c: "Corpus") -> bool:
    """대상을 분명히 가리키는 말: 드물게 쓰이는(회의록 250발언에 하나 이하) 세 글자 이상 복합 명사('사업계획',
    '이격거리', '산란계'). '물류', '어업인', '산불'처럼 넓거나 늘 나오는 말은 혼자서 같은 일을 가리킨다고 보지 않는다."""
    return len(n) >= 3 and c.idf(n) >= 5.5


def _distinct(found: list[str]) -> list[str]:
    """'어선원'이 맞았을 때 그 안의 '어선'은 따로 세지 않는다."""
    return [k for k in found if not any(k != o and k in o for o in found)]


def action_of(text: str) -> str:
    return next(k for k, _, pat in ACTIONS if pat.search(text))


def law_names(text: str) -> list[str]:
    """'수의사법', '농업소득보전법' 같은 법률 이름(일반어 '방법·불법' 제외)."""
    out = []
    for w in WORD.findall(text):
        s = strip_josa(w)
        s = s[:-1] if s.endswith("법률") else s
        if (len(s) >= 3 and s.endswith("법") and s not in _NOT_LAW and not s.endswith(("방법", "불법", "위법"))
                and s not in out):
            out.append(s)
    return out[:3]


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in SENT_SPLIT.split(text) if len(s.strip()) >= 8]


class Corpus:
    """후속 근거 검색용: 전체 발언(날짜순), 명사 색인, 문서빈도, 회의별 안건."""

    def __init__(self, store: Store):
        words = vocab(store)["words"]
        rows = store.conn.execute(
            "SELECT u.id, u.meeting_id, u.idx, u.speaker_name, u.speaker_role, u.speaker_type, u.org, "
            "u.text, u.is_question, u.is_commitment, u.issues_json, u.orgs_json, "
            "m.date AS date, m.session_no AS session_no, m.title AS title "
            "FROM utterances u JOIN meetings m ON m.id=u.meeting_id "
            "WHERE m.date IS NOT NULL AND m.date != '' ORDER BY m.date, u.meeting_id, u.idx").fetchall()
        self.utts = [dict(r) for r in rows]
        self.dates = [u["date"] for u in self.utts]
        self.index: dict[str, list[int]] = defaultdict(list)
        df: Counter = Counter()
        for i, u in enumerate(self.utts):
            nouns = set(_nouns(u["text"], words))
            for n in nouns:
                self.index[n].append(i)
            df.update(nouns)
        self.df, self.n = df, max(1, len(self.utts))
        self.words = words
        self.names = {u["speaker_name"] for u in self.utts}
        self.meetings = [dict(r) for r in store.conn.execute(
            "SELECT id, date, title, agendas_json FROM meetings WHERE date IS NOT NULL ORDER BY date")]
        for m in self.meetings:
            m["agendas"] = json.loads(m.pop("agendas_json") or "[]")
        self.as_of = max(self.dates) if self.dates else date.today().isoformat()

    def idf(self, w: str) -> float:
        return math.log(self.n / (self.df.get(w, 0) + 1))


def _question_before(c: Corpus, pos: int) -> dict | None:
    """약속 바로 앞(같은 회의, 6발언 이내)의 위원 질의."""
    u = c.utts[pos]
    for j in range(pos - 1, max(-1, pos - 7), -1):
        x = c.utts[j]
        if x["meeting_id"] != u["meeting_id"]:
            break
        if x["speaker_type"] == "member":
            return x
    return None


def _keywords(c: Corpus, promise: str, question: str, exclude: set[str], k: int = 5) -> list[str]:
    """약속의 대상어: 약속 문장(가중 2)과 앞 질의(1)의 명사 중 회의록 전체에서 드문 말(IDF) 순."""
    tf: Counter = Counter()
    for text, w in ((promise, 2.0), (question, 1.0)):
        for n in _nouns(text, c.words):
            tf[n] += w
    scored = []
    for n, f in tf.items():
        if n in _GENERIC or n in exclude or n in _ORG_WORDS or len(n) < 2 or _VERBISH.search(n):
            continue
        if any(nm and len(nm) >= 2 and nm in n for nm in c.names):
            continue
        idf = c.idf(n)
        if idf < 3.0:  # 회의록 스무 발언에 하나 이상 나오는 흔한 말은 대상어로 보기 어렵다
            continue
        strong = _strong(n, c)
        if not strong and (len(n) < 3 and (n in _FILLER2 or idf < 6.0 or re.search(r"[아어해게지고요]$", n))):
            continue
        scored.append((f * idf * (1.5 if strong else 1.0), n))
    scored.sort(reverse=True)
    return [n for _, n in scored[:k]]


def _evidence_sentence(c: "Corpus", text: str, kws: list[str], laws: list[str], pattern: re.Pattern) -> tuple[str, int]:
    """근거 발언에서 같은 일을 가리키는 문장: 대상어 둘 이상(그중 하나는 분명한 말)이나 법률 이름이 들고
    pattern(이행·진행·재지적 표현)이 맞는 문장 중 대상어가 가장 많은 것. 없으면 ('', 0)."""
    best, best_hits = "", 0
    for s in _sentences(text):
        if not pattern.search(s):
            continue
        found = _distinct([k for k in kws if k in s])
        strong = sum(1 for k in found if _strong(k, c))
        law = any(l in s for l in laws)
        # 이행 보고는 더 엄격히: 분명한 대상어 둘 이상, 또는 법률 이름(업무보고의 넓은 말 겹침을 이행으로 보지 않도록)
        ok = (strong >= 2 or law) if pattern is DONE else ((strong >= 1 and len(found) >= 2) or law)
        if not ok:
            continue
        hits = len(found) + strong + 2 * law
        if hits > best_hits:
            best, best_hits = s, hits
    return (truncate(best, 220), best_hits) if best else ("", 0)


def _minutes_evidence(c: Corpus, item: dict, pos: int) -> list[dict]:
    kws, laws, org = item["keywords"], item["laws"], item["org"]
    start = bisect_right(c.dates, item["date"])  # 약속한 회의 다음 날부터
    hits: Counter = Counter()
    for k in kws:
        for i in c.index.get(k, ()):
            if i >= start:
                hits[i] += 1
    for law in laws:  # 법률 이름은 색인(명사 사전)에 없을 수 있어 직접 찾는다
        for i in range(start, len(c.utts)):
            if law in c.utts[i]["text"]:
                hits[i] += 2
    found: dict[str, list[tuple]] = defaultdict(list)
    for i, h in hits.items():
        if h < 2:
            continue
        u = c.utts[i]
        if u["speaker_type"] in _ANSWERERS:
            if org and u["org"] and u["org"] != org:
                continue
            sent, h = _evidence_sentence(c, u["text"], kws, laws, DONE)
            kind = "done"
            if not sent:
                sent, h = _evidence_sentence(c, u["text"], kws, laws, PROGRESS)
                kind = "progress"
        elif u["speaker_type"] == "member":
            if org and org not in u["orgs_json"] and h < 3:
                continue
            sent, h = _evidence_sentence(c, u["text"], kws, laws, RERAISE)
            kind = "reraised"
        else:
            continue
        if sent:
            found[kind].append((h, u["date"], u, sent))
    out = []
    for kind, xs in found.items():
        xs.sort(key=lambda x: (x[0], x[1]), reverse=True)
        seen_meetings = set()
        for h, _, u, sent in xs:
            if u["meeting_id"] in seen_meetings:
                continue
            seen_meetings.add(u["meeting_id"])
            who = f"{u['speaker_name']} 위원" if u["speaker_type"] == "member" else f"{u['speaker_role']} {u['speaker_name']}"
            out.append({"source": "minutes", "kind": kind, "date": u["date"], "meeting_id": u["meeting_id"],
                        "idx": u["idx"], "title": who, "text": sent, "score": h})
            if len(seen_meetings) >= 3:
                break
    return out


def _bill_key(agenda: str) -> str:
    m = re.search(r"의안번호\s*(\d+)", agenda)
    return m.group(1) if m else agenda


def _agenda_evidence(c: Corpus, item: dict) -> list[dict]:
    """약속에 나온 법률의 개정법률안이 뒤 회의 안건으로 올라옴(같은 의안은 처음 상정된 회의 한 번만)."""
    out, seen = [], set()
    for m in c.meetings:
        if m["date"] <= item["date"]:
            continue
        hit = next((a for a in m["agendas"] for law in item["laws"]
                    if law in a and "법률안" in a and _bill_key(a) not in seen), None)
        if hit:
            seen.add(_bill_key(hit))
            out.append({"source": "agenda", "kind": "progress", "date": m["date"], "meeting_id": m["id"],
                        "title": "후속 안건 상정", "text": truncate(re.sub(r"^\s*\d+\s*\.\s*", "", hit), 160)})
            if len(out) >= 3:
                break
    return out


def verdict(item_date: str, evidence: list[dict], as_of: str) -> str:
    kinds = defaultdict(list)
    for e in evidence:
        kinds[e["kind"]].append(e["date"] or "")
    if kinds["done"]:
        return "done"
    last_re, last_prog = max(kinds["reraised"], default=""), max(kinds["progress"], default="")
    if last_re and last_re >= last_prog:
        return "reraised"
    if last_prog:
        return "progress"
    try:
        if (date.fromisoformat(as_of) - date.fromisoformat(item_date)).days < EARLY_DAYS:
            return "early"
    except ValueError:
        pass
    return "none"


def _item(c: Corpus, pos: int) -> dict | None:
    u = c.utts[pos]
    sents = commitment_sentences(u["text"])
    if not sents:
        return None
    promise = " ".join(sents[:2])
    q = _question_before(c, pos)
    qtext = _question_text(q["text"]) if q else ""
    exclude = {u["speaker_name"], *(q and [q["speaker_name"]] or [])}
    kws = _keywords(c, promise, qtext, exclude)
    laws = law_names(promise) or law_names(qtext)
    if len(kws) < 2 and not laws:  # 무엇을 하겠다는지 드러나지 않는 약속('지적사항 반영하겠습니다')
        return None
    return {
        "id": u["id"], "meeting_id": u["meeting_id"], "idx": u["idx"], "date": u["date"],
        "session": u["session_no"], "meeting_title": u["title"],
        "speaker": f"{u['speaker_role']} {u['speaker_name']}", "org": u["org"] or "",
        "text": truncate(promise, 300),
        "question": {"member": q["speaker_name"], "text": qtext} if q else None,
        "issues": [issue_label(i) for i in json.loads(u["issues_json"] or "{}")][:3],
        "action": action_of(promise), "keywords": kws, "laws": laws,
    }


def finalize(item: dict, evidence: list[dict], as_of: str) -> dict:
    evidence = sorted(evidence, key=lambda e: (e["date"] or "", e.get("source", "")))
    st = verdict(item["date"], evidence, as_of)
    return {**item, "evidence": evidence, "status": st, "status_label": STATUS[st][0],
            "action_label": ACTION_LABEL[item["action"]]}


def track_all(store: Store) -> dict[str, Any]:
    """모든 약속의 이행 추적(사전 계산 대상). 대상이 드러나지 않는 약속은 개수만 센다."""
    c = Corpus(store)
    items, vague = [], 0
    for pos, u in enumerate(c.utts):
        if not u["is_commitment"] or u["speaker_type"] not in _ANSWERERS:
            continue
        it = _item(c, pos)
        if it is None:
            vague += 1
            continue
        ev = _minutes_evidence(c, it, pos) + (_agenda_evidence(c, it) if it["laws"] else [])
        items.append(finalize(it, ev, c.as_of))
    items.sort(key=lambda x: (x["date"], x["idx"]), reverse=True)
    counts = Counter(x["status"] for x in items)
    return {"as_of": c.as_of, "vague": vague,
            "counts": {s: counts.get(s, 0) for s in STATUS_ORDER},
            "statuses": {s: {"label": l, "desc": d} for s, (l, d) in STATUS.items()},
            "actions": ACTION_LABEL, "items": items}
