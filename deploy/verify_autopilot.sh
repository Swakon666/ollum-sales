#!/usr/bin/env bash
set -Eeuo pipefail

die() {
  printf 'Autopilot verification error: %s\n' "$1" >&2
  exit 1
}

[[ $EUID -eq 0 ]] || die 'verification must run through sudo'
[[ $# -eq 1 ]] || die 'expected deploy user'

deploy_user=$1
[[ $deploy_user =~ ^[a-z_][a-z0-9_-]*$ ]] || die 'invalid deploy user'
deploy_home=$(getent passwd "$deploy_user" | cut -d: -f6)
deploy_root="$deploy_home/ollum-sales"
current_release=$(readlink -f "$deploy_root/current")
case "$current_release" in
  "$deploy_root"/releases/*) ;;
  *) die 'current release is missing or unsafe' ;;
esac
[[ -f $current_release/docker-compose.yml ]] || die 'current release is incomplete'

cd "$current_release"
cleanup() {
  docker compose start ollum-sales-worker >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker compose stop -t 30 ollum-sales-worker >/dev/null
run_output=$(docker compose exec -T ollum-sales-mcp python -m app.production_check run)
cycle_id=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["cycle_id"])' <<<"$run_output")
[[ $cycle_id =~ ^[0-9a-f-]{36}$ ]] || die 'verification returned an invalid cycle id'
printf 'AUTOPILOT_SAFE_CYCLE=%s\n' "$run_output"

docker compose start ollum-sales-worker >/dev/null
sleep 5
worker_container=$(docker compose ps -q ollum-sales-worker)
[[ -n $worker_container ]] || die 'worker container is missing'
[[ $(docker inspect -f '{{.State.Running}}' "$worker_container") == true ]] \
  || die 'worker is not running'
[[ $(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$worker_container") == unless-stopped ]] \
  || die 'worker restart policy is not unless-stopped'

docker compose restart -t 30 ollum-sales-worker >/dev/null
sleep 5
worker_container=$(docker compose ps -q ollum-sales-worker)
[[ $(docker inspect -f '{{.State.Running}}' "$worker_container") == true ]] \
  || die 'worker did not return after restart'
persisted_output=$(
  docker compose exec -T ollum-sales-mcp \
    python -m app.production_check status "$cycle_id"
)
printf 'AUTOPILOT_PERSISTENCE=%s\n' "$persisted_output"
printf 'AUTOPILOT_WORKER=%s\n' "$(docker inspect -f \
  'running={{.State.Running}} restart={{.HostConfig.RestartPolicy.Name}} started_at={{.State.StartedAt}}' \
  "$worker_container")"

trap - EXIT
