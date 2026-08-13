#!/usr/bin/env bash
set -euo pipefail

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

[[ $EUID -eq 0 ]] || die 'connect_whatsapp.sh must run through sudo'
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

cd "$current_release"
docker compose config --quiet

send_flag=$(grep -E '^OLLUM_ALLOW_WHATSAPP_SEND=' "$deploy_root/shared/.env" | tail -1 | cut -d= -f2- || true)
[[ $send_flag == false ]] || die 'OLLUM_ALLOW_WHATSAPP_SEND must remain false during pairing'

printf 'Restarting only whatsapp-bridge; its persistent Docker volume is preserved.\n'
docker compose up -d --no-deps --force-recreate --no-build whatsapp-bridge

printf '\nOpen WhatsApp on the phone: Settings -> Linked devices -> Link a device.\n'
printf 'Scan the newest QR code below. Fresh QR batches rotate for up to ten minutes.\n\n'

set +e
timeout --signal=INT --kill-after=10s 660s \
  docker compose logs --no-color --follow --since=30s whatsapp-bridge
log_status=$?
set -e

if [[ $log_status -ne 0 && $log_status -ne 124 && $log_status -ne 130 ]]; then
  die "WhatsApp bridge log stream failed with status $log_status"
fi

container_id=$(docker compose ps -q whatsapp-bridge)
[[ -n $container_id ]] || die 'whatsapp-bridge container is not running'

container_status=$(docker inspect --format '{{.State.Status}}' "$container_id")
[[ $container_status == running ]] || die "whatsapp-bridge is not running: $container_status"

printf '\nPairing window closed. whatsapp-bridge is still running and send remains disabled.\n'
