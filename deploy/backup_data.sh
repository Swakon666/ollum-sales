#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

die() {
  printf 'Backup failed: %s\n' "$*" >&2
  exit 1
}

for command_name in docker openssl sha256sum tar; do
  command -v "$command_name" >/dev/null || die "required command is missing: $command_name"
done

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
project_dir=$(cd -- "$script_dir/.." && pwd)
cd "$project_dir"

docker_run() {
  if [[ -n ${MSYSTEM:-} ]] && command -v cygpath >/dev/null 2>&1; then
    MSYS2_ARG_CONV_EXCL='*' docker run "$@"
  else
    docker run "$@"
  fi
}

docker_bind_path() {
  if [[ -n ${MSYSTEM:-} ]] && command -v cygpath >/dev/null 2>&1; then
    cygpath -m "$1"
  else
    printf '%s\n' "$1"
  fi
}

passphrase_file=${OLLUM_BACKUP_PASSPHRASE_FILE:-}
[[ -n $passphrase_file && -r $passphrase_file ]] \
  || die 'OLLUM_BACKUP_PASSPHRASE_FILE must point to a readable secret file'
[[ -s $passphrase_file ]] || die 'backup passphrase file is empty'
openssl_passphrase_file=$(docker_bind_path "$passphrase_file")

backup_dir=${1:-${OLLUM_BACKUP_DIR:-/var/backups/ollum-sales}}
mkdir -p -- "$backup_dir"
backup_dir=$(cd -- "$backup_dir" && pwd)

timestamp=$(date -u +%Y%m%dT%H%M%SZ)
archive_name="ollum-sales-$timestamp.tar.gz.enc"
final_archive="$backup_dir/$archive_name"
temporary_archive="$backup_dir/.$archive_name.tmp.$$"
[[ ! -e $final_archive && ! -e $temporary_archive ]] || die 'backup archive already exists'

stage_root=$(mktemp -d "${TMPDIR:-/tmp}/ollum-sales-backup.XXXXXXXX")
payload_dir="$stage_root/payload"
plain_archive="$stage_root/backup.tar.gz"
mkdir -p -- "$payload_dir"
payload_mount=$(docker_bind_path "$payload_dir")

running_services=()
while IFS= read -r service_name; do
  [[ -n $service_name ]] && running_services+=("$service_name")
done < <(docker compose ps --services --filter status=running)
stack_stopped=false

resume_stack() {
  if [[ $stack_stopped == true && ${#running_services[@]} -gt 0 ]]; then
    docker compose up -d "${running_services[@]}" >/dev/null
    stack_stopped=false
  fi
}

cleanup() {
  local status=$?
  trap - EXIT INT TERM
  if [[ $stack_stopped == true ]]; then
    resume_stack || status=1
  fi
  rm -f -- "$temporary_archive"
  case "$stage_root" in
    "${TMPDIR:-/tmp}"/ollum-sales-backup.*) rm -rf -- "$stage_root" ;;
    *) printf 'Refusing to remove unexpected temporary path: %s\n' "$stage_root" >&2; status=1 ;;
  esac
  exit "$status"
}
trap cleanup EXIT INT TERM

runtime_image=$(docker compose config --images | grep 'ollum-sales-mcp' | head -1)
[[ -n $runtime_image ]] || die 'could not identify the local MCP image'
docker image inspect "$runtime_image" >/dev/null 2>&1 \
  || die "runtime image is unavailable: $runtime_image"
for volume in ollum-sales-crm-data ollum-sales-whatsapp-data; do
  docker volume inspect "$volume" >/dev/null 2>&1 \
    || die "required persistent volume is unavailable: $volume"
done

if [[ ${#running_services[@]} -gt 0 ]]; then
  docker compose stop --timeout 60 "${running_services[@]}" >/dev/null
  stack_stopped=true
fi

docker_run --rm --user 0:0 --entrypoint /bin/tar \
  --volume ollum-sales-crm-data:/source:ro \
  --volume "$payload_mount":/backup \
  "$runtime_image" -C /source -czf /backup/crm-data.tar.gz .
docker_run --rm --user 0:0 --entrypoint /bin/tar \
  --volume ollum-sales-whatsapp-data:/source:ro \
  --volume "$payload_mount":/backup \
  "$runtime_image" -C /source -czf /backup/whatsapp-data.tar.gz .

resume_stack

(
  cd "$payload_dir"
  sha256sum crm-data.tar.gz whatsapp-data.tar.gz > SHA256SUMS
)
cat > "$payload_dir/MANIFEST" <<EOF
format=ollum-sales-backup-v1
created_at=$timestamp
git_commit=$(git rev-parse HEAD 2>/dev/null || printf 'unknown')
crm_volume=ollum-sales-crm-data
whatsapp_volume=ollum-sales-whatsapp-data
runtime_uid=10001
EOF

tar -C "$payload_dir" -czf "$plain_archive" \
  MANIFEST SHA256SUMS crm-data.tar.gz whatsapp-data.tar.gz
openssl enc -aes-256-cbc -salt -pbkdf2 -iter 600000 \
  -in "$plain_archive" \
  -out "$temporary_archive" \
  -pass "file:$openssl_passphrase_file"
chmod 0600 "$temporary_archive"
mv -- "$temporary_archive" "$final_archive"
(
  cd "$backup_dir"
  sha256sum "$archive_name" > "$archive_name.sha256"
)
chmod 0600 "$final_archive.sha256"

printf 'Encrypted backup created: %s\n' "$final_archive"
