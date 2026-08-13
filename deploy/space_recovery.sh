#!/usr/bin/env bash
set -Eeuo pipefail

die() {
  printf 'space recovery error: %s\n' "$1" >&2
  exit 1
}

[[ $EUID -eq 0 ]] || die 'space recovery must run through sudo'
[[ $# -eq 3 ]] || die 'expected deploy user, cache record ID, and failed release ID'

deploy_user=$1
cache_record_id=$2
failed_release_id=$3
[[ $deploy_user =~ ^[a-z_][a-z0-9_-]*$ ]] || die 'invalid deploy user'
[[ $cache_record_id =~ ^[a-z0-9]{25}$ ]] || die 'invalid BuildKit cache record ID'
[[ $failed_release_id =~ ^[A-Fa-f0-9-]+$ ]] || die 'invalid failed release ID'

deploy_home=$(getent passwd "$deploy_user" | cut -d: -f6)
[[ -n $deploy_home && -d $deploy_home ]] || die 'deploy user home was not found'
deploy_root="$deploy_home/ollum-sales"
release_dir="$deploy_root/releases/$failed_release_id"
incoming_dir="$deploy_home/.ollum-sales-incoming/$failed_release_id"

cache_details=$(docker buildx du --verbose)
reclaimable=$(awk -v target="$cache_record_id" '
  $1 == "ID:" { selected = ($2 == target) }
  selected && $1 == "Reclaimable:" { print $2; exit }
' <<<"$cache_details")
[[ $reclaimable == true ]] || die 'the selected cache record is absent or not reclaimable'
description=$(awk -v target="$cache_record_id" '
  $1 == "ID:" { selected = ($2 == target) }
  selected && $1 == "Description:" {
    sub(/^[^:]+:[[:space:]]*/, "")
    print
    exit
  }
' <<<"$cache_details")
[[ $description == *'pip install /app/upstream/Scrapegraph-ai'* ]] \
  || die 'the selected record is not the verified Ollum ScrapeGraphAI build cache'

current_release=$(readlink -f "$deploy_root/current" 2>/dev/null || true)
previous_release=$(readlink -f "$deploy_root/previous" 2>/dev/null || true)
if [[ -e $release_dir ]]; then
  resolved_release=$(readlink -f "$release_dir")
  case "$resolved_release" in
    "$deploy_root"/releases/*) ;;
    *) die 'failed release path escaped the Ollum release directory' ;;
  esac
  [[ $resolved_release != "$current_release" ]] \
    || die 'refusing to remove the current release'
  [[ $resolved_release != "$previous_release" ]] \
    || die 'refusing to remove the rollback release'
  rm -rf -- "$resolved_release"
  printf 'Removed failed Ollum release: %s\n' "$failed_release_id"
fi

if [[ -e $incoming_dir ]]; then
  resolved_incoming=$(readlink -f "$incoming_dir")
  case "$resolved_incoming" in
    "$deploy_home"/.ollum-sales-incoming/*) ;;
    *) die 'incoming path escaped the Ollum incoming directory' ;;
  esac
  rm -rf -- "$resolved_incoming"
  printf 'Removed failed Ollum incoming payload: %s\n' "$failed_release_id"
fi

docker buildx prune --force \
  --filter 'description="mount / from exec /bin/sh -c pip install /app/upstream/Scrapegraph-ai"'
if docker buildx du --verbose | awk -v target="$cache_record_id" '
  $1 == "ID:" && $2 == target { found = 1 }
  END { exit(found ? 0 : 1) }
'; then
  die 'the selected BuildKit record still exists after pruning'
fi
printf 'Pruned verified BuildKit record: %s\n' "$cache_record_id"
df -hT /
docker system df
