# 국회 농해수위 온톨로지 (AgriSea Audit Ontology)

배포: https://ax-rosy-seven.vercel.app/

국회 **농림축산식품해양수산위원회** 회의록을 열린국회정보 API로 모아 회의·안건·발언·발언자·피감기관·쟁점·정부 약속을 잇는 **지식그래프**로 만들고,
국정감사 준비에 필요한 검색·요약·브리핑·약속 이행 추적을 제공합니다.

```
열린국회정보 API → 회의록 PDF → 발언 파싱(질의·답변·약속 판별) → SQLite → RDF 지식그래프(SPARQL) → 웹·API·CLI
```

관련 서비스: [지원사업 레이더](support-radar/README.md) — 회의록의 정책 신호를 K-Startup 지원사업 공고와 연결

## 주요 기능

- **대시보드**: 회기별 쟁점 히트맵, 위원→쟁점→기관 흐름도, 최근 회의·정부 약속
- **회의록**: 통합 검색, 회의 요약·전체 회의록(발언 흐름 미니맵)·질의응답·약속·안건, Markdown 내보내기
- **의원·쟁점 분석**: 의원별 관심 쟁점·질의 관점·예상 질문, 쟁점별 회기 추이와 질의–답변
- **국감 브리핑**: 기관·쟁점·위원·기간 조건으로 핵심 쟁점, 질의와 답변, 약속 추적표를 정리(Markdown)
- **정부 약속 이행 추적**: 약속마다 후속 회의록·안건, 국회 의안 API, K-Startup 공고 API에서 근거를 찾아
  이행 보고 / 진행 중 / 재지적 / 확인 기간 부족 / 근거 미확인으로 판정
- **SPARQL·TTL**: 지식그래프 직접 질의와 내려받기
- (선택) **Claude 요약**: `ANTHROPIC_API_KEY`가 있으면 LLM 회의 요약

## 시작하기

```bash
pip install -r requirements.txt
cp .env.example .env                                   # ASSEMBLY_API_KEY 입력
python -m agrisea collect --dae 22 --days 14 --fetch   # 최근 14일 수집
python -m agrisea serve --port 8000                    # http://127.0.0.1:8000
```

- 인증키는 `.env`나 환경변수로만 설정하세요(저장소에 넣지 않음).
- 데이터는 `var/`에 저장됩니다(`AGRISEA_DATA_DIR`로 변경).
- 시연용 가상 데이터: `python -m agrisea sample`

## 배포 (Vercel)

저장소를 Vercel에 연결하면 `main` 푸시마다 배포됩니다(서울 리전, 60초 제한). 환경 변수:

| 이름 | 용도 |
|---|---|
| `ASSEMBLY_API_KEY` | 열린국회정보 인증키(필수) |
| `KSTARTUP_API_KEY` | 이행 추적의 지원사업 공고 근거(선택) |
| `ANTHROPIC_API_KEY`, `AGRISEA_LLM_MODEL` | Claude 요약(선택) |

배포 환경은 `/tmp`만 쓸 수 있어, 화면에서 수집한 데이터는 재시작하면 사라집니다.

## 자동화 (GitHub Actions)

| 워크플로 | 하는 일 | 필요 설정 |
|---|---|---|
| 회의록 데이터 갱신 | 하루 3번 회의록을 수집·분석해 `data/`에 커밋하면 Vercel이 재배포(국회 PDF는 서울 리전 배포본을 거쳐 받음) | Secret `ASSEMBLY_API_KEY` |
| 지원사업 공고 갱신 | 지원사업 레이더 공고 수집 | Secret `KSTARTUP_API_KEY` |
| GitLab 미러 | `main`을 `gitlab.aigov.go.kr/kimjs1408/kookgam-ax`로 푸시 | Secret `GITLAB_TOKEN`, Variable `MIRROR_RUNNER` |

기관 GitLab은 해외(GitHub 러너)에서 접속되지 않습니다. 국내 PC에 self-hosted 러너를 두고 `MIRROR_RUNNER=self-hosted`로 지정하거나,
그 PC에서 `scripts/mirror-gitlab.sh`를 주기적으로 실행하세요.

## CLI

```bash
python -m agrisea search 쌀값 시장격리            # 발언 검색
python -m agrisea summary <회의ID> [--llm]        # 회의 요약
python -m agrisea briefing --org 농촌진흥청 -o 브리핑.md
python -m agrisea sparql "SELECT ..."             # 지식그래프 질의
python -m agrisea ingest 회의록.pdf --title ... --date 2024-10-07   # PDF 직접 적재
python -m agrisea stats
```

## REST API (주요)

| 경로 | 설명 |
|---|---|
| `GET /api/meetings`, `/api/meetings/{id}`, `/{id}/summary` | 회의 목록·상세·요약 |
| `GET /api/search?q=` | 발언 검색 |
| `GET /api/issues`, `/api/issues/{id}`, `/api/graph` | 쟁점·관계망 |
| `GET /api/briefing?org=&issue=&format=md` | 국감 브리핑 |
| `GET /api/followups`, `/{id}`, `/{id}/external` | 약속 이행 추적 |
| `POST /api/sparql` | 읽기 전용 SPARQL |
| `POST /api/admin/collect`, `/fetch-minutes`, `/sample` | 수집(인증 없음) |

## 테스트

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

## 한계

- 요약과 질의·답변·약속 판별은 규칙 기반이라 오탐·누락이 있을 수 있습니다. 공식 문서에는 원문 PDF를 확인하세요.
- 이행 판정은 기록에서 찾은 신호입니다. '근거 미확인'은 미이행이 아니라 회의록에서 확인하지 못했다는 뜻입니다.
- PDF에서 사라진 띄어쓰기는 회의록 말뭉치로 학습한 규칙으로 복원합니다(`agrisea/spacing.py`).
