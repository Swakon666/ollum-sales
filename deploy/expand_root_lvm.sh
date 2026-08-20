#!/usr/bin/env bash
set -Eeuo pipefail

[[ $EUID -eq 0 ]] || {
  printf 'root LVM expansion must run through sudo\n' >&2
  exit 1
}
[[ $# -eq 0 ]] || {
  printf 'root LVM expansion accepts no arguments\n' >&2
  exit 1
}

disk=/dev/sda
partition_number=3
pv=/dev/sda3
vg=ubuntu-vg
lv=ubuntu-lv
lv_path=/dev/ubuntu-vg/ubuntu-lv

for command_name in blockdev df findmnt growpart lsblk lvs pvs pvresize readlink vgs; do
  command -v "$command_name" >/dev/null || {
    printf 'required command is missing: %s\n' "$command_name" >&2
    exit 1
  }
done

[[ $(lsblk -dn -o TYPE "$disk") == disk ]] || {
  printf 'expected %s to be a disk\n' "$disk" >&2
  exit 1
}
[[ $(lsblk -dn -o TYPE "$pv") == part ]] || {
  printf 'expected %s to be a partition\n' "$pv" >&2
  exit 1
}
[[ $(lsblk -dn -o PKNAME "$pv") == sda ]] || {
  printf 'expected %s to belong to %s\n' "$pv" "$disk" >&2
  exit 1
}
[[ $(findmnt -n -o FSTYPE /) == ext4 ]] || {
  printf 'root filesystem is not ext4\n' >&2
  exit 1
}
[[ $(readlink -f "$(findmnt -n -o SOURCE /)") == $(readlink -f "$lv_path") ]] || {
  printf 'root filesystem is not mounted from %s\n' "$lv_path" >&2
  exit 1
}
[[ $(pvs --noheadings -o vg_name "$pv" | xargs) == "$vg" ]] || {
  printf '%s is not the physical volume for %s\n' "$pv" "$vg" >&2
  exit 1
}
[[ $(lvs --noheadings -o lv_name "$vg/$lv" | xargs) == "$lv" ]] || {
  printf 'logical volume %s/%s was not found\n' "$vg" "$lv" >&2
  exit 1
}

disk_bytes=$(blockdev --getsize64 "$disk")
partition_bytes=$(blockdev --getsize64 "$pv")
minimum_disk_bytes=$((44 * 1024 * 1024 * 1024))
minimum_root_bytes=$((40 * 1024 * 1024 * 1024))

(( disk_bytes >= minimum_disk_bytes )) || {
  printf '%s is smaller than the confirmed 45 GiB disk\n' "$disk" >&2
  exit 1
}

printf 'ROOT_LVM_BEFORE disk_bytes=%s partition_bytes=%s\n' \
  "$disk_bytes" "$partition_bytes"
lsblk -o NAME,TYPE,SIZE,FSTYPE,MOUNTPOINTS "$disk"
pvs --units g --nosuffix -o pv_name,pv_size,pv_free "$pv"
vgs --units g --nosuffix -o vg_name,vg_size,vg_free "$vg"
lvs --units g --nosuffix -o lv_name,vg_name,lv_size "$vg/$lv"
df -hT /

if (( partition_bytes < minimum_root_bytes )); then
  printf 'Validating partition expansion with growpart dry-run\n'
  growpart -N "$disk" "$partition_number"
  printf 'Expanding %s partition %s\n' "$disk" "$partition_number"
  growpart "$disk" "$partition_number"
  command -v partprobe >/dev/null && partprobe "$disk" || true
  command -v udevadm >/dev/null && udevadm settle || true
fi

partition_bytes=$(blockdev --getsize64 "$pv")
(( partition_bytes >= minimum_root_bytes )) || {
  printf '%s did not expand to at least 40 GiB\n' "$pv" >&2
  exit 1
}

pvresize "$pv"
vg_free_bytes=$(vgs --noheadings --units b --nosuffix -o vg_free "$vg" \
  | tr -d ' ' \
  | cut -d. -f1)
if (( vg_free_bytes > 16 * 1024 * 1024 )); then
  lvextend -l +100%FREE -r "$lv_path"
fi

lv_bytes=$(lvs --noheadings --units b --nosuffix -o lv_size "$vg/$lv" \
  | tr -d ' ' \
  | cut -d. -f1)
filesystem_bytes=$(( $(df --output=size / | tail -1 | xargs) * 1024 ))
(( lv_bytes >= minimum_root_bytes )) || {
  printf '%s did not expand to at least 40 GiB\n' "$lv_path" >&2
  exit 1
}
(( filesystem_bytes >= minimum_root_bytes )) || {
  printf 'root ext4 filesystem did not expand to at least 40 GiB\n' >&2
  exit 1
}

printf 'ROOT_LVM_AFTER disk_bytes=%s partition_bytes=%s lv_bytes=%s filesystem_bytes=%s\n' \
  "$disk_bytes" "$partition_bytes" "$lv_bytes" "$filesystem_bytes"
lsblk -o NAME,TYPE,SIZE,FSTYPE,MOUNTPOINTS "$disk"
pvs --units g --nosuffix -o pv_name,pv_size,pv_free "$pv"
vgs --units g --nosuffix -o vg_name,vg_size,vg_free "$vg"
lvs --units g --nosuffix -o lv_name,vg_name,lv_size "$vg/$lv"
df -hT /
