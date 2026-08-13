#!/usr/bin/env bash
set -Eeuo pipefail

[[ $EUID -eq 0 ]] || {
  printf 'deep space diagnostics must run through sudo\n' >&2
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

printf '\n== filesystem blocks and inodes ==\n'
df -hT /
df -ih /

printf '\n== root directories by allocated bytes ==\n'
du -x -B1 --max-depth=1 / 2>/dev/null | sort -n

printf '\n== /var directories by allocated bytes ==\n'
du -x -B1 --max-depth=2 /var 2>/dev/null | sort -n | tail -40

printf '\n== /home directories by allocated bytes ==\n'
du -x -B1 --max-depth=3 /home 2>/dev/null | sort -n | tail -50

printf '\n== deploy user directories by allocated bytes ==\n'
du -x -B1 --max-depth=3 "$deploy_home" 2>/dev/null | sort -n | tail -60

printf '\n== Docker storage directories by allocated bytes ==\n'
docker_root=$(docker info --format '{{.DockerRootDir}}')
case "$docker_root" in
  /*) ;;
  *) printf 'unsafe Docker root path\n' >&2; exit 1 ;;
esac
du -x -B1 --max-depth=2 "$docker_root" 2>/dev/null | sort -n | tail -50

printf '\n== Docker detailed disk use ==\n'
docker system df -v

printf '\n== system journal use ==\n'
journalctl --disk-usage

printf '\n== large files on root filesystem (100 MiB+) ==\n'
find / -xdev -type f -size +100M -printf '%s\t%p\n' 2>/dev/null \
  | sort -nr \
  | head -50

printf '\n== Ollum release and incoming payload sizes ==\n'
du -x -B1 --max-depth=2 "$deploy_home/ollum-sales" 2>/dev/null | sort -n | tail -40
if [[ -d $deploy_home/.ollum-sales-incoming ]]; then
  du -x -B1 --max-depth=2 "$deploy_home/.ollum-sales-incoming" 2>/dev/null \
    | sort -n \
    | tail -40
fi
