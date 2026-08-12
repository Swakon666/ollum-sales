#!/usr/bin/env bash
set -Eeuo pipefail

die() {
  printf 'rollback error: %s\n' "$1" >&2
  exit 1
}

[[ $EUID -eq 0 ]] || die 'remote_rollback.sh must run through sudo'
[[ $# -eq 3 ]] || die 'expected deploy user, domain, and bind port'

deploy_user=$1
domain=$2
bind_port=$3
deploy_home=$(getent passwd "$deploy_user" | cut -d: -f6)
deploy_root="$deploy_home/ollum-sales"

[[ -L $deploy_root/previous ]] || die 'no previous release is recorded'
rollback_release=$(readlink -f "$deploy_root/previous")
current_release=$(readlink -f "$deploy_root/current")
case "$rollback_release" in
  "$deploy_root"/releases/*) ;;
  *) die 'previous symlink points outside the release directory' ;;
esac
[[ -f $rollback_release/docker-compose.yml ]] || die 'previous release is incomplete'

cd "$rollback_release"
docker compose config --quiet
docker compose build
docker compose up -d --remove-orphans

for _attempt in $(seq 1 60); do
  if curl -fsS --max-time 5 "http://127.0.0.1:$bind_port/health" >/dev/null; then
    ln -sfn "$rollback_release" "$deploy_root/current"
    ln -sfn "$current_release" "$deploy_root/previous"
    curl -fsS --max-time 20 "https://$domain/health" >/dev/null
    printf 'Rollback completed: release=%s\n' "$(basename "$rollback_release")"
    exit 0
  fi
  sleep 5
done

docker compose ps >&2 || true
docker compose logs --tail=100 ollum-sales-mcp >&2 || true
die 'rollback health check failed'
