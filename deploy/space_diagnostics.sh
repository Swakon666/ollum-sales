#!/usr/bin/env bash
set -Eeuo pipefail

[[ $EUID -eq 0 ]] || {
  printf 'space diagnostics must run through sudo\n' >&2
  exit 1
}
[[ $# -eq 1 ]] || {
  printf 'expected deploy user\n' >&2
  exit 1
}

deploy_user=$1
[[ $deploy_user =~ ^[a-z_][a-z0-9_-]*$ ]] || {
  printf 'invalid deploy user\n' >&2
  exit 1
}
deploy_home=$(getent passwd "$deploy_user" | cut -d: -f6)
[[ -n $deploy_home && -d $deploy_home ]] || {
  printf 'deploy user home was not found\n' >&2
  exit 1
}
deploy_root="$deploy_home/ollum-sales"

printf '\n== filesystem ==\n'
df -hT /

printf '\n== Docker summary ==\n'
docker system df

printf '\n== Ollum containers ==\n'
docker ps -a --filter 'name=ollum-sales-' \
  --format 'table {{.ID}}\t{{.Names}}\t{{.Image}}\t{{.Status}}'

printf '\n== Ollum tagged images ==\n'
docker image ls --format '{{.Repository}}\t{{.Tag}}\t{{.ID}}\t{{.Size}}' \
  | awk '$1 ~ /^ollum-sales-/ {print}'

printf '\n== Ollum releases ==\n'
if [[ -d $deploy_root/releases ]]; then
  find "$deploy_root/releases" -mindepth 1 -maxdepth 1 -type d -print0 \
    | sort -z \
    | xargs -0r du -sh
fi

printf '\n== BuildKit cache records ==\n'
docker buildx du --verbose
