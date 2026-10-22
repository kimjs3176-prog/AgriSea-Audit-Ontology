# 지원사업 레이더 (Support Radar)

**K-Startup 지원사업 공고**(창업진흥원 조회서비스, 공공데이터포털)를 **국회 농해수위 회의록 정책 신호**와 엮은 별도 서비스입니다.
같은 저장소의 [국회 농해수위 온톨로지](../README.md)가 모은 회의록을 근거로 다음 두 가지를 제공합니다.

1. **맞춤 매칭 + 자격 자동 판정**: 프로필(예비/창업기업, 개업일, 나이, 소재지, 관심 분야, 사업 키워드)을 넣으면
   공고마다 **신청 가능 / 조건부 / 불가**와 판정 근거, 적합도, 마감 D-day, 신청 가능 공고의 **최대 지원액 합계**를 보여 줍니다.
2. **정책 레이더**: 주제(스마트팜·푸드테크·청년농·블루푸드 등 13개)별로
   - 국회 관심도(최근 12개월 회의록 언급, 전년 대비 비중 증감)와 공고 공급(최근 12개월 공고 수)을 한 지도에 놓고
     **공백·기회 / 활발 / 공고 우위 / 잠재**로 판정합니다. 국회 관심은 높은데 공고가 적으면 신규·확대 공고 가능성을 살펴볼 만한 신호입니다.
   - **정부 약속 추적**: 국회에서 장관·기관장이 한 약속(예: "청년농업인의 기술 창업을 지원하는 사업도 신규 추진하겠습니다")마다
     그 뒤에 같은 주제의 공고가 나왔는지 보여 줍니다.
   - 공고 상세에서는 **이 공고의 정책 배경**(관련 국회 질의·정부 약속)을 함께 보여 줍니다.

```
K-Startup API ─┐                         ┌─► 맞춤 매칭·자격 판정 (eligibility.py)
 (공고·사업소개) ├─► catalog.json.gz ──────┤
               │                         └─► 정책 레이더: 관심도×공급, 약속→공고 추적 (policy.py)
국회 회의록 DB ─┴─► policy_signals.json ───┘
 (../data/seed.sqlite3.gz)
```

## 구성

| 파일 | 내용 |
|---|---|
| `radar/kstartup.py` | K-Startup API 클라이언트(JSON/XML, 페이지네이션, 재시도, 인증키 가림, 필드 정규화) |
| `radar/topics.py` | 국회 발언과 공고를 같은 기준으로 묶는 주제 사전(키워드 추가 시 재계산) |
| `radar/policy.py` | 회의록 → 주제별 언급·질의·정부 약속(신호), 신호 × 공고 → 공백 판정·약속 추적 |
| `radar/eligibility.py` | 창업기간·연령·지역·신청대상·본문 조건(업력 N년 이내, 만 N세 이하, 여성·재창업 등) 판정, 지원금액 추출, 적합도 |
| `radar/llm.py` | (선택) Claude로 공고문 자격 조건 구조화 → 판정에 함께 사용 |
| `radar/catalog.py` | 공고 스냅숏 보관·갱신 |
| `radar/web/` | FastAPI + 단일 페이지 UI |
| `data/` | 자동 수집이 커밋하는 `catalog.json.gz`(공고), `policy_signals.json`(국회 신호) |

## 로컬 실행

```bash
cd support-radar
pip install -r requirements-dev.txt
cp .env.example .env              # KSTARTUP_API_KEY=공공데이터포털 일반 인증키
python -m radar collect           # 2024-05-30 이후 공고 + 사업 소개 수집 → data/catalog.json.gz
python -m radar collect --open-only   # 접수 중 공고만 빠르게
# 국회 정책 신호(저장소 루트의 회의록 DB 사용)
mkdir -p var && gunzip -c ../data/seed.sqlite3.gz > var/agrisea.sqlite3
PYTHONPATH=.:.. python -m radar signals --agrisea-db var/agrisea.sqlite3
python -m radar serve --port 8100     # http://127.0.0.1:8100
```

CLI로도 확인할 수 있습니다.

```bash
python -m radar match --kind company --founded 2024-03-01 --age 34 --region 전북 --keywords "스마트팜 IoT"
python -m radar radar     # 주제별 판정표
python -m radar rules     # (선택) ANTHROPIC_API_KEY 설정 시 자격 조건 구조화
python -m radar stats
```

> 인증키는 **코드나 저장소에 넣지 말고** `.env`(git 제외) 또는 환경변수·GitHub Secrets·Vercel 환경변수로만 설정하세요.

## 배포(Vercel, 별도 프로젝트)

1. [vercel.com/new](https://vercel.com/new) → 이 저장소를 한 번 더 Import
2. **Root Directory**를 `support-radar`로 지정 (Framework Preset: Other)
3. Environment Variables: `KSTARTUP_API_KEY`(필수), `ANTHROPIC_API_KEY`(선택)
4. Deploy. 함수 리전은 공공데이터포털과 가까운 서울(`icn1`)입니다.

배포본은 저장소에 커밋된 `data/`를 읽고, 화면에서 「지금 K-Startup에서 받기」(또는 `POST /api/admin/refresh`)로
접수 중 공고를 새로 받을 수 있습니다(10분 간격 제한, `/tmp`라 재시작 시 사라짐).

## 자동 갱신(GitHub Actions)

`.github/workflows/refresh-radar.yml` 「지원사업 공고 갱신」이 하루 2번(08:47·20:47 KST)
국회 정책 신호를 다시 계산하고 K-Startup 공고를 수집해 `support-radar/data/`에 커밋합니다.

1. 저장소 **Settings → Secrets and variables → Actions**에 `KSTARTUP_API_KEY` 등록 (선택: `ANTHROPIC_API_KEY`)
2. 공공데이터포털이 해외(Actions) 접속을 막으면 **Variables**에 `RADAR_PROXY=https://<배포주소>/api/kstartup` 지정
   → 서울 리전 배포본이 인증키를 붙여 대신 조회합니다(조회 오퍼레이션 두 개만 허용).
3. 처음 한 번은 워크플로가 `main`에 머지될 때, 또는 **Actions → 지원사업 공고 갱신 → Run workflow**로 실행합니다.

## REST API

| 메서드 | 경로 | 설명 |
|---|---|---|
| POST | `/api/match` `{kind, founded, age, region, keywords, categories[], student, female, disabled, restart, include_closed}` | 맞춤 매칭·자격 판정 |
| GET | `/api/announcements?q=&state=open\|upcoming\|closed\|all&category=&topic=&region=` | 공고 목록 |
| GET | `/api/announcements/{id}?<프로필>` | 공고 상세 + 정책 배경(+ 프로필을 주면 자격 판정) |
| GET | `/api/radar`, `/api/radar/{주제}` | 주제별 관심도·공급·판정, 약속 추적·질의·공고 |
| GET | `/api/meta`, `/api/health` | 현황·선택지·진단 |
| POST | `/api/admin/refresh?full=false` | K-Startup에서 공고 새로 받기 |
| GET | `/api/kstartup/{오퍼레이션}` | 자동 수집용 중계(서울 리전) |

## 한계

- 자격 판정은 공고의 정형 필드와 문구를 규칙으로 해석한 **참고 결과**입니다. 신청 전 공고문 원문을 반드시 확인하세요.
  판정할 정보가 부족하면 「조건부」로, 제외 대상은 「참고」로 표시합니다.
- 지원금액은 공고 본문의 '최대·한도·이내 ○○원' 표현에서 뽑은 값으로, 금액이 적히지 않은 공고는 합계에서 빠집니다.
- 정책 레이더의 주제 분류는 키워드 기반입니다. `radar/topics.py`에 키워드를 더하면 다음 갱신 때 반영됩니다.
  '공백·기회'는 공고가 나올 것이라는 예측이 아니라, 국회 관심과 공고 공급의 차이를 보여 주는 신호입니다.
- K-Startup 응답 필드명은 `radar/kstartup.py`의 `ANNOUNCEMENT_FIELDS`에서 별칭까지 흡수합니다. 실제 응답이 다르면 여기에 추가하세요.

## 테스트

```bash
python -m pytest -q    # support-radar 폴더 또는 저장소 루트에서
```
