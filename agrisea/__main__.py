"""명령행 인터페이스.

    python -m agrisea collect --dae 22 --param CONF_DATE=2024-10-07
    python -m agrisea fetch-minutes --limit 20
    python -m agrisea ingest 회의록.pdf --title "..." --date 2024-10-07
    python -m agrisea sample              # 가상 예시 데이터 적재(시연용)
    python -m agrisea build               # 지식그래프(TTL) 생성
    python -m agrisea search 쌀값 시장격리
    python -m agrisea summary <회의ID> [--llm]
    python -m agrisea briefing --org 농촌진흥청 -o 브리핑.md
    python -m agrisea sparql "SELECT ..."
    python -m agrisea serve --port 8000
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import TERM_START, get_settings
from .store import Store


def _store(settings) -> Store:
    settings.ensure_dirs()
    return Store(settings.db_path)


def _kv(pairs: list[str]) -> dict[str, str]:
    out = {}
    for p in pairs or []:
        k, _, v = p.partition("=")
        out[k.strip()] = v.strip()
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="agrisea", description="농해수위 회의록 국정감사 온톨로지 서비스")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("collect", help="Open API에서 위원회 회의록 목록 수집")
    c.add_argument("--dae", default="22", help="국회 대수(DAE_NUM)")
    c.add_argument("--date", help="회의일자(CONF_DATE), 예: 2024-10-07")
    c.add_argument("--from", dest="date_from", help="기간 수집 시작일(일자별로 API 호출)")
    c.add_argument("--to", dest="date_to", help="기간 수집 종료일(기본: 오늘)")
    c.add_argument("--days", type=int, help="오늘 기준 최근 N일 수집(--from 대신)")
    c.add_argument("--resume", action="store_true",
                   help="저장된 마지막 회의일 7일 전부터 오늘까지 수집(데이터가 없으면 22대 임기 시작일부터)")
    c.add_argument("--param", action="append", help="추가 요청인자 KEY=VALUE (반복 가능)")
    c.add_argument("--all-committees", action="store_true", help="농해수위 외 위원회도 저장")
    c.add_argument("--fetch", action="store_true", help="수집 후 회의록 본문까지 내려받기")

    f = sub.add_parser("fetch-minutes", help="회의록 PDF 내려받아 발언 단위로 파싱")
    f.add_argument("--id", action="append", help="특정 회의 ID만")
    f.add_argument("--limit", type=int)

    i = sub.add_parser("ingest", help="로컬 회의록(PDF/TXT) 적재")
    i.add_argument("path", type=Path)
    i.add_argument("--id")
    i.add_argument("--title", required=True)
    i.add_argument("--date", required=True)
    i.add_argument("--dae", default="22")
    i.add_argument("--kind", default="")
    i.add_argument("--agenda", action="append", default=[])

    pg = sub.add_parser("programs", help="지원사업 공고(공공데이터포털 K-Startup) 수집: 농림·해양수산 관련만 저장")
    pg.add_argument("--since", default=TERM_START, help="이 날짜 이후 접수를 시작한 공고(YYYY-MM-DD)")
    sub.add_parser("sample", help="시연용 가상 예시 회의록 적재")
    sub.add_parser("export-seed", help="현재 DB를 배포용 초기 데이터(data/seed.sqlite3)로 내보내기(가상 예시 제외)")
    sub.add_parser("build", help="RDF 지식그래프(TTL) 재생성")
    sub.add_parser("stats", help="적재 현황")
    gr = sub.add_parser("governance-run", help="운영 관문 G1: 자동 판정 실행 기록을 원장에 남김(자동 수집 직후)")
    gr.add_argument("--trigger", default="자동 수집")
    gr.add_argument("--force", action="store_true", help="같은 판정 결과·정책이어도 다시 기록")

    s = sub.add_parser("search", help="발언 전문검색")
    s.add_argument("query", nargs="+")
    s.add_argument("--limit", type=int, default=20)

    sm = sub.add_parser("summary", help="회의 요약(주요내용·핵심안건)")
    sm.add_argument("meeting_id")
    sm.add_argument("--llm", action="store_true", help="Claude API 요약 사용")

    b = sub.add_parser("briefing", help="국정감사 대비 브리핑(Markdown)")
    b.add_argument("--org", default="")
    b.add_argument("--issue", default="", help="쟁점 id 또는 이름(예: rice, 쌀·식량안보)")
    b.add_argument("--keyword", default="")
    b.add_argument("--member", default="")
    b.add_argument("--from", dest="date_from", default="")
    b.add_argument("--to", dest="date_to", default="")
    b.add_argument("-o", "--output", type=Path)

    q = sub.add_parser("sparql", help="지식그래프 SPARQL 질의")
    q.add_argument("query")

    sv = sub.add_parser("serve", help="웹 서비스 실행")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)

    args = ap.parse_args(argv)
    settings = get_settings()

    if args.cmd == "serve":
        import uvicorn

        from .web.app import create_app
        uvicorn.run(create_app(settings), host=args.host, port=args.port)
        return 0

    store = _store(settings)

    if args.cmd == "collect":
        from datetime import date, timedelta

        from .pipeline import collect, collect_range, fetch_minutes, rebuild_graph
        params = {"DAE_NUM": args.dae, **_kv(args.param)}
        if args.resume:
            # 마지막 회의일 7일 전부터(늦게 올라오는 회의록 대비). 휴회가 길어도 조회 일수가
            # 끝없이 늘지 않도록 최근 45일로 제한(45일 넘게 수집이 멈췄다면 --from 으로 직접 지정)
            last = store.stats()["date_max"]
            floor = date.today() - timedelta(days=45)
            args.date_from = (max(date.fromisoformat(last) - timedelta(days=7), floor).isoformat()
                              if last else TERM_START)
            print(f"수집 기간: {args.date_from} ~ {args.date_to or date.today().isoformat()}")
        elif args.days:
            args.date_from = (date.today() - timedelta(days=args.days)).isoformat()
        if args.date_from:
            meetings = collect_range(store, settings, args.date_from,
                                     args.date_to or date.today().isoformat(), params,
                                     only_target=not args.all_committees)
        else:
            meetings = collect(store, settings, {**params, "CONF_DATE": args.date},
                               only_target=not args.all_committees)
        if args.fetch:
            parsed = {m["id"] for m in store.meetings(status="parsed")}
            todo = [m["meeting_id"] for m in meetings if m["meeting_id"] not in parsed]
            if todo:
                fetch_minutes(store, settings, todo)
        rebuild_graph(store, settings)
    elif args.cmd == "fetch-minutes":
        from .pipeline import fetch_minutes, rebuild_graph
        n = fetch_minutes(store, settings, args.id, args.limit)
        print(f"본문 파싱 성공 {n}건")
        rebuild_graph(store, settings)
    elif args.cmd == "ingest":
        from .pipeline import ingest_file, rebuild_graph
        meta = {"CONFER_NUM": args.id or "", "TITLE": args.title, "CONF_DATE": args.date,
                "DAE_NUM": args.dae, "CLASS_NAME": args.kind,
                "COMM_NAME": "농림축산식품해양수산위원회", "agendas": args.agenda}
        n = ingest_file(store, args.path, meta)
        print(f"발언 {n}건 적재")
        rebuild_graph(store, settings)
    elif args.cmd == "programs":
        from .programs import ProgramError, collect as collect_programs
        try:
            n = collect_programs(store, settings, args.since)
        except ProgramError as e:
            print(f"지원사업 공고 수집 실패: {e}", file=sys.stderr)
            return 1
        print(f"지원사업 공고 {n}건 저장")
    elif args.cmd == "sample":
        from .pipeline import load_sample, rebuild_graph
        n = load_sample(store)
        g = rebuild_graph(store, settings)
        print(f"가상 예시 회의록 적재: 발언 {n}건, 트리플 {len(g)}개 → {settings.graph_path}")
    elif args.cmd == "export-seed":
        from .config import SEED_DB
        from .pipeline import export_seed
        meta = export_seed(store)
        print(json.dumps(meta, ensure_ascii=False, indent=2))
        print(f"{'저장' if meta['changed'] else '변경 없음'}: {SEED_DB}")
    elif args.cmd == "build":
        from .pipeline import rebuild_graph
        g = rebuild_graph(store, settings)
        print(f"트리플 {len(g)}개 → {settings.graph_path}")
    elif args.cmd == "governance-run":
        from . import governance as gov
        from .followup import track_all
        from .pipeline import seed_meta
        ledger = gov.ledger_from_env(settings.data_dir, serverless=False)
        pol = gov.active_policy(ledger.events())
        d = track_all(store, pol)
        payload = gov.run_payload(d, pol, seed_meta().get("hash") or "local", args.trigger)
        if gov.has_run(ledger.events(), payload["output_hash"], pol["version"]) and not args.force:
            print(json.dumps({"skipped": "같은 판정 결과·정책의 실행 기록이 이미 있음", **payload}, ensure_ascii=False))
        else:
            ev = ledger.append("run", "AI 판정", "system", payload)
            print(json.dumps({"seq": ev["seq"], "hash": ev["hash"][:12], "storage": ledger.sink.kind,
                              **ev["payload"]}, ensure_ascii=False))
    elif args.cmd == "stats":
        print(json.dumps(store.stats(), ensure_ascii=False, indent=2))
    elif args.cmd == "search":
        for r in store.search(" ".join(args.query), limit=args.limit):
            print(f"[{r['meeting_date']}] {r['speaker_role']} {r['speaker_name']}: {r['text'][:200]}")
    elif args.cmd == "summary":
        if args.llm:
            from .llm import llm_summarize
            out = llm_summarize(store, args.meeting_id, settings)
        else:
            from .analysis import summarize_meeting
            out = summarize_meeting(store, args.meeting_id)
        print(json.dumps(out, ensure_ascii=False, indent=2))
    elif args.cmd == "briefing":
        from .analysis import briefing, briefing_markdown
        md = briefing_markdown(briefing(store, args.org, args.issue, args.keyword, args.member,
                                        args.date_from, args.date_to))
        if args.output:
            args.output.write_text(md, encoding="utf-8")
            print(f"저장: {args.output}")
        else:
            print(md)
    elif args.cmd == "sparql":
        from .ontology import KG_PATH, kg_from_file, kg_from_store, run_sparql
        kg = kg_from_store(store) if store.stats()["utterances"] or not KG_PATH.exists() \
            else kg_from_file()
        res = run_sparql(kg, args.query)
        print("\t".join(res["columns"]))
        for row in res["rows"]:
            print("\t".join("" if v is None else v for v in row))
    return 0


if __name__ == "__main__":
    sys.exit(main())
