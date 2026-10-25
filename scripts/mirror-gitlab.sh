#!/usr/bin/env bash
# 기관망 서버용 GitLab 미러: GitHub 원본의 main·governance-log 를 기관 GitLab 으로 밀어 넣는다.
# GitHub 호스트 러너는 gitlab.aigov.go.kr 에 접속하지 못하므로, 두 곳 모두 접속되는 기관망 서버에서 cron 으로 돌린다.
#   GITLAB_TOKEN=... scripts/mirror-gitlab.sh
#   */30 * * * * GITLAB_TOKEN=... /path/to/mirror-gitlab.sh >> /var/log/ax-mirror.log 2>&1
# 원본 저장소가 비공개면 GITHUB_TOKEN(contents 읽기)도 지정한다.
set -euo pipefail

SRC=${SRC:-https://github.com/kimjs3176-prog/ax-.git}
DST=${DST:-https://gitlab.aigov.go.kr/kimjs1408/kookgam-ax.git}
DIR=${MIRROR_DIR:-$HOME/.cache/ax-mirror.git}
BRANCHES=${BRANCHES:-"main governance-log"}
: "${GITLAB_TOKEN:?GITLAB_TOKEN(write_repository)이 필요합니다}"

# 토큰을 주소에 넣지 않고 헤더로 넘겨 프로세스 목록·원격 설정에 남지 않게 한다
auth() { printf 'Authorization: Basic %s' "$(printf '%s:%s' "$1" "$2" | base64 | tr -d '\n')"; }
src_auth=()
[ -n "${GITHUB_TOKEN:-}" ] && src_auth=(-c "http.https://github.com/.extraHeader=$(auth x-access-token "$GITHUB_TOKEN")")
dst_auth=(-c "http.extraHeader=$(auth oauth2 "$GITLAB_TOKEN")")

[ -d "$DIR" ] || git init -q --bare "$DIR"
git -C "$DIR" "${src_auth[@]}" fetch -q --prune "$SRC" '+refs/heads/*:refs/heads/*'
for b in $BRANCHES; do
  git -C "$DIR" rev-parse -q --verify "refs/heads/$b" >/dev/null || continue
  git -C "$DIR" "${dst_auth[@]}" push -q "$DST" "refs/heads/$b:refs/heads/$b"
  echo "$(date '+%F %T') $b → $(git -C "$DIR" rev-parse --short "refs/heads/$b")"
done
