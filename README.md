# 국회 농해수위 온톨로지 (AgriSea Audit Ontology)
배포페이지 : https://ax-rosy-seven.vercel.app/
국회 **열린국회정보 「위원회 회의록」 Open API**(`ncwgseseafwbuheph`)로 **농림축산식품해양수산위원회** 회의록을 모아
회의·안건·발언·발언자·피감기관·쟁점·이행약속을 잇는 **지식그래프(온톨로지)** 로 구조화합니다.
이 그래프를 바탕으로 국정감사 준비에 쓸 **회의내용 검색 · 주요내용 정리 · 핵심안건 요약 · 기관/쟁점별 브리핑** 기능을 제공합니다.

```
열린국회정보 API ──► 회의 메타데이터(회의명·일자·안건·PDF 링크)
        │
        ▼
회의록 PDF ──► 발언 단위 파싱(◯발언자 구분, 안건 전환, 질의/답변/이행약속/자료요구 판별)
        │
        ▼
SQLite(FTS5 전문검색) ──► RDF 지식그래프(OWL/SKOS, Turtle) ──► SPARQL
        │
        ▼
웹 UI / REST API / CLI : 검색 · 회의 요약 · 쟁점 관계망 · 국감 브리핑(Markdown 내보내기)
```

> **관련 서비스: [지원사업 레이더](support-radar/README.md)** — 이 회의록 데이터로 국회 정책 신호(주제별 관심도·정부 약속)를 뽑아
> K-Startup 지원사업 공고와 연결하고, 기업 프로필로 공고별 신청 자격을 판정하는 별도 서비스(`support-radar/`).

## 주요 기능

| 기능 | 내용 |
|---|---|
| 공유 링크 | 주소로 화면 공유: `#/meetings/회의ID`, `#/issues/쟁점ID`, `#/brief` |
| 회기 선택 | 화면 상단에서 회기(예: 제418회 정기회)를 고르면 회의록·검색·쟁점 상세·브리핑이 그 회기로 좁혀짐 |
| 대시보드 | 세로로 길게 늘어지지 않도록 카드를 가로로 나란히 두고, 긴 목록은 상자 안에서 스크롤. 첫 화면 요약(최근 회기·가장 많이 다룬 쟁점·핵심 수치), **회기별 쟁점 흐름 히트맵**(최근 14개 회기, 칸마다 언급 횟수·합계), **위원→쟁점→기관 관계 흐름도**(띠 굵기 = 함께 언급된 발언 수, 가리키면 연결만 강조, 누르면 쟁점 분석·의원 분석·기관 브리핑 / 3D 신경망 보기로 전환), 쟁점 순위, 최근 회의, 최근 정부 약속 |
| 회의록 | **회의록 중심 화면**: 상단 통합 검색(발언자 필터·회기별 언급·관련 쟁점), 회의 목록, 회의 상세 탭(요약 / 전체 회의록 / 질의응답 / 약속·자료요구 / 안건). 전체 회의록은 긴 발언을 문단으로 나누고 발언자별 색·머리글자로 구분하며, 안건 머리글이 화면 위에 붙어 따라오고, **발언 흐름 미니맵**(회의 전체를 한 줄로, 폭 = 발언 길이·색 = 위원/정부/위원장/약속, 누르면 그 발언으로 이동)·발언자 거르기·안건 바로가기·회의 내 찾기를 제공, **내 AI로 분석**(ChatGPT·Claude 새 창 열기 + 전문 클립보드 복사, 전문 Markdown 링크·다운로드 `/api/meetings/{id}/export.md`) |
| 의원 분석 | 의원별 관심 쟁점·질의 관점(예산·감독·피해 등, 전체 의원 평균 대비)·핵심어(TF-IDF, 명사만)·상대 기관·회기별 질의, 과거 질의·받아낸 약속·반복 제기 핵심어 기반 **예상 질문**. 범위: 전체/회기/최근 회의 5·10건, 회의 상세의 「의원별 관점」 탭(`/api/members`, `/api/members/{이름}?meetings=`) |
| 정부 약속 이행 추적 | 회의에서 정부·기관이 한 약속마다 무엇을 하겠다는지(앞선 위원 질의·약속 문장의 대상어, 법률명, 조치 유형)를 뽑고, 그 뒤 기록에서 이행 근거를 모아 **이행 보고 / 진행 중 / 재지적(미이행 의심) / 확인 기간 부족 / 근거 미확인**으로 판정. 근거: 후속 회의록(같은 기관의 '마련했습니다·개정했습니다' 보고, '추진 중' 답변, 위원의 '아직도·약속하셨는데' 재지적), 후속 안건(같은 법률의 개정법률안 상정), **국회 의안 API**(약속 뒤 발의된 개정안과 처리 결과), **K-Startup 지원사업 공고 API**(약속 뒤 같은 주제 공고). 근거 타임라인에서 해당 발언·의안·공고로 바로 이동 |
| 운영 관문(AI 전제 업무 흐름) | 이행 판정을 **AI가 먼저 수행**하고(G1 접수), 판정마다 **위험 점수**(G2)로 **저위험 자동 처리 · 표본 검증 · 고위험 숙련자 이관**(G3). 숙련자 **승인·거부(정정 판정·사유·문제 대상어)**(G4), **롤백**·AI 판정 변경 시 재검토(G5), 결정마다 **평가 데이터**(일치율·혼동 행렬·표본 정확도, CSV/JSONL)(G6), 쌓인 예외에서 **규칙 개정 제안 → 관리자 채택 → 정책 버전**으로 다음 판정부터 적용(G7). 모든 행위는 **해시로 이어진 원장**에 남고 증거 보고서로 내보냄. 화면에는 표시하지 않고 API(`/api/governance/*`)와 증거 보고서로만 운영(「이행 추적」의 공개 판정은 숙련자 정정을 따름) |
| 쟁점 분석 | 쟁점 분류(SKOS 상·하위) → 쟁점 상세: 회기별 추이(막대 위 수치), 질의 위원 → 쟁점 → 관련 기관 흐름도, 질의(왼쪽)–정부 답변·약속(오른쪽) 대화 |
| 주요내용 정리 | 회의별 개요, 핵심 쟁점, 키워드, 중요 문장 추출 요약 |
| 핵심안건 요약 | 안건별 발언 구간을 나눠 요약·발언자·쟁점 정리, 주요 질의–답변 쌍 선별 |
| 국감 브리핑 | 피감기관·쟁점·키워드·위원·기간 조건으로 과거 회의록을 모아 핵심 쟁점, 주요 질의와 정부 답변, **이행약속 추적표**, 관심 위원, 관련 회의를 한 번에 정리(Markdown 다운로드) |
| 고급 도구(하단 링크) | SPARQL 지식그래프 직접 질의(예시 질의 제공), 전체 그래프 TTL 내려받기 |
| (선택) Claude 심층 요약 | `ANTHROPIC_API_KEY`가 있으면 회의별 핵심안건·쟁점·정부 입장·국감 후속 점검사항을 LLM으로 요약 |

## 운영 관문: AI 전제 업무 재설계와 운영 증거 (`agrisea/governance.py`)

업무 시작점을 사람이 아닌 **AI**로 두고, 사람(숙련자·관리자)은 위험이 높은 판정과 예외, 제도 개정에 집중하도록 경로와 역할을 다시 설계했습니다.
적용 업무는 결과가 가장 무거운 **정부 약속 이행 판정**입니다.

| 관문 | 하는 일 | 역할 | 운영 증거 |
|---|---|---|---|
| G1 접수 | 자동 수집 → 약속 인식 → 이행 판정을 AI가 수행 | AI | `run` 이벤트(데이터 해시·기준일·정책 버전·경로별 건수). 자동 수집(Actions)마다 기록 |
| G2 위험평가 | 판정 종류(이행·미이행 단정↑)·조치 유형(법·예산↑)·근거 강도(약함·1건·의원 안건뿐↑)로 0~100점 | AI | 건마다 점수와 요인 |
| G3 경로배정 | 기준 점수 이상 → 숙련자 이관, 미만 → 자동 공개, 그중 일부(해시로 재현 가능한 표본) → 표본 검증 | AI | 경로·적용 정책 버전 |
| G4 숙련자 검토 | 승인(AI 판정 확정) / 거부(올바른 판정·사유 코드·문제 대상어·메모). 공개 판정은 숙련자 결정을 따름 | 검토자 | `decision` 이벤트(판정 지문·위험·정책 버전 포함) |
| G5 예외처리 | 직전 결정 롤백(사유 필수, 남의 결정은 관리자만). 데이터가 바뀌어 AI 판정이 달라지면 이전 결정은 '재검토 필요' | 검토자·관리자 | `rollback` 이벤트 |
| G6 평가 데이터 | 결정마다 (AI 판정, 최종 판정, 일치, 경로, 위험 요인, 정책 버전) → 일치율(95% 구간)·표본 정확도·혼동 행렬·거부 사유·정책 버전별 효과 | — | `/api/governance/dataset.csv`·`.jsonl` |
| G7 제도 반영 | 같은 대상어가 거부 근거로 반복 → 차단 제안, 표본 일치율 저조 → 이관 기준·표본 비율 강화 제안, 이관 건 일치율 높음 → 자동화 확대 제안. 관리자 채택 시 새 정책 버전 → 다음 판정부터 적용(화면 조회·자동 수집 모두) | 관리자 | `policy`·`proposal_dismissed` 이벤트, 정책 이력 |

- **원장**: 모든 이벤트는 앞 이벤트의 해시를 품은 추가 전용 기록이라 중간 수정·삭제가 드러납니다(상태 API·보고서에 연결 검증 표시).
  `AGRISEA_GOV_GITHUB_REPO`·`AGRISEA_GOV_GITHUB_TOKEN`(contents 쓰기 권한)을 주면 저장소의 `governance-log` 브랜치(`governance/events.jsonl`)에
  커밋해 영구 보관합니다(커밋 이력 자체도 증거, Vercel은 이 브랜치를 배포하지 않음). 없으면 로컬 파일에 쓰며, 배포 환경(/tmp)에서는 재시작 때 사라진다는 것을 상태 API(`durable: false`)로 알립니다.
- **역할**: `AGRISEA_REVIEWERS`(검토자), `AGRISEA_ADMINS`(관리자)에 `이름:토큰`을 쉼표로 넣고, 결정·롤백·채택 요청에 `X-Agrisea-Token` 헤더로 토큰을 보냅니다.
  토큰이 없으면 배포 환경은 열람만 되고, 로컬 서버(`serve`)는 개발 편의를 위해 관리자 권한으로 열립니다.
- **자동 수집과의 연결**: 「회의록 데이터 갱신」이 `governance-log`의 최신 정책으로 판정하고(정책이 바뀌면 같은 데이터라도 다시 계산), 실행 기록을 원장에 커밋합니다.
- 증거 보고서: `/api/governance/report.md`(관문별 지표·정책 이력·최근 원장).

## 온톨로지 개요 (`ontology/agrisea.ttl`)

```
ag:Committee ◄─ag:heldBy─ ag:Meeting ─ag:hasAgendaItem─► ag:AgendaItem
                           │  (AuditMeeting / PlenaryCommitteeMeeting / SubcommitteeMeeting)
                           │─ag:audited─► ag:AuditedAgency (피감기관)
                           │─ag:hasUtterance─► ag:Utterance ─ag:spokenBy─► ag:Person
                           │                     │ (Question / Answer / Commitment / DataRequest)
                           │                     │─ag:respondsTo─► ag:Question
                           │                     │─ag:concernsIssue─► ag:Issue (skos:Concept, skos:broader)
                           │                     │─ag:mentionsOrganization─► ag:Organization
                           │                     └─ag:committedBy─► ag:Organization (이행약속 주체)
ag:Person: Legislator / Chair / GovernmentOfficial / Witness / Staff ─ag:affiliatedWith─► ag:Organization
```

- 쟁점 분류체계와 피감기관 사전은 `agrisea/lexicon.py`에 있습니다. 키워드를 추가하면 재색인 시 그래프에 반영됩니다.
- **이행약속(ag:Commitment)**: 정부·기관 답변 중 구체적 조치 약속(검토·개선·마련·제출·점검 등 + '하겠습니다')만 표시합니다.
  인사말·의례 문장("감사드립니다", "최선을 다하겠습니다")과 업무보고 시작 인사("○○과장 보고드리겠습니다")는 제외합니다.
- **회기**: 회의 제목의 「제○○회」로 묶고, 첫 회의가 9월인 회기를 정기회로 표시합니다.

## 설치

```bash
pip install -r requirements.txt
cp .env.example .env      # ASSEMBLY_API_KEY=발급받은_인증키 입력
```

> 인증키는 **코드나 저장소에 넣지 말고** `.env`(git에서 제외됨) 또는 환경변수로만 설정하세요.

## 사용법

### 1) 데이터 수집

```bash
# 22대 국회, 특정 회의일자의 농해수위 회의 목록 + 회의록 본문(PDF) 수집
python -m agrisea collect --dae 22 --date 2024-10-07 --fetch

# API 요청인자를 직접 추가(KEY=VALUE 반복 가능)
python -m agrisea collect --dae 22 --param CONF_DATE=2024 --fetch

# 기간 수집(회의일자를 하루씩 바꿔 호출) / 최근 N일 / 마지막 수집일부터 이어서
python -m agrisea collect --dae 22 --from 2024-10-01 --to 2024-10-31 --fetch
python -m agrisea collect --dae 22 --days 14 --fetch
python -m agrisea collect --resume --fetch

# 이미 저장된 회의 중 본문 미수집 건만 다시 내려받기
python -m agrisea fetch-minutes --limit 50
```

- 수집 결과는 `var/agrisea.sqlite3`, 지식그래프는 `var/agrisea_kg.ttl`에 저장됩니다(`AGRISEA_DATA_DIR`로 변경 가능).
- 위원회 필터: 위원회명/회의명에 `농림축산식품해양수산위원회`(과거 명칭 포함)가 들어간 행만 저장합니다. 다른 위원회까지 받으려면 `--all-committees`.
- API 필수 요청인자는 `DAE_NUM`(대수)과 `CONF_DATE`(회의일자)입니다. `CONF_DATE`는 `2024`, `2024-10`, `2024-10-07` 형식이며
  `20241007`처럼 입력해도 자동 변환합니다(구분자 없는 형식은 API가 빈 결과를 돌려줌). 빠지면 `ERROR-300`이 납니다.
- 출력 필드명(`CONFER_NUM`, `TITLE`, `COMM_NAME`, `CONF_DATE`, `SUB_NAME`, `PDF_LINK_URL` 등)은 `agrisea/api_client.py`의 `FIELD_ALIASES`에서 별칭까지 흡수합니다. 실제 응답 필드가 다르면 여기에 추가하면 됩니다.

API 접근이 어려운 환경에서는 내려받은 회의록 PDF를 직접 적재할 수 있습니다.

```bash
python -m agrisea ingest 회의록.pdf --title "제418회 국회(정기회) 제1차 농림축산식품해양수산위원회" \
  --date 2024-10-07 --kind 국정감사 --agenda "농림축산식품부에 대한 국정감사"
```

### 2) Vercel 배포

저장소에 Vercel용 설정(`vercel.json`, `api/index.py`)이 들어 있어 GitHub 저장소를 그대로 가져오면 됩니다.

1. [vercel.com/new](https://vercel.com/new) → **Import Git Repository** → 이 저장소 선택
   (Framework Preset: **Other**, Build/Output 설정은 비워 둠)
2. **Environment Variables**에 다음을 등록(Production·Preview 모두)

   | 이름 | 필수 | 설명 |
   |---|---|---|
   | `ASSEMBLY_API_KEY` | 예 | 열린국회정보 인증키 |
   | `ANTHROPIC_API_KEY` | 아니오 | Claude 심층 요약 사용 시 |
   | `AGRISEA_LLM_MODEL` | 아니오 | 기본 `claude-opus-5` |
   | `AGRISEA_REVIEWERS`, `AGRISEA_ADMINS` | 운영 관문 사용 시 | 검토자·관리자 `이름:토큰` 목록(없으면 열람만) |
   | `AGRISEA_GOV_GITHUB_REPO`, `AGRISEA_GOV_GITHUB_TOKEN` | 운영 관문 사용 시 | 운영 증거 원장 영구 보관(`governance-log` 브랜치) |
   | `KSTARTUP_API_KEY` | 아니오 | 이행 추적에서 지원사업 공고(K-Startup, 공공데이터포털 일반 인증키 Decoding)를 근거로 찾을 때 |

3. **Deploy**. 이후 `main`에 푸시할 때마다 자동 배포됩니다.

배포 후 대시보드 「데이터 수집」에서 회의일자(또는 31일 이내 기간)를 지정해 수집합니다.
수집 기능에는 별도 인증이 없어 URL을 아는 누구나 실행할 수 있습니다(인증키 사용량이 소모될 수 있음).
인증키는 서버(함수)에서만 쓰이고 브라우저로 전달되지 않으며, 오류 메시지에서도 `KEY=***`로 가려집니다.

**회의록 자동 수집(권장)**: GitHub Actions 「회의록 데이터 갱신」(`.github/workflows/refresh-data.yml`)이
하루 3번(07:17·13:17·19:17 KST) 국회 API로 회의록을 수집·파싱해 `data/seed.sqlite3.gz`로 커밋하고, Vercel이 재배포하면서 이 데이터를 싣습니다.
정각 예약은 GitHub 혼잡 시간대라 2~4시간씩 늦게 시작되는 일이 잦아 17분으로 비켜 두었습니다.

1. 저장소 **Settings → Secrets and variables → Actions**에 `ASSEMBLY_API_KEY` 등록
2. 처음 한 번은 22대 임기 시작일(2024-05-30)부터 전체를 수집합니다(이 워크플로가 `main`에 머지될 때 자동 실행,
   또는 **Actions → 회의록 데이터 갱신 → Run workflow**). 이후에는 마지막 회의일 7일 전부터 이어서 수집합니다.
3. 내용이 바뀐 때만 커밋되며, 화면 대시보드에 마지막 갱신 시각(한국시간)이 표시됩니다.
   - 국회 API 요청은 실패 시 2·4·8·16초 간격으로 재시도하고, 그래도 막힌 날짜는 건너뛰고 나머지를 저장합니다(모든 날짜가 막히면 실패 처리).
   - 휴회가 길어도 조회 일수가 끝없이 늘지 않도록 이어 받기는 최근 45일 안에서 합니다.
   - 예약 실행이 실패하면 「회의록 자동 수집 실패」 이슈가 열리고(반복 실패는 댓글로 추가), 다음 실행이 성공하면 자동으로 닫힙니다.
4. 수집 직후 **회의록 본문 띄어쓰기 교정**(아래 참고)과 **회의별 요약, 쟁점·기관 집계, 쟁점–기관–위원 관계망, 지식그래프(`data/kg.nt.gz`)** 를 미리 계산해 함께 싣습니다.
   배포 환경은 이 결과를 바로 보여 주고, SPARQL은 `pyoxigraph`로 질의합니다(첫 질의 때 그래프 적재 약 1초).
   화면에서 직접 수집하면 해당 인스턴스에서 집계를 다시 계산합니다.

회의록 PDF 서버(`record.assembly.go.kr`)는 해외 접속이 막혀 있어 GitHub Actions에서 직접 받을 수 없습니다.
그래서 자동 수집은 서울 리전 배포본의 `/api/minutes-text?id=<회의번호>`를 거쳐 본문을 받습니다
(국회 회의록 주소만 조회하도록 고정). 배포 주소가 바뀌면 저장소 **Variables**에 `MINUTES_PROXY`를 지정하세요.

배포 환경(Vercel)은 `/tmp`만 쓸 수 있어 화면에서 직접 수집한 데이터는 서버가 재시작되면 사라집니다(확인·시연용).
초기 데이터에는 전문검색 인덱스를 넣지 않고 압축하며, 배포 환경에서는 일반 문자열 검색을 씁니다.
로컬 DB를 직접 올리려면 `python -m agrisea export-seed` 후 `data/seed.sqlite3.gz`, `data/seed.meta.json`을 커밋하세요.

함수 리전은 국회 API와 가까운 서울(`icn1`), 최대 실행시간은 60초로 설정되어 있습니다(`vercel.json`).
회의록 본문은 실행시간 제한 때문에 화면에서 3건씩 나눠 자동 반복 처리합니다.

### 3) 로컬 웹 서비스

```bash
python -m agrisea serve --port 8000     # http://127.0.0.1:8000
```

탭 구성: 대시보드 · 회의내용 검색 · 회의록·요약 · 쟁점 온톨로지 · 국감 브리핑 · SPARQL.
대시보드에서 바로 API 수집도 실행할 수 있습니다(`/api/admin/*`, 별도 인증 없음).

### 4) CLI

```bash
python -m agrisea search 쌀값 시장격리                 # 발언 검색
python -m agrisea summary <회의ID>                     # 회의 요약(JSON)
python -m agrisea summary <회의ID> --llm               # Claude 심층 요약(ANTHROPIC_API_KEY 필요)
python -m agrisea briefing --org 농촌진흥청 -o 브리핑.md
python -m agrisea briefing --issue sea --from 2024-01-01
python -m agrisea sparql "PREFIX ag: <https://w3id.org/agrisea/ontology#> SELECT (COUNT(?c) AS ?n) WHERE { ?c a ag:Commitment }"
python -m agrisea stats
```

### 5) 시연용 예시 데이터

```bash
python -m agrisea sample
```

`agrisea/data/sample/`의 회의록 4건은 **기능 시연·테스트용 가상 데이터**입니다(인물·수치 모두 실제와 무관, 회의명에 `[예시·가상]` 표기).
실제 분석 전에는 `var/` 폴더를 지우고 API로 수집한 데이터만 사용하세요.

## REST API

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/api/stats` | 적재 현황 |
| GET | `/api/sessions` | 회기 목록(정기회/임시회, 기간, 회의 수) |
| GET | `/api/overview`, `/api/issue-trend` | 대시보드용 최근 회의·약속, 회기×쟁점 언급량 |
| GET | `/api/issues/{쟁점id}?session=` | 쟁점 상세(회기별 추이, 관련 기관·위원, 대표 질의, 정부 약속) |
| GET | `/api/meetings?session=&q=` | 회의 목록 |
| GET | `/api/meetings/{id}` | 회의 + 전체 발언 |
| GET | `/api/meetings/{id}/summary?llm=false` | 회의 요약(주요내용·핵심안건·질의답변·이행약속) |
| GET | `/api/search?q=&speaker_type=&session=` | 발언 검색 |
| GET | `/api/briefing?org=&issue=&keyword=&member=&session=&format=json\|md` | 국감 브리핑 |
| GET | `/api/issues`, `/api/taxonomy`, `/api/orgs`, `/api/speakers` | 쟁점·기관·발언자 집계 |
| GET | `/api/graph` | 쟁점–기관–위원 관계망 |
| GET | `/api/governance/overview`, `/status` | 관문별 지표·원장 보관 방식·해시 연결 검증·현재 정책·내 역할 |
| GET | `/api/governance/queue?view=pending\|reviewed\|auto\|all` | 검토 대기열(위험 높은 순) |
| GET·POST | `/api/governance/cases/{id}` · `/decision` · `/rollback` | 건 상세(위험 요인·근거·이력) · 승인/거부 · 롤백(검토자 토큰 `X-Agrisea-Token`) |
| GET | `/api/governance/metrics`, `/dataset.csv`, `/dataset.jsonl` | 평가 지표·평가 데이터 |
| GET·POST | `/api/governance/proposals` · `/{id}/adopt` · `/{id}/dismiss` | 규칙 개정 제안·정책 이력 · 채택/기각(관리자) |
| GET | `/api/governance/ledger`, `/report.md` | 운영 증거 원장 · 증거 보고서 |
| GET | `/api/followups?q=&org=&action=&session=` | 정부 약속 이행 추적 목록과 상태별 개수 |
| GET | `/api/followups/{id}` | 약속 상세: 앞선 질의, 대상어·법률명, 이행 근거 타임라인 |
| GET | `/api/followups/{id}/external?refresh=false` | 국회 의안·지원사업 공고 API로 외부 근거 확인(하루 동안 저장) |
| POST | `/api/sparql` `{"query": "..."}` | 읽기 전용 SPARQL(SERVICE/갱신 구문 차단) |
| GET | `/api/ontology.ttl` | 온톨로지 스키마(Turtle). 인스턴스 전체는 저장소의 `data/kg.nt.gz`(N-Triples) |
| GET | `/api/minutes-text?id=<회의번호>` | 국회 회의록 PDF 본문 텍스트(자동 수집용) |
| POST | `/api/admin/collect` `{"dae","date"}` 또는 `{"dae","date_from","date_to"}` | 회의 목록 수집 |
| POST | `/api/admin/fetch-minutes?limit=3` | 본문 미수집 회의를 limit건씩 처리, 남은 건수 반환 |
| POST | `/api/admin/sample` | 가상 예시 데이터 적재 |

## 테스트

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

API 클라이언트(응답 파싱·페이지네이션·위원회 필터), 회의록 파서, 요약·브리핑, 온톨로지/SPARQL, 웹 API를 검사합니다.

## 한계와 참고

- 요약은 기본적으로 **규칙 기반 추출 요약**(키워드 밀도·도메인 용어·수치 가중)입니다. 자연스러운 서술형 요약이 필요하면 Claude 요약을 켜세요.
- **이행 추적 판정은 기록에서 찾은 신호**입니다. 같은 기관이 같은 대상어(드문 복합 명사 둘 이상)나 법률 이름을 한 문장에서 '했다'고 말해야 이행 보고로 보고,
  넓은 말('어업인', '산불')만 겹치는 업무보고 문장은 근거로 삼지 않아 놓치는 경우가 많습니다(보수적 판정). '근거 미확인'은 미이행이 아니라 회의록에서 확인하지 못했다는 뜻이며,
  공식 이행 여부는 정부 서면 답변·시정조치 결과보고서로 확인하세요. 대상이 드러나지 않는 약속("지적사항 반영하겠습니다")은 추적에서 뺍니다.
  국회 의안 API는 국회의원 발의 법률안만 다루며, 두 외부 API는 국내 접속이 필요해 서울 리전 배포본에서 호출합니다.
- 질의/답변/이행약속 판별은 문형 패턴 기반이라 오탐·누락이 있을 수 있습니다. 공식 결과보고서 작성 시 원문(PDF 링크)을 반드시 확인하세요.
- 회의록 PDF는 양쪽 정렬이라 텍스트로 뽑으면 띄어쓰기가 사라지거나('저희들이간략하게정리를') 줄 끝에서 낱말이 끊깁니다('정 부가').
  `agrisea/spacing.py`가 회의록 말뭉치에서 띄어쓰기가 정상인 문장의 어절 빈도를 학습해, 붙은 덩어리는 가장 그럴듯한 어절 열로 나누고
  끊긴 어절은 붙인 형태가 훨씬 흔할 때 붙입니다(문장부호 뒤 공백, 목차 점선, 떨어진 조사도 정리). 발언마다 한 번만 교정하며(`utterances.spacing`),
  규칙을 바꾸면 `SPACING_VERSION`을 올려 다시 교정합니다. 실데이터에서 10음절 넘게 붙은 덩어리가 51,062개에서 2,886개로 줄었습니다.
- PDF 텍스트 추출 품질(띄어쓰기 손실 등)에 따라 발언자 인식률이 달라질 수 있어, 띄어쓰기 없는 표기(`◯홍길동위원`)도 처리하도록 패턴을 두었습니다.
