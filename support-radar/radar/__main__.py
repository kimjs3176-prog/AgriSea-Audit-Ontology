"""CLI: python -m radar <명령>

  collect   K-Startup 공고·사업 소개 수집 → data/catalog.json.gz
  signals   국회 회의록 DB(agrisea)에서 정책 신호 계산 → data/policy_signals.json
  rules     (선택) Claude로 접수 중 공고의 자격 조건 구조화
  match     프로필로 맞춤 공고·자격 판정
  radar     주제별 국회 관심도 × 공고 공급
  serve     웹 서비스
  stats     현황
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import config
from .catalog import Catalog, load_signals
from .config import get_settings


def _seed_catalog() -> Catalog:
    cat = Catalog.read(config.SEED_PATH) if config.SEED_PATH.exists() else Catalog()
    cat.signals = load_signals()
    return cat


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="radar", description="지원사업 레이더")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("collect", help="K-Startup 공고 수집(저장소 스냅숏 갱신)")
    c.add_argument("--since", default=config.COLLECT_SINCE, help="접수 시작일 기준(YYYY-MM-DD)")
    c.add_argument("--open-only", action="store_true", help="접수 중 공고만")
    c.add_argument("--no-programs", action="store_true", help="사업 소개(예산) 수집 생략")
    c.add_argument("--max-pages", type=int, default=400)

    s = sub.add_parser("signals", help="국회 정책 신호 계산")
    s.add_argument("--agrisea-db", required=True, help="국회 회의록 SQLite(agrisea) 경로")
    s.add_argument("-o", "--output", default=str(config.SIGNALS_PATH))

    r = sub.add_parser("rules", help="(선택) Claude로 자격 조건 구조화")
    r.add_argument("--limit", type=int, default=150)

    m = sub.add_parser("match", help="맞춤 매칭")
    m.add_argument("--kind", choices=["pre", "company"], default="company")
    m.add_argument("--founded", default="")
    m.add_argument("--age", type=int)
    m.add_argument("--region", default="")
    m.add_argument("--keywords", default="")
    m.add_argument("--categories", default="")
    m.add_argument("-n", type=int, default=15)

    sub.add_parser("radar", help="정책 레이더 표")
    sv = sub.add_parser("serve", help="웹 서비스")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8100)
    sub.add_parser("stats", help="현황")

    args = ap.parse_args(argv)
    settings = get_settings()

    if args.cmd == "collect":
        cat = Catalog.read(config.SEED_PATH) if config.SEED_PATH.exists() else Catalog()
        res = cat.refresh(settings, since=args.since, open_only=args.open_only,
                          with_programs=not args.no_programs, max_pages=args.max_pages)
        cat.write(config.SEED_PATH)
        print(json.dumps(res, ensure_ascii=False))
        print(json.dumps(cat.stats(), ensure_ascii=False, indent=1))
    elif args.cmd == "signals":
        from .policy import build_signals
        sig = build_signals(Path(args.agrisea_db))
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(sig, ensure_ascii=False, indent=1)
        # 내용이 같으면 생성 시각만 바뀐 파일을 커밋하지 않도록 기존 시각 유지
        if out.exists():
            old = json.loads(out.read_text(encoding="utf-8"))
            if {**old, "generated_at": ""} == {**sig, "generated_at": ""}:
                payload = json.dumps(old, ensure_ascii=False, indent=1)
        out.write_text(payload + "\n", encoding="utf-8")
        print(json.dumps(sig["source"], ensure_ascii=False))
        for tid, t in sig["topics"].items():
            print(f"  {t['label']:<14} 언급 {t['mentions']:>4}  질의 {t['questions']:>4}  약속 {len(t['commitments']):>3}")
    elif args.cmd == "rules":
        if not settings.llm_enabled:
            print("ANTHROPIC_API_KEY가 없어 건너뜁니다.")
            return 0
        from .kstartup import status
        from .llm import extract_rules
        cat = Catalog.read(config.SEED_PATH)
        targets = [a for a in cat.announcements if status(a) != "closed"]
        n = extract_rules(targets, settings, limit=args.limit)
        cat.write(config.SEED_PATH)
        print(f"자격 조건 구조화 {n}건")
    elif args.cmd == "match":
        from .eligibility import Profile, match
        cat = _seed_catalog()
        p = Profile.from_dict({k: v for k, v in vars(args).items() if k not in ("cmd", "n")})
        out = match(cat.announcements, p)
        print(json.dumps(out["summary"], ensure_ascii=False))
        for r in out["results"][:args.n]:
            print(f"[{r['verdict']:<11}] {r['score']:>3}  {r['end'] or '-':<10}  {r['title']}")
            for chk in r["checks"]:
                print(f"      {chk['result']:<11} {chk['item']}: {chk['detail']}")
    elif args.cmd == "radar":
        from .policy import QUADRANTS, radar
        cat = _seed_catalog()
        for row in radar(cat.signals, cat.announcements)["topics"]:
            print(f"{QUADRANTS[row['quadrant']][0]:<6} {row['label']:<14} 국회 {row['demand']:>4} "
                  f"(증감 {row['momentum']:+.0%})  공고 {row['supply']:>4}  접수중 {row['open']:>3}  약속 {row['commitments']}")
    elif args.cmd == "serve":
        import uvicorn
        from .web.app import create_app
        uvicorn.run(create_app(settings), host=args.host, port=args.port)
    elif args.cmd == "stats":
        print(json.dumps(Catalog.load(settings).stats(), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
