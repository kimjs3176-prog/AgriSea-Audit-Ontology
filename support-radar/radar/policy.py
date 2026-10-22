"""정책 신호: 국회 농해수위 회의록(agrisea DB)에서 주제별 관심도·질의·정부 약속을 뽑고,
지원사업 공고 공급과 맞대어 '정책 → 공고' 흐름을 보여 준다.

- build_signals(): agrisea SQLite → 주제별 신호(JSON, 자동 수집이 data/policy_signals.json 으로 커밋)
- radar(): 신호 × 공고 → 주제별 관심도·공급·공백 판정, 약속별 후속 공고 추적
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any

from .topics import TOPICS, announcement_topics, squash, topic_hits, topic_keywords

_SENT = re.compile(r"(?<=[.?!])\s+|(?<=니다)\s+|(?<=니까)\s+")


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT.split(text or "") if len(s.strip()) >= 8]


def _excerpt(text: str, tid: str, limit: int = 220) -> str:
    """주제 키워드가 들어간 문장(들)만 잘라 낸다."""
    kws = topic_keywords(tid)
    picked = [s for s in _sentences(text) if any(k in squash(s) for k in kws)]
    out = " ".join(picked[:2]) or text
    return out if len(out) <= limit else out[:limit].rstrip() + "…"


def _commitment_sentences(text: str) -> list[str]:
    try:  # 회의록 서비스와 같은 판별 규칙(저장소 루트의 agrisea 패키지)
        from agrisea.lexicon import commitment_sentences
        return commitment_sentences(text)
    except ImportError:
        return [s for s in _sentences(text) if re.search(r"(검토|개선|추진|마련|확대|지원|강화|도입|구축)"
                                                         r"\S{0,6}\s*(하|드리)겠습니다", s)]


def _month(d: str) -> str:
    return d[:7]


def build_signals(agrisea_db: Path | str) -> dict[str, Any]:
    conn = sqlite3.connect(f"file:{agrisea_db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT u.meeting_id, u.speaker_name, u.speaker_role, u.speaker_type, u.org, u.text, "
        "u.is_question, u.is_commitment, u.orgs_json, m.date, m.title "
        "FROM utterances u JOIN meetings m ON m.id = u.meeting_id "
        "WHERE COALESCE(m.is_sample, 0) = 0 ORDER BY m.date, u.meeting_id, u.idx").fetchall()
    meetings = conn.execute(
        "SELECT COUNT(*), MIN(date), MAX(date) FROM meetings WHERE COALESCE(is_sample,0)=0 "
        "AND text_status='parsed'").fetchone()
    conn.close()

    monthly_total: Counter = Counter()
    acc: dict[str, dict[str, Any]] = {
        t: {"mentions": 0, "questions": 0, "meetings": set(), "monthly": Counter(),
            "members": Counter(), "orgs": Counter(), "commitments": [], "questions_sample": []}
        for t in TOPICS
    }
    for r in rows:
        monthly_total[_month(r["date"])] += 1
        hits = topic_hits(r["text"])
        for tid in hits:
            a = acc[tid]
            a["mentions"] += 1
            a["meetings"].add(r["meeting_id"])
            a["monthly"][_month(r["date"])] += 1
            for org in json.loads(r["orgs_json"] or "{}"):
                a["orgs"][org] += 1
            if r["speaker_type"] == "member":
                a["members"][r["speaker_name"]] += 1
            if r["is_question"]:
                a["questions"] += 1
                a["questions_sample"].append({
                    "date": r["date"], "meeting_id": r["meeting_id"], "meeting_title": r["title"],
                    "speaker": r["speaker_name"], "text": _excerpt(r["text"], tid)})
            if r["is_commitment"]:
                kws = topic_keywords(tid)
                for s in _commitment_sentences(r["text"]):
                    if any(k in squash(s) for k in kws):
                        a["commitments"].append({
                            "date": r["date"], "meeting_id": r["meeting_id"],
                            "meeting_title": r["title"], "speaker": r["speaker_name"],
                            "role": r["speaker_role"], "org": r["org"] or "",
                            "text": s if len(s) <= 300 else s[:300] + "…"})

    topics = {}
    for tid, a in acc.items():
        topics[tid] = {
            "label": TOPICS[tid][0], "desc": TOPICS[tid][1],
            "mentions": a["mentions"], "questions": a["questions"], "meetings": len(a["meetings"]),
            "monthly": dict(sorted(a["monthly"].items())),
            "members": [{"name": n, "count": c} for n, c in a["members"].most_common(8)],
            "orgs": [{"name": n, "count": c} for n, c in a["orgs"].most_common(6)],
            "commitments": sorted(a["commitments"], key=lambda c: c["date"], reverse=True)[:40],
            "questions_sample": sorted(a["questions_sample"], key=lambda q: q["date"], reverse=True)[:8],
        }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": {"name": "국회 농림축산식품해양수산위원회 회의록(열린국회정보)",
                   "meetings": meetings[0], "utterances": len(rows),
                   "date_min": meetings[1], "date_max": meetings[2]},
        "monthly_total": dict(sorted(monthly_total.items())),
        "topics": topics,
    }


# ── 신호 × 공고 ───────────────────────────────────────────
def _shift_month(ym: str, months: int) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    idx = y * 12 + (m - 1) + months
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def _window_sum(monthly: dict[str, int], end_month: str, months: int) -> int:
    start = _shift_month(end_month, -months + 1)
    return sum(v for k, v in monthly.items() if start <= k <= end_month)


def tag_announcements(announcements: list[dict]) -> None:
    for a in announcements:
        if "topics" not in a:
            a["topics"] = announcement_topics(a)


def radar(signals: dict[str, Any] | None, announcements: list[dict],
          today: date | None = None) -> dict[str, Any]:
    """주제별 관심도(국회)·공급(공고)·공백 판정."""
    from .kstartup import status

    today = today or date.today()
    tag_announcements(announcements)
    sig_topics = (signals or {}).get("topics", {})
    totals = (signals or {}).get("monthly_total", {})
    sig_end = _month((signals or {}).get("source", {}).get("date_max") or today.isoformat())
    ann_end = _month(today.isoformat())

    by_topic: dict[str, list[dict]] = defaultdict(list)
    for a in announcements:
        for t in a["topics"]:
            by_topic[t].append(a)

    rows = []
    for tid, (name, desc, _) in TOPICS.items():
        s = sig_topics.get(tid, {})
        monthly = s.get("monthly", {})
        anns = by_topic.get(tid, [])
        ann_monthly = Counter(_month(a["start"]) for a in anns if a.get("start"))
        # 증감: 최근 12개월과 그 전 12개월의 '전체 발언 대비 비중' 비교(국정감사 계절성·수집량 차이 보정)
        recent = _window_sum(monthly, sig_end, 12) / (_window_sum(totals, sig_end, 12) or 1)
        prev = _window_sum(monthly, _shift_month(sig_end, -12), 12) / (_window_sum(totals, _shift_month(sig_end, -12), 12) or 1)
        rows.append({
            "id": tid, "label": name, "desc": desc,
            "demand": _window_sum(monthly, sig_end, 12),
            "demand_total": s.get("mentions", 0),
            "questions": s.get("questions", 0),
            "commitments": len(s.get("commitments", [])),
            "momentum": round((recent - prev) / prev, 2) if prev else (1.0 if recent else 0.0),
            "supply": _window_sum(dict(ann_monthly), ann_end, 12),
            "supply_total": len(anns),
            "open": sum(1 for a in anns if status(a, today) == "open"),
            "monthly": monthly,
            "ann_monthly": dict(sorted(ann_monthly.items())),
        })

    def norm(key: str) -> None:
        top = max((r[key] for r in rows), default=0) or 1
        for r in rows:
            r[f"{key}_score"] = round(r[key] / top, 3)

    norm("demand")
    norm("supply")
    d_mid = median(r["demand_score"] for r in rows) if rows else 0
    s_mid = median(r["supply_score"] for r in rows) if rows else 0
    for r in rows:
        hi_d, hi_s = r["demand_score"] > d_mid, r["supply_score"] > s_mid
        r["quadrant"] = ("nodata" if not announcements  # 공고가 없으면 공백 판정을 하지 않는다
                         else "gap" if hi_d and not hi_s else "active" if hi_d and hi_s
                         else "supply" if hi_s else "latent")
        r["gap"] = round(r["demand_score"] - r["supply_score"], 3)
    rows.sort(key=lambda r: (-r["gap"], -r["demand"]))
    return {"topics": rows, "thresholds": {"demand": d_mid, "supply": s_mid},
            "signals_as_of": (signals or {}).get("source", {}).get("date_max"),
            "signals_generated_at": (signals or {}).get("generated_at")}


QUADRANTS = {
    "gap": ("공백·기회", "국회 관심은 높은데 공고가 적음 → 신규·확대 공고 가능성"),
    "active": ("활발", "국회 관심과 공고가 함께 많음 → 지금 신청할 공고가 많음"),
    "supply": ("공고 우위", "공고는 많지만 국회 논의는 적음 → 경쟁·중복 확인"),
    "latent": ("잠재", "관심·공고 모두 적음"),
    "nodata": ("공고 수집 전", "공고 데이터를 받은 뒤 판정"),
}


def topic_detail(signals: dict[str, Any] | None, announcements: list[dict], tid: str,
                 today: date | None = None) -> dict[str, Any]:
    """주제 상세: 신호, 약속별 후속 공고, 관련 공고."""
    from .kstartup import status

    today = today or date.today()
    tag_announcements(announcements)
    s = (signals or {}).get("topics", {}).get(tid, {})
    anns = sorted((a for a in announcements if tid in a["topics"]),
                  key=lambda a: a.get("start", ""), reverse=True)
    tracking = []
    for c in s.get("commitments", []):
        follow = sorted((a for a in anns if a.get("start") and a["start"] >= c["date"]),
                        key=lambda a: a["start"])
        tracking.append({**c, "followups": len(follow),
                         "first_followup": _brief(follow[0], today) if follow else None})
    row = next((r for r in radar(signals, announcements, today)["topics"] if r["id"] == tid), {})
    return {
        **row,
        "members": s.get("members", []), "orgs": s.get("orgs", []),
        "questions_sample": s.get("questions_sample", []),
        "tracking": tracking,
        "announcements": [_brief(a, today) for a in anns[:30]],
        "open_announcements": [_brief(a, today) for a in anns if status(a, today) == "open"][:30],
    }


def _brief(a: dict, today: date) -> dict[str, Any]:
    from .kstartup import status
    return {"id": a["id"], "title": a["title"], "org": a.get("org", ""), "category": a.get("category", ""),
            "start": a.get("start", ""), "end": a.get("end", ""), "status": status(a, today),
            "url": a.get("url", "")}


def background(signals: dict[str, Any] | None, a: dict) -> list[dict[str, Any]]:
    """공고의 정책 배경: 주제별 국회 관심도와 공고 이전의 정부 약속."""
    tag_announcements([a])
    out = []
    for tid in a["topics"]:
        s = (signals or {}).get("topics", {}).get(tid)
        if not s:
            continue
        before = [c for c in s.get("commitments", []) if not a.get("start") or c["date"] <= a["start"]]
        out.append({"topic": tid, "label": s["label"], "mentions": s["mentions"],
                    "questions": s["questions"], "commitments": before[:3],
                    "questions_sample": s.get("questions_sample", [])[:2]})
    return out
