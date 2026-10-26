# GitLab 미러(Windows): GitHub 원본의 main·governance-log 를 기관 GitLab 으로 밀어 넣는다.
# GitHub 호스트 러너(해외)는 gitlab.aigov.go.kr 에 접속하지 못하므로, 기관망에 붙은 국내 PC에서 돌린다.
#
#   1) 토큰을 사용자 환경변수로 한 번 저장(창을 새로 열어야 반영):
#        [Environment]::SetEnvironmentVariable("GITLAB_TOKEN", "<새로 발급한 토큰>", "User")
#   2) 실행:  powershell -ExecutionPolicy Bypass -File scripts\mirror-gitlab.ps1
#   3) 작업 스케줄러에 30분마다 등록(docs/gitlab-mirror.md 참고)
# 필요: Git for Windows(git 명령)
$ErrorActionPreference = "Stop"

$Src = if ($env:SRC) { $env:SRC } else { "https://github.com/kimjs3176-prog/ax-.git" }
$Dst = if ($env:DST) { $env:DST } else { "https://gitlab.aigov.go.kr/kimjs1408/kookgam-ax.git" }
$Dir = if ($env:MIRROR_DIR) { $env:MIRROR_DIR } else { Join-Path $env:LOCALAPPDATA "ax-mirror.git" }
$Branches = if ($env:BRANCHES) { $env:BRANCHES -split " " } else { @("main", "governance-log") }
$Token = $env:GITLAB_TOKEN
if (-not $Token) { $Token = [Environment]::GetEnvironmentVariable("GITLAB_TOKEN", "User") }
if (-not $Token) { throw "GITLAB_TOKEN(write_repository) 환경변수가 필요합니다." }

# 토큰을 주소에 넣지 않고 헤더로 넘긴다(원격 설정·로그에 남지 않게)
$basic = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("oauth2:$Token"))
$auth = "http.extraHeader=Authorization: Basic $basic"

if (-not (Test-Path $Dir)) { git init -q --bare $Dir | Out-Null }
git -C $Dir fetch -q --prune $Src "+refs/heads/*:refs/heads/*"
if ($LASTEXITCODE -ne 0) { throw "GitHub에서 가져오기 실패" }

foreach ($b in $Branches) {
  git -C $Dir rev-parse -q --verify "refs/heads/$b" *> $null
  if ($LASTEXITCODE -ne 0) { continue }
  git -C $Dir -c $auth push -q $Dst "refs/heads/${b}:refs/heads/$b"
  if ($LASTEXITCODE -ne 0) { throw "GitLab 푸시 실패: $b" }
  $sha = git -C $Dir rev-parse --short "refs/heads/$b"
  Write-Output ("{0} {1} -> {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $b, $sha)
}
