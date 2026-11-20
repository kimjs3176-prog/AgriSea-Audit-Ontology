"""의원별 질의 분석: 관심 쟁점·질의 관점·핵심 키워드와 과거 질의에 근거한 예상 질문.

범위는 전체 회기, 한 회기, 또는 고른 회의들(한 회의 포함)이다. 키워드는 위원들 사이에서
그 위원만 자주 쓰는 명사(TF-IDF)를 고르며, 말버릇('굉장히', '그러니까')이 섞이지 않도록
회의록 전체에서 조사가 붙어 쓰이는 말(명사)만 후보로 삼는다.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from typing import Any

from .analysis import (_HANGUL, _pair_score, _pair_view, _substantive, shown_promise, tracked_promises,
                       commitment_view, qa_pairs)
from .lexicon import ISSUES, PERSPECTIVES, issue_label, match_organizations
from .nlp import JOSA, STOPWORDS, WORD, _DOMAIN_TERMS, strip_josa, truncate
from .store import Store

VOCAB_VERSION = 4
_JOSA = frozenset(JOSA)
_DOMAIN = frozenset(_DOMAIN_TERMS)
# 명사처럼 조사가 붙지만 관심사를 드러내지 않는 말(발화 습관·회의 진행·일반어)
_FILLER = frozenset("""
얘기 제가 저는 제는 우리가 실제 가지고 거지 거기 여기 그거 이거 저거 사람 사람들 입장 상황 문제점 부분은 측면
내용들 사항들 여러분 여러분들 자체 기본 정부 국가 해당 이번 지난번 작년 금년 올해 내년 당시 최근 현재 향후 앞으로
방청석 위원장석 발언대 속기록 회의록 의사일정 안건 법안 개정안 대안 원안 수정안 질의 답변 자료 시간 오전 오후
장관 차관 청장 차장 위원 위원장 의원 국장 실장 과장 원장 사장 이사장 본부장 기관 기관장 직원 담당 담당자
하나 가지 정도 경우 이유 결과 방법 방향 필요 계획 대책 방안 문제 부분 말씀 생각 의견 질문 확인 검토 노력
이야기 이분 부탁말씀 민주주 잘사 방식 적어 예전 용어 문자 본래 다름 느끼 국무회 토의 토론 증인 참고인 채택 책임자 비서관 보좌관 표결 의결 상정 소위 소위원회 전체회의 간사 기립
""".split())
_ROLE_TAIL = re.compile(r"(장관|차관|청장|차장|원장|사장|이사|실장|국장|과장|본부장|위원|의원|단장)$")
# 활용형·부사성 말('된다', '말이지', '기본적', '뜻이에')과 회의장 위치('조정위원장석옆')
_NOT_NOUN = re.compile(r"(다|지|요|죠|에|으|세|었|았|겠|끼|들|히|든|겠고|했고|해서|것인|니까|는데|거든|적|석옆|석앞)$|위원장|위원석|의원석")


# ── 명사 사전(회의록 전체 기준, 사전 계산) ─────────────────────────────
def build_vocab(store: Store) -> dict:
    """위원 발언에서 조사와 함께 쓰이는 말(명사 후보)과 그 말을 쓰는 위원 수(문서빈도)."""
    rows = store.conn.execute(
        "SELECT speaker_name, text, issues_json FROM utterances WHERE speaker_type='member'").fetchall()
    names = {r[0] for r in store.conn.execute("SELECT DISTINCT speaker_name FROM utterances")}
    josa, total = Counter(), Counter()
    per_member: dict[str, Counter] = defaultdict(Counter)
    issue_cnt: Counter = Counter()
    for name, text, issues in rows:
        issue_cnt.update(json.loads(issues or "{}").keys())
        for w in WORD.findall(text):
            if not _HANGUL.match(w):
                continue
            stem = _stem(w)
            total[stem] += 1
            if stem != w and w[len(stem):] in _JOSA:
                josa[stem] += 1
            per_member[name][stem] += 1
    words = {}
    for stem, j in josa.items():
        if j < 3 or j / total[stem] < 0.25 or len(stem) < 2:
            continue
        if stem in STOPWORDS or stem in _FILLER or _bad_token(stem, names):
            continue
        words[stem] = sum(1 for c in per_member.values() if c[stem] >= 2)
    for term in _DOMAIN:  # 사전 쟁점어는 항상 후보
        words.setdefault(term, sum(1 for c in per_member.values() if c[term] >= 2))
    # 관점별 전체 위원 평균 비율: 누구나 자주 쓰는 관점('피해')이 모든 위원의 1순위가 되지 않도록 비교 기준으로 쓴다
    subst = [t for _, t, _ in rows if len(_HANGUL.findall(t)) >= 20]
    pc = _perspectives([{"text": t} for t in subst])
    base = {p: round(pc[p] / max(1, len(subst)), 4) for p in PERSPECTIVES}
    n_iss = sum(issue_cnt.values()) or 1
    return {"version": VOCAB_VERSION, "n_members": max(1, len(per_member)), "words": words,
            "persp_base": base, "issue_base": {i: round(n / n_iss, 5) for i, n in issue_cnt.items()}}


def _stem(w: str) -> str:
    """조사·어미를 두 겹까지 뗀다('내용보다는' → '내용')."""
    s = strip_josa(w)
    return strip_josa(s) if s != w else s


def _bad_token(tok: str, names: set[str]) -> bool:
    if _NOT_NOUN.search(tok):
        return True
    if len(tok) > 3 and _ROLE_TAIL.search(tok):  # '산림청차장' 같은 직함 붙은 말
        return True
    return any(n and len(n) >= 2 and n in tok for n in names)  # '박은식', '전무이사지준섭'


def vocab(store: Store) -> dict:
    v = store.kv_get("member_vocab")
    if not v or v.get("version") != VOCAB_VERSION:
        v = build_vocab(store)
        store.kv_set("member_vocab", v)
    return v


def _nouns(text: str, words: dict) -> list[str]:
    out = [t for t in _DOMAIN_TERMS if " " in t and t in text]
    for w in WORD.findall(text):
        if _HANGUL.match(w):
            stem = _stem(w)
            if stem in words:
                out.append(stem)
    return out


# ── 위원별 분석 ─────────────────────────────────────────────────
def _scope_utts(store: Store, session: int | None, meeting_ids: list[str] | None) -> list[dict]:
    if meeting_ids:
        return [u for mid in meeting_ids for u in store.utterances(meeting_id=mid)]
    return store.utterances(session=session)


def _perspectives(utts: list[dict]) -> Counter:
    cnt: Counter = Counter()
    for u in utts:
        for pid, (_, rx) in PERSPECTIVES.items():
            if rx.search(u["text"]):
                cnt[pid] += 1
    return cnt


_TEMPLATES = {
    "budget": "「{issue}」 관련 예산 집행 실적은 어떻고, 내년도 예산은 충분히 확보했습니까?",
    "law": "「{issue}」 관련 법·제도 개선은 어디까지 진행됐고, 남은 사각지대는 무엇입니까?",
    "field": "「{issue}」 현장 피해 실태를 파악하고 있습니까? 농어업인 지원·보상 대책은 무엇입니까?",
    "oversight": "「{issue}」 관리·감독 부실의 책임은 누구에게 있고, 재발 방지 대책은 무엇입니까?",
    "safety": "「{issue}」 관련 사고·재난 예방과 대응 체계는 충분히 갖춰져 있습니까?",
    "perf": "「{issue}」 사업의 목표 대비 실적과 실효성을 어떻게 평가합니까?",
    "trade": "「{issue}」 관련 수입·수급 여건 변화에 어떻게 대응하고 있습니까?",
    "region": "「{issue}」 관련 지역 현안 해결을 위해 지자체와 어떻게 협력하고 있습니까?",
}
_DEFAULT_TEMPLATE = "「{issue}」 현황과 앞으로의 구체적인 대책은 무엇입니까?"


def _basis(u: dict) -> dict:
    return {"meeting_id": u["meeting_id"], "date": u.get("meeting_date"),
            "text": truncate(u["text"], 160)}


def _expected_questions(mine: list[dict], top_issues: list[dict], keywords: list[dict],
                        promises: list[dict], base: dict) -> list[dict]:
    """과거 질의 패턴에 근거한 예상 질문: 쟁점×관점, 약속 이행 점검, 반복 제기 키워드."""
    out: list[dict] = []
    for it in top_issues[:3]:
        about = [u for u in mine if it["id"] in u["issues"] and _substantive(u)]
        if not about:
            continue
        pc = _perspectives(about)
        cand = [(n / len(about) / max(base.get(p, 0), 1e-3), p) for p, n in pc.items() if n >= 2]
        pid = max(cand)[1] if cand else None
        best = max(about, key=lambda u: (len(u["issues"]) + (2 if u["is_question"] else 0)
                                          + min(len(u["text"]) / 400, 2), u.get("meeting_date") or ""))
        out.append({"type": "관점", "issue": it["label"],
                    "perspective": PERSPECTIVES[pid][0] if pid else None,
                    "question": (_TEMPLATES.get(pid) or _DEFAULT_TEMPLATE).format(issue=it["label"]),
                    "why": f"「{it['label']}」 발언 {len(about)}건"
                           + (f", 주로 {PERSPECTIVES[pid][0]} 관점" if pid else ""),
                    "basis": _basis(best)})
    for c in promises[:2]:
        out.append({"type": "이행 점검", "issue": (c["issues"] or [None])[0], "perspective": None,
                    "question": f"{(c['date'] or '').replace('-', '.')} 회의에서 정부가 답변한 "
                                f"「{truncate(c['text'], 70)}」의 이행 결과와 향후 일정은 어떻게 됩니까?",
                    "why": f"이 의원 질의에 대한 {c['speaker']} 답변(약속)",
                    "basis": {"meeting_id": c["meeting_id"], "date": c["date"], "text": c["text"]}})
    # 반복 제기: 이 위원이 두드러지게 쓰는 말(여러 위원이 두루 쓰는 말·기관명 제외)
    repeated = sorted((k for k in keywords[:8] if k["meetings"] >= 3 and k["idf"] >= 0.5
                       and not match_organizations(k["word"])), key=lambda k: -k["meetings"])[:2]
    for k in repeated:
        last = max((u for u in mine if k["word"] in u["text"]), key=lambda u: u.get("meeting_date") or "")
        out.append({"type": "반복 제기", "issue": None, "perspective": None,
                    "question": f"「{k['word']}」 문제는 {k['meetings']}개 회의에서 거듭 제기됐습니다. "
                                f"그간 개선된 점과 남은 과제는 무엇입니까?",
                    "why": f"{k['meetings']}개 회의에서 {k['count']}회 언급",
                    "basis": _basis(last)})
    return out


def _profile(name: str, mine: list[dict], pairs: list[dict], words: dict, n_members: int,
             min_count: int, base: dict, issue_base: dict,
             tracked: set[tuple[str, int]] | None = None) -> dict[str, Any]:
    questions = [u for u in mine if u["is_question"]]
    tf, kw_meetings = Counter(), defaultdict(set)
    for u in mine:
        for t in _nouns(u["text"], words):
            tf[t] += 1
            kw_meetings[t].add(u["meeting_id"])
    total = sum(tf.values()) or 1
    scored = []
    for t, c in tf.items():
        if c < min_count and t not in _DOMAIN:  # 사전 쟁점어는 한 번만 나와도 후보
            continue
        idf = math.log((n_members + 1) / (words.get(t, 0) + 1))
        if idf <= 0.05:
            continue
        scored.append((t, c / total * idf * 1000 * (1.5 if t in _DOMAIN else 1.0), c, idf))
    scored.sort(key=lambda x: -x[1])
    keywords = [{"word": t, "count": c, "meetings": len(kw_meetings[t]), "idf": round(idf, 2)}
                for t, _, c, idf in scored[:15]]

    issue_cnt: Counter = Counter()
    for u in mine:
        issue_cnt.update(u["issues"].keys())
    n_iss = sum(issue_cnt.values()) or 1
    top_issues = []
    for i, n in issue_cnt.items():
        if not ISSUES.get(i, (0, None))[1]:
            continue
        lift = n / n_iss / max(issue_base.get(i, 0), 1e-3)
        top_issues.append({"id": i, "label": issue_label(i), "count": n, "lift": round(lift, 2),
                           "parent": issue_label(ISSUES[i][1])})
    # 많이 말한 쟁점 중에서도 다른 위원보다 두드러진 쟁점을 앞에(모두가 말하는 쟁점만 1순위가 되지 않게)
    top_issues.sort(key=lambda x: x["count"] * min(x["lift"], 4) ** 0.5, reverse=True)
    top_issues = top_issues[:8]
    subst = [u for u in mine if _substantive(u)]
    pc = _perspectives(subst)
    perspectives = []
    for p, n in pc.items():
        share = n / max(1, len(subst))
        lift = share / max(base.get(p, 0), 1e-3)
        perspectives.append({"id": p, "label": PERSPECTIVES[p][0], "count": n, "share": round(share, 3),
                             "lift": round(lift, 2)})
    # 전체 위원 평균보다 두드러진 관점 순(표본이 너무 적은 관점은 뒤로)
    perspectives.sort(key=lambda x: (x["count"] >= 2, x["lift"] * min(1.0, x["count"] / 5)), reverse=True)
    orgs: Counter = Counter()
    for u in mine:
        orgs.update(u["orgs"])
    for p in pairs:
        for a in p["answers"]:
            if a.get("org"):
                orgs[a["org"]] += 1
    promises = []
    for p in sorted(pairs, key=lambda p: p["question"].get("meeting_date") or "", reverse=True):
        for a in p["answers"]:
            if a["is_commitment"]:
                v = commitment_view(a)
                if shown_promise(tracked, a, v["text"]):
                    promises.append({**v, "question": _pair_view(p)["question"]})
                break
    meetings = sorted({u["meeting_id"] for u in mine})
    top_p = [p["label"] for p in perspectives[:2] if p["lift"] >= 1]
    headline = (f"{'·'.join('「' + i['label'] + '」' for i in top_issues[:2]) or '여러'} 쟁점을 "
                + (f"{', '.join(top_p)} 관점에서 " if top_p else "")
                + ("주로 질의" if questions else "주로 발언"))
    return {
        "name": name,
        "counts": {"utterances": len(mine), "questions": len(questions), "meetings": len(meetings),
                   "data_requests": sum(1 for u in mine if u["is_data_request"]),
                   "promises": len(promises)},
        "headline": headline,
        "top_issues": top_issues,
        "perspectives": perspectives,
        "keywords": keywords,
        "orgs": [{"name": o, "count": n} for o, n in orgs.most_common(6)],
        "by_session": [{"session": s, "count": n} for s, n in
                       sorted(Counter(u["session_no"] for u in questions if u.get("session_no")).items())],
        "expected_questions": _expected_questions(mine, top_issues, keywords, promises, base),
        "top_questions": [_pair_view(p) for p in sorted(pairs, key=_pair_score, reverse=True)[:4]],
        "promises": promises[:8],
        "data_requests": [_basis(u) for u in sorted((u for u in mine if u["is_data_request"]),
                                                    key=lambda u: u.get("meeting_date") or "",
                                                    reverse=True)[:5]],
        "meetings": meetings,
    }


def member_profiles(store: Store, session: int | None = None,
                    meeting_ids: list[str] | None = None) -> dict[str, Any]:
    """범위 안에서 질의한 모든 위원의 분석 결과 {scope, members: {이름: 분석}}."""
    v = vocab(store)
    tracked = tracked_promises(store)
    utts = _scope_utts(store, session, meeting_ids)
    by_member: dict[str, list[dict]] = defaultdict(list)
    for u in utts:
        if u["speaker_type"] == "member" and u["speaker_name"]:
            by_member[u["speaker_name"]].append(u)
    by_meeting: dict[str, list[dict]] = defaultdict(list)
    for u in utts:
        by_meeting[u["meeting_id"]].append(u)
    pairs_by: dict[str, list[dict]] = defaultdict(list)
    for mu in by_meeting.values():
        for p in qa_pairs(mu):
            pairs_by[p["question"]["speaker_name"]].append(p)
    n_meet = len(by_meeting)
    min_count = 2 if n_meet <= 5 else 3
    members = {}
    for name, mine in by_member.items():
        if not any(_substantive(u) for u in mine):
            continue
        members[name] = _profile(name, mine, pairs_by.get(name, []), v["words"],
                                 v["n_members"], min_count, v["persp_base"], v["issue_base"], tracked)
    return {"scope": {"session": session, "meetings": meeting_ids or [], "n_meetings": n_meet},
            "members": members}


def summary_rows(profiles: dict) -> list[dict]:
    """목록용 요약(질의 많은 순)."""
    rows = [{"name": p["name"], "counts": p["counts"], "headline": p["headline"],
             "top_issues": p["top_issues"][:3], "perspectives": p["perspectives"][:2],
             "keywords": [k["word"] for k in p["keywords"][:5]]}
            for p in profiles["members"].values()]
    rows.sort(key=lambda r: (-r["counts"]["questions"], -r["counts"]["utterances"], r["name"]))
    return rows
