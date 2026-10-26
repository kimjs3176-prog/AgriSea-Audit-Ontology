# 기관 GitLab 자동 미러 설정

GitHub(`kimjs3176-prog/AgriSea-Audit-Ontology`)가 원본이고, 기관 GitLab(`https://gitlab.aigov.go.kr/kimjs1408/kookgam-ax.git`)에
`main`·`governance-log` 브랜치를 그대로 복사합니다. 자동 수집 데이터 커밋도 함께 넘어갑니다.

> **왜 GitHub Actions만으로는 안 되나**: 기관 GitLab은 GitHub 호스트 러너(해외 서버)에서 접속되지 않습니다
> (443 연결 시간 초과). 그래서 **기관 GitLab에 접속되는 국내 PC·서버**가 하나 필요합니다.
> 아래 방법 중 하나를 고르세요.

## 0. 토큰 준비(공통)

1. GitLab → 프로젝트 `kookgam-ax` → **Settings → Access Tokens**(또는 사용자 **Preferences → Access Tokens**)
2. 권한 **`write_repository`** 만 체크, 만료일 지정 → 발급
3. 토큰은 **GitHub Secrets·PC 환경변수에만** 넣고, 채팅·메일·코드·커밋에 붙여 넣지 않습니다.
   노출된 토큰은 즉시 **Revoke** 후 새로 발급하세요.
4. `main` 이 GitLab에서 보호 브랜치면, 토큰 사용자에게 **Allowed to push** 권한이 있어야 합니다.
   GitLab 쪽에서 직접 커밋하면 미러가 거부되니(이력 불일치) GitLab은 읽기 전용으로 쓰세요.

## 방법 A. 국내 PC를 GitHub self-hosted 러너로(권장: GitHub 푸시 즉시 반영)

1. GitHub 저장소 → **Settings → Actions → Runners → New self-hosted runner**
   → PC 운영체제(Windows/Linux)를 고르고 안내된 명령으로 설치·등록(서비스로 실행 권장).
   러너 라벨에 예: `kookgam` 추가.
2. **Settings → Secrets and variables → Actions**
   - Secrets: `GITLAB_TOKEN` = 0단계 토큰
   - Variables: `MIRROR_RUNNER` = `kookgam`(러너 라벨)
3. **Actions → GitLab 미러 → Run workflow**로 한 번 실행해 확인합니다.
   이후 `main` 푸시와 자동 수집 커밋마다 자동으로 미러됩니다.

`MIRROR_RUNNER`가 없으면 워크플로는 실행되지 않고 건너뜁니다(실패 표시가 쌓이지 않게).
이 PC는 회의록 자동 수집(국회 API 해외 차단)에도 같은 방식으로 쓸 수 있습니다.

## 방법 B. 국내 PC에서 스크립트를 주기 실행(러너 설치 없이)

**Windows** (Git for Windows 필요)

```powershell
# 1) 토큰을 사용자 환경변수로 저장(한 번만)
[Environment]::SetEnvironmentVariable("GITLAB_TOKEN", "<새 토큰>", "User")
# 2) 시험 실행(새 PowerShell 창에서)
powershell -ExecutionPolicy Bypass -File C:\path\to\ax-\scripts\mirror-gitlab.ps1
# 3) 30분마다 작업 스케줄러 등록
schtasks /Create /TN "kookgam-ax GitLab 미러" /SC MINUTE /MO 30 /TR "powershell -ExecutionPolicy Bypass -WindowStyle Hidden -File C:\path\to\ax-\scripts\mirror-gitlab.ps1"
```

**Linux / macOS**

```bash
chmod +x scripts/mirror-gitlab.sh
GITLAB_TOKEN=<새 토큰> scripts/mirror-gitlab.sh          # 시험 실행
crontab -e   # 30분마다
*/30 * * * * GITLAB_TOKEN=<새 토큰> /path/to/ax-/scripts/mirror-gitlab.sh >> $HOME/ax-mirror.log 2>&1
```

스크립트는 GitHub에서 가져와 GitLab으로 밀어 넣기만 하므로 저장소를 따로 체크아웃할 필요가 없습니다
(작업 폴더: Windows `%LOCALAPPDATA%\ax-mirror.git`, Linux `~/.cache/ax-mirror.git`).

## 문제 해결

| 증상 | 원인·조치 |
|---|---|
| `접속할 수 없습니다` / 연결 시간 초과 | 그 PC가 기관망·VPN에 연결돼 있는지 확인 |
| `HTTP Basic: Access denied` | 토큰 만료·권한(`write_repository`) 확인, 새 토큰으로 교체 |
| `pre-receive hook declined` / `protected branch` | GitLab 보호 브랜치 푸시 권한, 파일 크기 제한(데이터 파일 약 7~9MB) 확인 |
| `rejected (non-fast-forward)` | GitLab 쪽에 직접 커밋됨 → GitLab 브랜치를 GitHub 기준으로 되돌린 뒤 재실행 |
