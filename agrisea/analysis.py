"""회의 요약, 핵심안건 정리, 국정감사 대비 브리핑 생성(규칙 기반)."""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from typing import Any

from .lexicon import ISSUES, ORGANIZATIONS, QUESTION_PATTERNS, commitment_sentences, issue_label
from .nlp import SENT_SPLIT, extractive_summary, keywords, split_sentences, tokenize, truncate
from .store import Store

# 짧은 맞장구·인사: '예, 그렇습니다', '수고하셨습니다' — 대화의 실질 내용이 아니다
_HANGUL = re.compile(r"[가-힣]")
# 약속 문장에서 대상 없이 쓰이는 말: 이 말만 있으면 '무엇을' 하겠다는지 알 수 없다
_GENERIC_PROMISE = frozenset("""지적 취지 공감 시정 검토 말씀 위원님 위원장님 노력 적극 적극적 반영 추진 저희 저희들 부분 그런
    같이 하겠습니다 드리겠습니다 도록 최선 잘 더 좀 해서 관련 사항 내용 의견 부탁 알겠습니다 그렇게 이런 우리""".split())


def _substantive(u: dict, min_hangul: int = 20) -> bool:
    return len(_HANGUL.findall(u["text"])) >= min_hangul


def _specific_promise(text: str) -> bool:
    """'지적하신 취지에 공감하고 시정하겠습니다'처럼 대상이 없는 약속은 제외(무엇을 할지 드러나는 약속만)."""
    return len([t for t in tokenize(text) if t not in _GENERIC_PROMISE]) >= 3


def key_point_items(utts: list[dict], n: int) -> list[dict]:
    """중요 문장 n개를 누가 한 말인지와 함께: 화면에서 발언자·쟁점을 붙인 카드로 보여 주기 위함."""
    owner: dict[str, dict] = {}
    for u in utts:
        for sent in split_sentences(u["text"]):
            owner.setdefault(sent, u)
    out = []
    # 발언이 문장부호 없이 끊겨도('제가 좀……') 다음 발언과 한 문장으로 붙지 않게 마침표를 둔다
    joined = " ".join(u["text"] if u["text"].rstrip().endswith((".", "?", "!")) else u["text"] + "."
                      for u in utts)
    for sent in extractive_summary(joined, n):
        # 문장 경계가 발언 경계와 어긋난 경우(앞 발언 끝과 이어져 잘림) 본문 포함 여부로 찾는다
        u = owner.get(sent) or next((x for x in utts if sent[:40] in x["text"]), None) \
            or next((x for x in utts if sent[:12] in x["text"]), None)
        out.append({"text": sent,
                    "speaker": (u["speaker_name"] + " 위원" if u["speaker_type"] == "member"
                                else f"{u['speaker_role']} {u['speaker_name']}") if u else "",
                    "speaker_type": u["speaker_type"] if u else "",
                    "meeting_id": u["meeting_id"] if u else None,
                    "date": u.get("meeting_date") if u else None,
                    "issues": [issue_label(i) for i in (u["issues"] if u else {})][:2]})
    return out


def _issue_rank(utts: list[dict], top: int = 8) -> list[dict]:
    cnt: Counter = Counter()
    for u in utts:
        cnt.update(u["issues"])
    out = []
    for iid, n in cnt.most_common():
        parent = ISSUES[iid][1]
        out.append({"id": iid, "label": issue_label(iid), "count": n,
                    "parent": issue_label(parent) if parent else None})
    return out[:top]


def _names(utts: list[dict]) -> set[str]:
    return {u["speaker_name"] for u in utts} | {u["speaker_role"] for u in utts}


def qa_pairs(utts: list[dict]) -> list[dict]:
    """위원 질의 → 이어지는 정부측 답변 묶음."""
    pairs: list[dict] = []
    cur: dict | None = None
    for u in utts:
        if u["speaker_type"] == "member" and u["is_question"]:
            cur = {"question": u, "answers": []}
            pairs.append(cur)
        elif u["speaker_type"] in ("official", "witness") and cur is not None:
            cur["answers"].append(u)
        elif u["speaker_type"] == "chair":
            cur = None  # 위원장 발언 → 질의 순서 전환
    return pairs


def _main_answer(p: dict) -> dict | None:
    """질의에 대한 대표 답변: '예, 그렇습니다' 같은 맞장구가 아닌 첫 실질 답변(약속이면 우선)."""
    ans = p["answers"]
    return (next((a for a in ans if a["is_commitment"]), None)
            or next((a for a in ans if _substantive(a)), None)
            or (ans[0] if ans else None))


def _pair_view(p: dict) -> dict:
    q = p["question"]
    a = _main_answer(p)
    return {
        "meeting_id": q["meeting_id"],
        "date": q.get("meeting_date"),
        "member": q["speaker_name"],
        "question": _question_text(q["text"]),
        "question_full": q["text"],
        "answerer": f"{a['speaker_role']} {a['speaker_name']}" if a else None,
        "answer": _answer_text(a) if a else None,
        "commitment": any(x["is_commitment"] for x in p["answers"]),
        "issues": [issue_label(i) for i in q["issues"]],
    }


def _pair_score(p: dict) -> float:
    q, a = p["question"], _main_answer(p)
    return (len(q["issues"]) * 2 + len(q["orgs"]) + min(len(q["text"]) / 300, 3)
            + (2 if any(x["is_commitment"] for x in p["answers"]) else 0)
            + (1 if q["is_data_request"] else 0)
            # 실질 답변이 없는 질의(답변 없음·'예' 한마디)는 주요 질의응답으로 뽑지 않는다
            + (2 if a and _substantive(a) else -6))


def commitment_view(u: dict) -> dict:
    return {
        "meeting_id": u["meeting_id"], "date": u.get("meeting_date"),
        "speaker": f"{u['speaker_role']} {u['speaker_name']}", "org": u["org"],
        "text": truncate(" ".join(commitment_sentences(u["text"])[:2]) or u["text"], 260),
        "issues": [issue_label(i) for i in u["issues"]],
    }


def summarize_meeting(store: Store, meeting_id: str) -> dict[str, Any]:
    m = store.meeting(meeting_id)
    if not m:
        raise KeyError(meeting_id)
    utts = store.utterances(meeting_id=meeting_id)
    members = Counter(u["speaker_name"] for u in utts if u["speaker_type"] == "member")
    officials = Counter(f"{u['speaker_role']} {u['speaker_name']}" for u in utts
                        if u["speaker_type"] in ("official", "witness"))
    n_q = sum(u["is_question"] for u in utts)
    n_c = sum(u["is_commitment"] for u in utts)
    substantive = [u for u in utts if u["speaker_type"] != "chair"]
    full_text = " ".join(u["text"] for u in substantive)

    agenda_summaries = []
    for i, a in enumerate(m["agendas"]):
        a_utts = [u for u in substantive if u["agenda_idx"] == i]
        if not a_utts:
            agenda_summaries.append({"agenda": a, "summary": [], "speakers": [], "issues": []})
            continue
        agenda_summaries.append({
            "agenda": a,
            "summary": extractive_summary(" ".join(u["text"] for u in a_utts), 3),
            "speakers": [n for n, _ in Counter(u["speaker_name"] for u in a_utts).most_common(6)],
            "issues": [x["label"] for x in _issue_rank(a_utts, 4)],
        })

    pairs = sorted(qa_pairs(utts), key=_pair_score, reverse=True)
    overview = (f"{m['date']} {m['title']} — 안건 {len(m['agendas'])}건, 발언 {len(utts)}회"
                f"(위원 질의 {n_q}회, 이행약속 답변 {n_c}회), 질의 위원 {len(members)}명.")
    if not utts:
        overview = (f"{m['date']} {m['title']} — 회의록 본문이 아직 수집되지 않았습니다. "
                    f"안건 {len(m['agendas'])}건.")
    return {
        "meeting": m,
        "method": "rule-based",
        "overview": overview,
        "agendas": m["agendas"],
        "key_issues": _issue_rank(substantive),
        "keywords": [w for w, _ in keywords([full_text], 15, exclude=_names(utts))]
                    if full_text else [],
        "key_points": extractive_summary(full_text, 5) if full_text else [],
        "key_point_items": key_point_items(substantive, 6) if full_text else [],
        # 발언 구성(발언 수 기준): 위원 질의 / 정부·기관 답변 / 위원장 진행 / 기타(전문위원 등)
        "composition": {k: sum(1 for u in utts if (u["speaker_type"] if u["speaker_type"] in
                                                    ("member", "chair") else
                                                    "gov" if u["speaker_type"] in ("official", "witness")
                                                    else "other") == k)
                        for k in ("member", "gov", "chair", "other")},
        "agenda_summaries": agenda_summaries,
        "qa_highlights": [_pair_view(p) for p in pairs[:8]],
        "commitments": [commitment_view(u) for u in utts if u["is_commitment"]],
        "data_requests": [{"member": u["speaker_name"], "text": truncate(u["text"], 200)}
                          for u in utts if u["is_data_request"]],
        "participants": {
            "members": [{"name": n, "count": c} for n, c in members.most_common()],
            "officials": [{"name": n, "count": c} for n, c in officials.most_common()],
        },
    }


SUMMARY_POOL = 600  # 브리핑 요약 후보 발언 수 상한(응답 시간)


def briefing_cache_key(org: str = "", issue: str = "", keyword: str = "", member: str = "",
                       date_from: str = "", date_to: str = "", session: int | None = None) -> str:
    """브리핑 결과 저장 키(kv). 조건 없는 전체 브리핑은 사전 계산해 seed에 싣는다."""
    return "briefing:" + json.dumps([org, issue, keyword, member, date_from, date_to, session], ensure_ascii=False)


def briefing(store: Store, org: str = "", issue: str = "", keyword: str = "",
             member: str = "", date_from: str = "", date_to: str = "",
             top: int = 10, session: int | None = None) -> dict[str, Any]:
    """국정감사 대비 브리핑: 대상(기관/쟁점/키워드/위원) 관련 과거 회의록을 모아 정리."""
    issue_ids = set()
    if issue:
        issue_ids = {iid for iid, (label, parent, _) in ISSUES.items()
                     if iid == issue or label == issue or parent == issue
                     or (parent and ISSUES[parent][0] == issue)}
        if not issue_ids:
            raise KeyError(f"쟁점 '{issue}'을(를) 찾을 수 없습니다.")
    utts = store.utterances(session=session)

    def keep(u: dict) -> bool:
        if date_from and (u["meeting_date"] or "") < date_from:
            return False
        if date_to and (u["meeting_date"] or "") > date_to:
            return False
        if org and not (u["org"] == org or org in u["orgs"]):
            return False
        if issue_ids and not (issue_ids & set(u["issues"])):
            return False
        if keyword and keyword not in u["text"]:
            return False
        if member and u["speaker_name"] != member:
            return False
        return True

    # 질의가 조건에 맞으면 그 답변도 포함(답변에 기관명이 없어도 맥락 유지)
    by_meeting: dict[str, list[dict]] = defaultdict(list)
    for u in utts:
        by_meeting[u["meeting_id"]].append(u)
    selected_pairs, selected = [], []
    for mid, mu in by_meeting.items():
        for p in qa_pairs(mu):
            if keep(p["question"]) or (not member and any(keep(a) for a in p["answers"])):
                selected_pairs.append(p)
        selected.extend(u for u in mu if u["speaker_type"] != "chair" and keep(u))

    commitments = [u for u in utts if u["is_commitment"] and keep(u)]
    seen = {u["id"] for u in commitments}
    for p in selected_pairs:
        for a in p["answers"]:
            if a["is_commitment"] and a["id"] not in seen:
                seen.add(a["id"])
                commitments.append(a)
    if org:  # 기관 브리핑의 약속 추적표는 그 기관이 한 약속만(다른 기관 답변이 섞이지 않게)
        commitments = [u for u in commitments if u["org"] == org]

    timeline = Counter(u["meeting_date"] for u in selected)
    meetings_idx = {m["id"]: m for m in store.meetings(session=session)}
    meeting_list = sorted({u["meeting_id"] for u in selected},
                          key=lambda mid: meetings_idx[mid]["date"] or "", reverse=True)
    per_meeting = Counter(u["meeting_id"] for u in selected)
    members = Counter(p["question"]["speaker_name"] for p in selected_pairs)
    pairs_sorted = sorted(selected_pairs, key=_pair_score, reverse=True)
    all_text = " ".join(u["text"] for u in selected)
    # 국감 준비에는 위원 질의·정부 답변이 핵심: 전문위원 보고보다 이들 발언에서 뽑는다.
    # 조건이 넓으면(전체·대형 기관) 발언이 수만 건이라, 쟁점을 많이 다룬 발언 위주로 후보를 줄여 요약한다.
    speakers = [u for u in selected if u["speaker_type"] in ("member", "official", "witness")] or selected
    pool = sorted(speakers, key=lambda u: (len(u["issues"]), min(len(u["text"]), 1500)), reverse=True)[:SUMMARY_POOL]
    pool.sort(key=lambda u: (u["meeting_date"] or "", u["meeting_id"], u["idx"]))
    points = key_point_items(pool, 6) if all_text else []

    target = " / ".join(x for x in (org, issue and next(
        (ISSUES[i][0] for i in ISSUES if i == issue), issue), keyword, member) if x) or "전체"
    return {
        "target": target,
        "filters": {"org": org, "issue": issue, "keyword": keyword, "member": member,
                    "date_from": date_from, "date_to": date_to, "session": session},
        "counts": {"utterances": len(selected), "meetings": len(meeting_list),
                   "qa_pairs": len(selected_pairs), "commitments": len(commitments)},
        "key_issues": _issue_rank(selected),
        "keywords": [w for w, _ in keywords([all_text], 20, exclude=_names(utts))]
                    if all_text else [],
        "key_points": [p["text"] for p in points],
        "key_point_items": points,
        "top_questions": [_pair_view(p) for p in pairs_sorted[:top]],
        # 추적표에는 무엇을 하겠다는지 드러나는 약속만('지적하신 취지에 공감합니다' 류 제외)
        "commitments": [v for v in (commitment_view(u) for u in
                        sorted(commitments, key=lambda u: u["meeting_date"] or "", reverse=True))
                        if _specific_promise(v["text"])],
        "active_members": [{"name": n, "count": c} for n, c in members.most_common(10)],
        "timeline": [{"date": d, "count": c} for d, c in sorted(timeline.items())],
        "by_session": [{"session": k, "count": v} for k, v in
                       sorted(Counter(u["session_no"] for u in selected if u.get("session_no")).items())],
        "meetings": [{"id": mid, "date": meetings_idx[mid]["date"], "session_no": meetings_idx[mid]["session_no"],
                      "title": meetings_idx[mid]["title"], "count": per_meeting[mid]} for mid in meeting_list],
    }


def briefing_markdown(b: dict) -> str:
    L = [f"# 국정감사 대비 브리핑: {b['target']}", ""]
    c = b["counts"]
    L.append(f"- 분석 범위: 회의 {c['meetings']}건, 발언 {c['utterances']}회, "
             f"질의·답변 {c['qa_pairs']}쌍, 이행약속 답변 {c['commitments']}건")
    if b["filters"].get("session"):
        L.append(f"- 회기: 제{b['filters']['session']}회")
    if b["filters"]["date_from"] or b["filters"]["date_to"]:
        L.append(f"- 기간: {b['filters']['date_from'] or '처음'} ~ {b['filters']['date_to'] or '현재'}")
    L += ["", "## 1. 핵심 쟁점"]
    for i in b["key_issues"]:
        L.append(f"- **{i['label']}** ({i['count']}회)" + (f" · 상위: {i['parent']}" if i["parent"] else ""))
    if b["keywords"]:
        L += ["", "주요 키워드: " + ", ".join(b["keywords"])]
    L += ["", "## 2. 주요 내용"]
    L += [f"- {s}" for s in b["key_points"]] or ["- (해당 발언 없음)"]
    L += ["", "## 3. 주요 질의와 정부 답변"]
    for n, q in enumerate(b["top_questions"], 1):
        L.append(f"{n}. **{q['member']} 위원** ({q['date']}) — {q['question']}")
        if q["answer"]:
            L.append(f"   - 답변({q['answerer']}): {q['answer']}" + (" **[이행약속]**" if q["commitment"] else ""))
    L += ["", "## 4. 이행약속 추적표 (사후 점검 대상)", "",
          "| 일자 | 답변자 | 기관 | 내용 |", "|---|---|---|---|"]
    for cm in b["commitments"]:
        L.append(f"| {cm['date']} | {cm['speaker']} | {cm['org'] or '-'} | {cm['text'].replace('|', '/')} |")
    L += ["", "## 5. 관심 위원"]
    L += [f"- {m['name']} 위원: 질의 {m['count']}회" for m in b["active_members"]]
    L += ["", "## 6. 관련 회의"]
    L += [f"- {m['date']} {m['title']}" for m in b["meetings"]]
    if b.get("programs"):  # 기관 브리핑: 그 기관의 지원사업 공고(공공데이터포털 K-Startup)
        p = b["programs"]
        L += ["", f"## 7. 지원사업 공고 ({p['total']}건, 접수 중 {p['open']}건)", "",
              "| 접수 | 상태 | 공고 |", "|---|---|---|"]
        status = {"open": "접수 중", "upcoming": "접수 예정", "closed": "마감"}
        L += [f"| {a['start']} ~ {a['end'] or '미정'} | {status.get(a['status'], '')} | "
              f"[{a['title'].replace('|', '/')}]({a['url']}) |" for a in p["items"]]
    return "\n".join(L) + "\n"


def issue_overview(store: Store) -> list[dict]:
    utts = store.utterances()
    per_issue: dict[str, dict] = {}
    for iid, (label, parent, _) in ISSUES.items():
        per_issue[iid] = {"id": iid, "label": label, "parent": parent, "count": 0,
                          "meetings": set(), "orgs": Counter(), "members": Counter()}
    for u in utts:
        for iid, n in u["issues"].items():
            d = per_issue[iid]
            d["count"] += n
            d["meetings"].add(u["meeting_id"])
            d["orgs"].update(u["orgs"])
            if u["org"]:
                d["orgs"][u["org"]] += 1
            if u["speaker_type"] == "member":
                d["members"][u["speaker_name"]] += 1
            parent = ISSUES[iid][1]
            if parent:
                per_issue[parent]["count"] += n
                per_issue[parent]["meetings"].add(u["meeting_id"])
    out = []
    for d in per_issue.values():
        out.append({**d, "meetings": len(d["meetings"]),
                    "orgs": [o for o, _ in d["orgs"].most_common(5)],
                    "members": [m for m, _ in d["members"].most_common(5)]})
    return out


def org_overview(store: Store) -> list[dict]:
    cnt, commits = Counter(), Counter()
    for u in store.utterances():
        for o in set(u["orgs"]) | ({u["org"]} if u["org"] else set()):
            cnt[o] += 1
        if u["is_commitment"] and u["org"]:
            commits[u["org"]] += 1
    return [{"name": n, "aliases": list(ORGANIZATIONS[n]), "mentions": cnt[n],
             "commitments": commits[n]} for n in ORGANIZATIONS]


def issue_network(store: Store, min_weight: int = 1) -> dict:
    """쟁점–기관–위원 관계망(시각화용)."""
    nodes: dict[str, dict] = {}
    edges: Counter = Counter()

    def node(nid: str, label: str, kind: str):
        n = nodes.setdefault(nid, {"id": nid, "label": label, "kind": kind, "weight": 0})
        n["weight"] += 1

    for u in store.utterances():
        orgs_ = set(u["orgs"]) | ({u["org"]} if u["org"] else set())
        for i in u["issues"]:
            node(f"issue:{i}", ISSUES[i][0], "issue")
            for o in orgs_:
                node(f"org:{o}", o, "org")
                edges[(f"issue:{i}", f"org:{o}")] += 1
            if u["speaker_type"] == "member":
                node(f"member:{u['speaker_name']}", f"{u['speaker_name']} 위원", "member")
                edges[(f"member:{u['speaker_name']}", f"issue:{i}")] += 1
    return {"nodes": list(nodes.values()),
            "edges": [{"source": a, "target": b, "weight": w}
                      for (a, b), w in edges.items() if w >= min_weight]}


def issue_by_session(store: Store) -> dict:
    """회기 × 쟁점 언급량(히트맵용). 가장 아래 단계 쟁점만 집계(하위가 없는 최상위 쟁점 포함)."""
    parents = {p for _, p, _ in ISSUES.values() if p}
    rows = store.conn.execute(
        "SELECT m.session_no, u.issues_json FROM utterances u JOIN meetings m ON m.id=u.meeting_id "
        "WHERE m.session_no IS NOT NULL AND u.issues_json != '{}'").fetchall()
    matrix: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        for iid, n in json.loads(r["issues_json"]).items():
            if iid not in parents:
                matrix[iid][r["session_no"]] += n
    sessions = sorted({sn for c in matrix.values() for sn in c})
    order = sorted(matrix, key=lambda i: -sum(matrix[i].values()))
    return {
        "sessions": sessions,
        "issues": [{"id": i, "label": ISSUES[i][0], "parent": ISSUES[i][1],
                    "total": sum(matrix[i].values()),
                    "by_session": {str(k): v for k, v in matrix[i].items()}} for i in order],
    }


def issue_detail(store: Store, issue_id: str, session: int | None = None, limit: int = 20) -> dict:
    """쟁점 상세: 회기별 추이, 관련 기관·위원, 대표 질의(요약)와 정부 약속."""
    if issue_id not in ISSUES:
        raise KeyError(issue_id)
    label, parent, _ = ISSUES[issue_id]
    ids = {issue_id} | {i for i, v in ISSUES.items() if v[1] == issue_id}
    focus = {k for i in ids for k in ISSUES[i][2]}
    seen, uniq = set(), []
    for i in ids:
        for u in store.utterances(issue=i, session=session):
            if u["id"] not in seen:
                seen.add(u["id"]); uniq.append(u)
    by_session = Counter(u["session_no"] for u in uniq if u["session_no"])
    orgs, members = Counter(), Counter()
    for u in uniq:
        orgs.update(set(u["orgs"]) | ({u["org"]} if u["org"] else set()))
        if u["speaker_type"] == "member":
            members[u["speaker_name"]] += 1
    weight = lambda u: sum(u["issues"].get(i, 0) for i in ids)
    questions = sorted((u for u in uniq if u["is_question"]),
                       key=lambda u: (-weight(u), -(len(u["text"]))))
    commitments = sorted((u for u in uniq if u["is_commitment"]),
                         key=lambda u: (u["meeting_date"] or "", weight(u)), reverse=True)

    def view(u: dict) -> dict:
        return {"meeting_id": u["meeting_id"], "date": u["meeting_date"], "session": u["session_no"],
                "speaker": u["speaker_name"] + " 위원" if u["speaker_type"] == "member"
                else f"{u['speaker_role']} {u['speaker_name']}",
                "speaker_type": u["speaker_type"],
                "text": " ".join(extractive_summary(u["text"], 2, focus=focus)) or truncate(u["text"], 220)}

    return {
        "id": issue_id, "label": label, "parent": ISSUES[parent][0] if parent else None,
        "dialogue": issue_dialogue(store, questions[:12], commitments[:40], focus),
        "children": [{"id": i, "label": v[0]} for i, v in ISSUES.items() if v[1] == issue_id],
        "keywords": sorted(focus), "count": len(uniq),
        "meetings": len({u["meeting_id"] for u in uniq}),
        "by_session": [{"session": k, "count": v} for k, v in sorted(by_session.items())],
        "orgs": [{"name": n, "count": c} for n, c in orgs.most_common(8)],
        "members": [{"name": n, "count": c} for n, c in members.most_common(8)],
        "questions": [view(u) for u in questions[:limit]],
        "commitments": [view(u) for u in commitments[:limit]],
    }


def _neighbors(store: Store, u: dict, before: int = 8, after: int = 8) -> list[dict]:
    rows = store.conn.execute(
        "SELECT u.*, m.date AS meeting_date, m.session_no AS session_no FROM utterances u "
        "JOIN meetings m ON m.id=u.meeting_id WHERE u.meeting_id=? AND u.idx BETWEEN ? AND ? "
        "ORDER BY u.idx", (u["meeting_id"], u["idx"] - before, u["idx"] + after)).fetchall()
    return [store._utt(r) for r in rows]


_ANSWERERS = ("official", "witness")


def _exchange(store: Store, anchor: dict) -> list[dict]:
    """anchor가 속한 '한 위원의 질의 순서'를 찾는다: 위원장 발언이나 다른 위원 발언이 나오기 전까지
    같은 위원과 정부 측이 주고받은 연속 발언. 질의–답변이 실제로 이어진 구간만 대화로 묶기 위함."""
    near = _neighbors(store, anchor, before=12, after=12)
    pos = next(i for i, u in enumerate(near) if u["id"] == anchor["id"])
    member = anchor["speaker_name"] if anchor["speaker_type"] == "member" else None
    lo = pos
    while lo > 0:
        u = near[lo - 1]
        if u["speaker_type"] in _ANSWERERS:
            lo -= 1
        elif u["speaker_type"] == "member" and member in (None, u["speaker_name"]):
            member = u["speaker_name"]; lo -= 1
        else:
            break
    hi = pos
    while hi + 1 < len(near):
        u = near[hi + 1]
        if u["speaker_type"] in _ANSWERERS or (u["speaker_type"] == "member" and u["speaker_name"] == member):
            hi += 1
        else:
            break
    block = near[lo:hi + 1]
    # 첫 위원 발언부터 시작(앞쪽의 정부 측 발언은 다른 질의에 대한 답)
    first = next((i for i, u in enumerate(block) if u["speaker_type"] == "member"), None)
    if member is None or first is None:
        return []
    block = block[first:]
    # anchor 바로 앞의 위원 질의부터 최대 4개 발언(질의 → 답변 → 추가 질의 → 답변)
    at = next(i for i, u in enumerate(block) if u["id"] == anchor["id"])
    start = max(i for i in range(at + 1) if block[i]["speaker_type"] == "member")
    turns = block[start:start + 4]
    # 끝에 붙은 맺음말('수고하셨습니다', '알겠습니다')은 뺀다
    while len(turns) > 2 and turns[-1]["speaker_type"] == "member" and not (
            turns[-1]["is_question"] or _substantive(turns[-1], 30)):
        turns.pop()
    return turns


def _question_text(text: str) -> str:
    """위원 발언에서 답변을 부른 부분: 끝쪽 물음 문장(없으면 마지막 두 문장)."""
    sents = [x.strip() for x in SENT_SPLIT.split(text) if x.strip()]
    qs = [i for i, x in enumerate(sents) if x.endswith("?") or QUESTION_PATTERNS.search(x)]
    if qs:
        i = qs[-1]
        picked = sents[max(0, i - 1):i + 1] if len(sents[i]) < 60 else [sents[i]]
    else:
        picked = sents[-2:]
    return truncate(" ".join(picked), 240)


def _answer_text(u: dict) -> str:
    """정부 측 발언의 첫머리(바로 앞 질의에 대한 직접 답). 약속 문장이 뒤에 있으면 덧붙인다."""
    sents = [x.strip() for x in SENT_SPLIT.split(u["text"]) if x.strip()]
    head = sents[:2]
    text = " ".join(head)
    if u["is_commitment"]:
        flat = re.sub(r"\s+", "", text)
        promise = [c for c in commitment_sentences(u["text"]) if re.sub(r"\s+", "", c) not in flat][:1]
        if promise:
            text += " … " + promise[0]
    return truncate(text, 300)


def issue_dialogue(store: Store, questions: list[dict], commitments: list[dict],
                   focus: set[str], limit: int = 10) -> list[dict]:
    """쟁점 대화: 정부 약속·대표 질의가 나온 '한 위원의 질의 순서'를 연속 발언 그대로 보여 준다.
    화면에서 위원 질의는 왼쪽, 정부 답변·약속은 오른쪽 말풍선."""
    seen, found = set(), []
    for anchor in [*commitments, *questions]:
        turns = _exchange(store, anchor)
        # 정부 측의 실질 답변(또는 약속)이 없는 대화는 보여 줄 가치가 없다
        if not any(t["speaker_type"] in _ANSWERERS and (t["is_commitment"] or _substantive(t))
                   for t in turns):
            continue
        ids = {t["id"] for t in turns}
        if ids & seen:  # 같은 질의 순서에서 겹치는 대화는 한 번만
            continue
        seen |= ids
        text = " ".join(t["text"] for t in turns)
        found.append({
            # 약속 포함 > 쟁점 표현 > '예.' 같은 짧은 대답이 아닌 실질 답변 수
            "rank": (any(t["is_commitment"] for t in turns), min(sum(text.count(k) for k in focus), 5),
                     sum(1 for t in turns if t["speaker_type"] in _ANSWERERS and len(t["text"]) > 40)),
            "meeting_id": anchor["meeting_id"], "date": anchor["meeting_date"],
            "session": anchor["session_no"], "idx": turns[0]["idx"],
            "turns": [{"side": "q", "speaker": t["speaker_name"] + " 위원", "text": _question_text(t["text"])}
                      if t["speaker_type"] == "member" else
                      {"side": "a", "speaker": f"{t['speaker_role']} {t['speaker_name']}",
                       "commitment": t["is_commitment"], "text": _answer_text(t)}
                      for t in turns],
        })
    # 약속이 있고 쟁점 표현이 많이 나온 대화를 고른 뒤, 최근 회의 순·회의 안에서는 발언 순으로
    found.sort(key=lambda d: d["rank"], reverse=True)
    chosen = [{k: v for k, v in d.items() if k != "rank"} for d in found[:limit]]
    chosen.sort(key=lambda d: (d["date"] or "", -d["idx"]), reverse=True)
    return chosen


def main_agendas(agendas: list[str]) -> list[str]:
    """번호가 붙은 본 안건만('가. 농림축산식품부 소관' 같은 하위 항목 제외)."""
    return [a for a in agendas if re.match(r"^\s*\d+\s*\.", a)] or agendas


def agenda_headline(agendas: list[str]) -> str:
    """회의의 대표 안건 한 줄: '2025회계연도 결산 외 7건'(번호·의안번호·발의자 표기는 뺀다)."""
    main = main_agendas(agendas)
    if not main:
        return ""
    first = re.sub(r"^\s*\d+\s*\.\s*", "", main[0])
    first = re.sub(r"\((?:[^()]*의원[^()]*|의안번호[^()]*|[^()]*대표발의[^()]*)\)", "", first).strip()
    return first + (f" 외 {len(main) - 1}건" if len(main) > 1 else "")


def overview(store: Store, recent: int = 6, ledger: int = 10) -> dict:
    """대시보드용: 최근 회의 원장(대표 안건·핵심 쟁점·발언·질의·약속 수),
    최근 정부 약속(무엇을 하겠다는지 드러나는 것만, 질의 맥락 포함)."""
    out_m = []
    for m in store.meetings()[:ledger]:
        s = store.summary(m["id"], "rule") or {}
        n = store.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(is_question),0), COALESCE(SUM(is_commitment),0) "
            "FROM utterances WHERE meeting_id=?", (m["id"],)).fetchone()
        out_m.append({"id": m["id"], "date": m["date"], "title": m["title"],
                      "session_no": m["session_no"], "agendas": m["agendas"][:3],
                      "n_agendas": len(main_agendas(m["agendas"])),
                      "n_utterances": n[0], "n_questions": n[1], "n_commitments": n[2],
                      "issues": [i["label"] for i in (s.get("key_issues") or [])[:3]],
                      "headline": agenda_headline(m["agendas"])})
    rows = store.conn.execute(
        "SELECT u.*, m.date AS meeting_date, m.title AS meeting_title, m.session_no AS session_no "
        "FROM utterances u JOIN meetings m ON m.id=u.meeting_id "
        "WHERE u.is_commitment=1 ORDER BY m.date DESC, u.idx DESC LIMIT 200").fetchall()
    out_c = []
    for r in rows:
        u = store._utt(r)
        view = commitment_view(u)
        if not _specific_promise(view["text"]):
            continue
        q = next((x for x in reversed(_neighbors(store, u, before=6, after=0)[:-1])
                  if x["speaker_type"] == "member"), None)
        view["question"] = {"member": q["speaker_name"], "text": _question_text(q["text"])} if q else None
        out_c.append(view)
        if len(out_c) >= recent * 2:
            break
    return {"meetings": out_m, "commitments": out_c}




def meeting_markdown(store: Store, meeting_id: str, base_url: str = "") -> str | None:
    """외부 AI(ChatGPT·Claude 등)에 붙여넣어 분석할 수 있는 회의록 전문(Markdown)."""
    m = store.meeting(meeting_id)
    if not m:
        return None
    utts = store.utterances(meeting_id=meeting_id)
    s = store.summary(meeting_id, "rule") or summarize_meeting(store, meeting_id)
    agendas = [re.sub(r"^\s*\d+\.\s*", "", a) for a in (m.get("agendas") or [])]
    lines = [f"# {m.get('title') or '국회 농림축산식품해양수산위원회 회의록'}", "",
             f"- 회의일: {m.get('date') or '-'}",
             f"- 회기: 제{m.get('session_no') or '?'}회 국회",
             f"- 발언 수: {len(utts)}"]
    if m.get("pdf_url"):
        lines.append(f"- 원문(PDF): {m['pdf_url']}")
    if base_url:
        lines.append(f"- 출처: 국회 농해수위 온톨로지 {base_url}/#meetings/{meeting_id}")
    if agendas:
        lines += ["", "## 안건", *[f"{i + 1}. {a}" for i, a in enumerate(agendas)]]
    issues = [i["label"] for i in (s.get("key_issues") or [])[:8]]
    if issues:
        lines += ["", "## 자동 추출 핵심 쟁점(참고)", ", ".join(issues)]
    lines += ["", "## 회의록 전문",
              "(표기: **발언자**: 발언 / [질의]·[약속]·[자료요구]는 자동 분류 표시)"]
    last = None
    for u in utts:
        ai = u.get("agenda_idx")
        if ai is not None and ai != last and 0 <= ai < len(agendas):
            last = ai
            lines += ["", f"### {agendas[ai]}"]
        role, name = u.get("speaker_role") or "", u.get("speaker_name") or ""
        who = f"{name} 위원" if role == "위원" else f"{role} {name}".strip()
        tags = "".join(t for k, t in (("is_question", "[질의]"), ("is_commitment", "[약속]"),
                                       ("is_data_request", "[자료요구]")) if u.get(k))
        lines += ["", f"**{who}**{(' ' + tags) if tags else ''}: {u.get('text', '').strip()}"]
    return "\n".join(lines) + "\n"
