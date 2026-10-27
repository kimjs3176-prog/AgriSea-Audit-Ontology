# 국회 농해수위 온톨로지 (AgriSea Audit Ontology)

배포: https://ax-rosy-seven.vercel.app/

## 서비스 개요

국회 **농림축산식품해양수산위원회** 회의록을 모아 회의·안건·발언·위원·피감기관·쟁점·정부 약속·지원사업 공고를 잇는
**지식그래프**로 만들고, 국정감사 준비에 필요한 검색·요약·브리핑·약속 이행 추적을 제공합니다.

| 데이터 | 출처 |
|---|---|
| 위원회 회의록 | 열린국회정보 「위원회 회의록」 API + 국회 회의록 PDF |
| 법률안 처리 현황 | 열린국회정보 「국회의원 발의법률안」 API |
| 지원사업 공고 | 공공데이터포털 「창업진흥원 K-Startup 조회서비스」(농림·해양수산 관련 공고만) |

## 사용 방법

| 화면 | 이렇게 씁니다 |
|---|---|
| 대시보드 | 회기별 쟁점 흐름, 위원→쟁점→기관 관계, 최근 회의·정부 약속, 접수 중인 지원사업 공고를 한눈에 봅니다. |
| 회의록 | 상단 검색으로 발언을 찾고, 회의를 골라 요약·전체 회의록·질의응답·약속·안건을 봅니다. 발언 흐름 막대를 누르면 그 발언으로 이동합니다. |
| 의원 분석 | 의원별 관심 쟁점, 질의 관점, 받아낸 약속, 예상 질문을 봅니다. |
| 쟁점 분석 | 쟁점을 고르면 회기별 추이, 질의 위원·기관, 질의와 정부 답변, **관련 지원사업 공고**와 국회 관심 대비 공고 공급(공백·활발 등)을 봅니다. |
| 이행 추적 | 정부 약속마다 뒤 회의록·안건·법률안·**지원사업 공고**에서 찾은 근거로 이행 보고 / 진행 중 / 재지적 / 근거 미확인을 판정합니다. |
| 국감 브리핑 | 기관·쟁점·위원·기간을 골라 핵심 쟁점, 주요 질의와 답변, 약속 추적표(기관이면 그 기관의 지원사업 공고까지)를 만들고 Markdown으로 내려받습니다. |
| SPARQL | 화면 아래 링크에서 지식그래프를 직접 질의합니다(예시 질의 제공). |

화면 위의 회기 선택으로 범위를 좁힐 수 있고, 주소(`#/meetings/회의ID`, `#/issues/쟁점ID`, `#/brief`)로 화면을 공유할 수 있습니다.

## 설치·실행

```bash
pip install -r requirements.txt
cp .env.example .env                                   # 인증키 입력
python -m agrisea collect --dae 22 --days 14 --fetch   # 최근 14일 회의록 수집
python -m agrisea programs                             # 지원사업 공고 수집
python -m agrisea serve --port 8000                    # http://127.0.0.1:8000
```

그 밖의 명령: `search`(발언 검색), `summary <회의ID>`(회의 요약), `briefing --org 기관명 -o 파일.md`, `sparql "질의"`, `stats`.
시연용 가상 데이터는 `python -m agrisea sample`(실제 회의록이 없을 때만)입니다.

## 배포·자동 갱신

| 설정 | 어디에 | 용도 |
|---|---|---|
| `ASSEMBLY_API_KEY` | Vercel 환경 변수, GitHub Actions Secrets | 열린국회정보 인증키(필수) |
| `KSTARTUP_API_KEY` | GitHub Actions Secrets, Vercel 환경 변수 | 공공데이터포털 인증키(지원사업 공고) |
| `ANTHROPIC_API_KEY` | Vercel 환경 변수 | (선택) Claude 회의 요약 |

- **회의록 데이터 갱신**(Actions, 하루 3번): 회의록과 지원사업 공고를 수집·분석해 `data/`에 커밋하면 Vercel이 다시 배포합니다.
  국회 PDF와 공공데이터포털이 해외 접속을 막으면 서울 리전 배포본(`/api/minutes-text`, `/api/kstartup`)을 거쳐 받습니다.
- **GitLab 미러**(Actions): `main`을 기관 GitLab으로 푸시합니다. 기관 GitLab은 해외(GitHub 호스트 러너)에서 접속되지 않아 국내 PC의 self-hosted 러너(`MIRROR_RUNNER`) 또는 PC 예약 스크립트(`scripts/mirror-gitlab.ps1`·`.sh`)로 실행합니다 → [설정 방법](docs/gitlab-mirror.md)

## 테스트

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

요약·질의응답·약속 판별과 이행 판정은 규칙 기반이라 놓치거나 잘못 잡는 경우가 있습니다. 공식 문서에는 원문을 확인하세요.
