#!/usr/bin/env bash
set -Eeuo pipefail

die() {
  printf 'WhatsApp persistence verification error: %s\n' "$1" >&2
  exit 1
}

[[ $EUID -eq 0 ]] || die 'verification must run through sudo'
[[ $# -eq 1 ]] || die 'expected deploy user'

deploy_user=$1
[[ $deploy_user =~ ^[a-z_][a-z0-9_-]*$ ]] || die 'invalid deploy user'
deploy_home=$(getent passwd "$deploy_user" | cut -d: -f6)
[[ -n $deploy_home && -d $deploy_home ]] || die 'deploy user home was not found'

deploy_root="$deploy_home/ollum-sales"
current_release=$(readlink -f "$deploy_root/current")
case "$current_release" in
  "$deploy_root"/releases/*) ;;
  *) die 'current release is unavailable or points outside the release directory' ;;
esac
[[ -f $current_release/docker-compose.yml ]] || die 'current release has no docker-compose.yml'

send_flag=$(grep -E '^OLLUM_ALLOW_WHATSAPP_SEND=' "$deploy_root/shared/.env" | tail -1 | cut -d= -f2- || true)
[[ $send_flag == false ]] || die 'OLLUM_ALLOW_WHATSAPP_SEND must remain false'

cd "$current_release"
docker compose config --quiet

bridge_status() {
  docker compose exec -T ollum-sales-mcp python - <<'PY'
import json
import urllib.request

with urllib.request.urlopen(
    "http://whatsapp-bridge:8080/api/status", timeout=5
) as response:
    print(json.dumps(json.load(response), separators=(",", ":")))
PY
}

status_identity() {
  python3 -c '
import json
import sys

payload = json.load(sys.stdin)
if not (
    payload.get("ready") is True
    and payload.get("connected") is True
    and payload.get("logged_in") is True
    and payload.get("send_enabled") is False
):
    raise SystemExit(1)
jid = payload.get("account_jid")
if not isinstance(jid, str) or not jid:
    raise SystemExit(1)
print(jid)
'
}

wait_for_authenticated_identity() {
  local timeout_seconds=$1
  local deadline=$((SECONDS + timeout_seconds))
  local payload
  local identity

  while (( SECONDS < deadline )); do
    if payload=$(bridge_status 2>/dev/null) \
      && identity=$(status_identity <<<"$payload" 2>/dev/null); then
      printf '%s\n' "$identity"
      return 0
    fi
    sleep 2
  done
  return 1
}

container_volume() {
  docker inspect --format \
    '{{range .Mounts}}{{if eq .Destination "/app/store"}}{{.Name}}{{end}}{{end}}' \
    "$1"
}

before_container=$(docker compose ps -q whatsapp-bridge)
[[ -n $before_container ]] || die 'whatsapp-bridge container is missing before restart'
before_volume=$(container_volume "$before_container")
[[ -n $before_volume ]] || die 'persistent WhatsApp volume is missing before restart'
before_identity=$(wait_for_authenticated_identity 15) \
  || die 'bridge is not authenticated before restart'

printf 'Recreating only whatsapp-bridge with its existing persistent volume.\n'
docker compose up -d --no-deps --force-recreate --no-build whatsapp-bridge

after_identity=$(wait_for_authenticated_identity 120) || {
  docker compose logs --tail=120 whatsapp-bridge >&2 || true
  die 'bridge did not reconnect with the persisted session'
}

after_container=$(docker compose ps -q whatsapp-bridge)
[[ -n $after_container ]] || die 'whatsapp-bridge container is missing after restart'
[[ $after_container != "$before_container" ]] || die 'whatsapp-bridge was not recreated'
after_volume=$(container_volume "$after_container")
[[ $after_volume == "$before_volume" ]] || die 'persistent WhatsApp volume changed during restart'
[[ $after_identity == "$before_identity" ]] || die 'authenticated WhatsApp identity changed during restart'

restart_policy=$(docker inspect --format '{{.HostConfig.RestartPolicy.Name}}' "$after_container")
[[ $restart_policy == unless-stopped ]] || die 'whatsapp-bridge restart policy is not unless-stopped'

send_flag=$(grep -E '^OLLUM_ALLOW_WHATSAPP_SEND=' "$deploy_root/shared/.env" | tail -1 | cut -d= -f2- || true)
[[ $send_flag == false ]] || die 'OLLUM_ALLOW_WHATSAPP_SEND changed during verification'

printf 'WHATSAPP_PERSISTENCE=ready identity_unchanged=true volume_unchanged=true restart_policy=%s send_enabled=false\n' \
  "$restart_policy"
