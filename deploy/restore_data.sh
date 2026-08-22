#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

die() {
  printf 'Restore failed: %s\n' "$*" >&2
  exit 1
}

[[ ${OLLUM_RESTORE_CONFIRM:-} == RESTORE ]] \
  || die 'set OLLUM_RESTORE_CONFIRM=RESTORE for this destructive operation'
[[ $# -eq 1 ]] || die 'usage: restore_data.sh /absolute/path/to/backup.tar.gz.enc'

for command_name in docker grep openssl sha256sum tar; do
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

encrypted_archive=$1
[[ $encrypted_archive == /* && -r $encrypted_archive ]] \
  || die 'backup archive must be an absolute path to a readable file'
[[ -r "$encrypted_archive.sha256" ]] || die 'backup SHA-256 sidecar is missing'
(
  cd -- "$(dirname -- "$encrypted_archive")"
  sha256sum --check --strict "$(basename -- "$encrypted_archive.sha256")"
)

passphrase_file=${OLLUM_BACKUP_PASSPHRASE_FILE:-}
[[ -n $passphrase_file && -r $passphrase_file && -s $passphrase_file ]] \
  || die 'OLLUM_BACKUP_PASSPHRASE_FILE must point to a non-empty readable secret file'
openssl_passphrase_file=$(docker_bind_path "$passphrase_file")

stage_root=$(mktemp -d "${TMPDIR:-/tmp}/ollum-sales-restore.XXXXXXXX")
plain_archive="$stage_root/backup.tar.gz"
payload_dir="$stage_root/payload"
mkdir -p -- "$payload_dir"
payload_mount=$(docker_bind_path "$payload_dir")
temporary_crm_volume="ollum-sales-restore-crm-$$"
temporary_whatsapp_volume="ollum-sales-restore-whatsapp-$$"
stack_stopped=false
production_mutated=false

running_services=()
while IFS= read -r service_name; do
  [[ -n $service_name ]] && running_services+=("$service_name")
done < <(docker compose ps --services --filter status=running)

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
    if [[ $production_mutated == true && $status -ne 0 ]]; then
      printf '%s\n' \
        'Restore stopped after production data changed; services remain stopped. Use the encrypted pre-restore backup for recovery.' >&2
    else
      resume_stack || status=1
    fi
  fi
  docker volume rm "$temporary_crm_volume" "$temporary_whatsapp_volume" >/dev/null 2>&1 || true
  case "$stage_root" in
    "${TMPDIR:-/tmp}"/ollum-sales-restore.*) rm -rf -- "$stage_root" ;;
    *) printf 'Refusing to remove unexpected temporary path: %s\n' "$stage_root" >&2; status=1 ;;
  esac
  exit "$status"
}
trap cleanup EXIT INT TERM

openssl enc -d -aes-256-cbc -pbkdf2 -iter 600000 \
  -in "$encrypted_archive" \
  -out "$plain_archive" \
  -pass "file:$openssl_passphrase_file"

while IFS= read -r member; do
  case "$member" in
    MANIFEST|SHA256SUMS|crm-data.tar.gz|whatsapp-data.tar.gz) ;;
    *) die "backup contains an unexpected member: $member" ;;
  esac
done < <(tar -tzf "$plain_archive")
tar -C "$payload_dir" -xzf "$plain_archive"
grep -qx 'format=ollum-sales-backup-v1' "$payload_dir/MANIFEST" \
  || die 'unsupported backup format'
(
  cd "$payload_dir"
  sha256sum --check --strict SHA256SUMS
)

runtime_image=$(docker compose config --images | grep 'ollum-sales-mcp' | head -1)
[[ -n $runtime_image ]] || die 'could not identify the local MCP image'
docker image inspect "$runtime_image" >/dev/null 2>&1 \
  || die "runtime image is unavailable: $runtime_image"

pre_restore_dir=${OLLUM_PRE_RESTORE_BACKUP_DIR:-$(dirname -- "$encrypted_archive")/pre-restore}
mkdir -p -- "$pre_restore_dir"
OLLUM_BACKUP_DIR="$pre_restore_dir" "$script_dir/backup_data.sh" "$pre_restore_dir"

docker volume create "$temporary_crm_volume" >/dev/null
docker volume create "$temporary_whatsapp_volume" >/dev/null

restore_archive_to_volume() {
  local archive=$1
  local volume=$2
  docker_run --rm --user 0:0 --entrypoint /bin/sh \
    --volume "$volume":/restore \
    --volume "$payload_mount":/backup:ro \
    "$runtime_image" -ec \
    "tar -xzf /backup/$archive -C /restore && chown -R 10001:10001 /restore"
}

restore_archive_to_volume crm-data.tar.gz "$temporary_crm_volume"
restore_archive_to_volume whatsapp-data.tar.gz "$temporary_whatsapp_volume"

docker_run --rm --user 10001:10001 --entrypoint python \
  --volume "$temporary_crm_volume":/data/crm \
  --volume "$temporary_whatsapp_volume":/data/whatsapp \
  "$runtime_image" -c $'import pathlib, sqlite3\npaths = list(pathlib.Path("/data/crm").glob("*.db")) + list(pathlib.Path("/data/whatsapp").glob("*.db"))\nassert paths, "backup contains no SQLite databases"\nfor path in paths:\n    connection = sqlite3.connect(path)\n    result = connection.execute("PRAGMA integrity_check").fetchone()[0]\n    connection.close()\n    assert result == "ok", f"integrity check failed for {path}: {result}"'

if [[ ${#running_services[@]} -gt 0 ]]; then
  docker compose stop --timeout 60 "${running_services[@]}" >/dev/null
  stack_stopped=true
fi

copy_volume() {
  local source_volume=$1
  local destination_volume=$2
  docker_run --rm --user 0:0 --entrypoint /bin/sh \
    --volume "$source_volume":/source:ro \
    --volume "$destination_volume":/destination \
    "$runtime_image" -ec \
    'find /destination -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +; tar -C /source -cf - . | tar -C /destination -xf -; chown -R 10001:10001 /destination'
}

production_mutated=true
copy_volume "$temporary_crm_volume" ollum-sales-crm-data
copy_volume "$temporary_whatsapp_volume" ollum-sales-whatsapp-data
resume_stack

printf 'Restore completed and prior service state restored from: %s\n' "$encrypted_archive"
