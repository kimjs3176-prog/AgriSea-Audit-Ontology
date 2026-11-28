"""기관 맞춤 대시보드: 기관 하나(또는 전체)를 고르면 그 기관에 대한 질의·답변·약속·쟁점·질의 위원을 모아 보여 준다.

질의 대상 기관은 질의에 이어 답한 정부측 발언자의 소속 기관으로 정하고, 답변자 소속이 없으면 질의가 언급한
기관으로 본다(지식그래프 ag:addressedTo 와 같은 기준). 모든 기관을 한 번에 계산해 사전 계산(kv)에 싣는다.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from .analysis import _main_answer, _pair_score, _pair_view, _substantive, qa_pairs
from .lexicon import ISSUES, ORGANIZATIONS, issue_label
from .store import Store

ALL = ""            # 전체 기관
RECENT_SESSIONS = 2  # '최근' 비교 구간: 질의가 있었던 마지막 회기 2개
OPEN_STATUSES = ("none", "reraised")   # 국감에서 물어볼 약속(근거 미확인·재지적)
_PARENTS = {p for _, p, _ in ISSUES.values() if p}


def question_targets(pair: dict) -> set[str]:
    """질의 대상 기관: 답한 정부측 발언자의 소속 기관, 없으면 질의가 언급한 기관(소관 기관만)."""
    by_answer = {a["org"] for a in pair["answers"] if a.get("org") in ORGANIZATIONS}
    return by_answer or {o for o in pair["question"]["orgs"] if o in ORGANIZATIONS}


def _leaf_issues(u: dict) -> list[str]:
    ids = [i for i in u["issues"] if i in ISSUES]
    return [i for i in ids if i not in _PARENTS or not any(ISSUES[j][1] == i for j in ids)]


def build(store: Store, followups: dict | None = None) -> dict[str, Any]:
    """{'orgs': [기관 순위], 'views': {기관명 또는 '': 대시보드 자료}}."""
    utts = store.utterances()
    by_meeting: dict[str, list[dict]] = defaultdict(list)
    for u in utts:
        by_meeting[u["meeting_id"]].append(u)
    pairs = [p for mu in by_meeting.values() for p in qa_pairs(mu)]

    groups: dict[str, list[dict]] = defaultdict(list)   # 기관 → 그 기관에 대한 질의 묶음
    for p in pairs:
        groups[ALL].append(p)
        for o in question_targets(p):
            groups[o].append(p)
    answers = Counter(u["org"] for u in utts if u["speaker_type"] in ("official", "witness") and u["org"])
    answers[ALL] = sum(answers.values())

    fu_items = (followups or store.kv_get("followups") or {}).get("items", [])
    fu_by_org: dict[str, list[dict]] = defaultdict(list)
    for x in fu_items:
        fu_by_org[ALL].append(x)
        if x.get("org"):
            fu_by_org[x["org"]].append(x)
    statuses = (followups or store.kv_get("followups") or {}).get("statuses", {})

    sessions = sorted({u["session_no"] for u in utts if u.get("session_no")})
    meetings = {m["id"]: m for m in store.meetings()}
    views = {org: _view(org, groups.get(org, []), answers.get(org, 0), fu_by_org.get(org, []),
                        statuses, sessions, meetings)
             for org in [ALL, *ORGANIZATIONS]}
    ranking = sorted(({"name": o, "questions": v["counts"]["questions"], "open": v["counts"]["open"],
                       "commitments": v["counts"]["commitments"]} for o, v in views.items() if o),
                     key=lambda r: (-r["questions"], r["name"]))
    return {"orgs": ranking, "views": views}


def _view(org: str, pairs: list[dict], n_answers: int, fus: list[dict], statuses: dict,
          sessions: list[int], meetings: dict) -> dict[str, Any]:
    qs = [p["question"] for p in pairs]
    by_session = Counter(q["session_no"] for q in qs if q.get("session_no"))
    active = [s for s in sessions if by_session.get(s)]
    recent = set(active[-RECENT_SESSIONS:])

    # 쟁점: 전체 건수와 최근 회기 건수, 최근에 비중이 커졌으면 '늘어남'
    total, near = Counter(), Counter()
    for q in qs:
        for i in _leaf_issues(q):
            total[i] += 1
            if q.get("session_no") in recent:
                near[i] += 1
    n_near = sum(1 for q in qs if q.get("session_no") in recent)
    n_far = len(qs) - n_near
    issues = []
    for iid, n in total.most_common(10):
        far = n - near[iid]
        rising = bool(n_near and n_far and near[iid] >= 3 and near[iid] / n_near >= 1.5 * far / n_far)
        issues.append({"id": iid, "label": issue_label(iid), "count": n, "recent": near[iid], "rising": rising})

    # 질의 위원: 질의 수와 그 위원이 가장 많이 물은 쟁점
    members = Counter(q["speaker_name"] for q in qs)
    member_issue: dict[str, Counter] = defaultdict(Counter)
    for q in qs:
        member_issue[q["speaker_name"]].update(_leaf_issues(q))
    top_members = [{"name": n, "count": c,
                    "issue": issue_label(member_issue[n].most_common(1)[0][0]) if member_issue[n] else ""}
                   for n, c in members.most_common(8)]

    # 최근 질의·답변: 최근 2개 회기에서 실질 답변이 있고 쟁점·약속이 드러난 질의 5개를 골라 날짜순
    # ('그냥 렌더링하나요?' 같은 짧은 되묻기보다 내용 있는 질의응답을 보여 준다)
    when = lambda p: (p["question"].get("meeting_date") or "", p["question"]["meeting_id"], p["question"]["idx"])
    answered = [p for p in pairs if (a := _main_answer(p)) and _substantive(a)]
    pool = [p for p in answered if p["question"].get("session_no") in recent] or answered
    picked = sorted(pool, key=lambda p: (_pair_score(p), when(p)), reverse=True)[:5]
    recent_qa = [{**_pair_view(p), "idx": p["question"]["idx"]} for p in sorted(picked, key=when, reverse=True)]
    for r in recent_qa:
        r.pop("question_full", None)

    # 질의를 받은 최근 회의
    per_meeting = Counter(q["meeting_id"] for q in qs)
    recent_meetings = sorted(per_meeting, key=lambda mid: (meetings[mid]["date"] or "", mid), reverse=True)[:6]

    # 약속 이행: 상태별 수, 국감에서 물어볼 약속 최근 순
    st = Counter(x["status"] for x in fus)
    open_items = sorted((x for x in fus if x["status"] in OPEN_STATUSES),
                        key=lambda x: (x["date"] or "", x["id"]), reverse=True)

    dates = [q.get("meeting_date") for q in qs if q.get("meeting_date")]
    return {
        "org": org,
        "counts": {"questions": len(qs), "answers": n_answers, "commitments": len(fus),
                   "open": sum(st[s] for s in OPEN_STATUSES), "meetings": len(per_meeting),
                   "members": len(members), "last_date": max(dates) if dates else None},
        "by_session": [{"session": s, "count": by_session.get(s, 0)} for s in sessions],
        "issues": issues,
        "members": top_members,
        "recent_qa": recent_qa,
        "meetings": [{"id": mid, "date": meetings[mid]["date"], "session_no": meetings[mid]["session_no"],
                      "title": meetings[mid]["title"], "questions": per_meeting[mid]} for mid in recent_meetings],
        "followups": {"counts": {k: st.get(k, 0) for k in statuses},
                      "labels": {k: v["label"] for k, v in statuses.items()},
                      "open": [{"id": x["id"], "date": x["date"], "org": x.get("org", ""), "speaker": x["speaker"],
                                "text": x["text"], "status": x["status"], "status_label": x["status_label"],
                                "member": (x.get("question") or {}).get("member")} for x in open_items[:5]]},
    }
