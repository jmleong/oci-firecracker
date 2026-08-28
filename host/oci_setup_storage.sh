#!/usr/bin/env bash
# oci_setup_storage.sh — OCI-specific pre-step. Put the Firecracker working dir on
# a reflink-capable filesystem so make_overlay's per-VM rootfs clones are free CoW
# copies instead of full 4 GB copies.
#
# WHY: OCI's boot volume is ext4 (no reflink). The density bench makes one overlay
# per microVM (hundreds), so on the boot disk those become full 4 GB copies that
# fill the 45 GB boot volume and wedge the host. AWS didn't hit this because its
# boot volume supports reflink.
#
# Strategy: pick the largest spare (non-boot, unmounted) block device — a local
# NVMe on E6 shapes, or an attached block volume on E5 (which has no local NVMe) —
# format it XFS (reflink=1) and mount it at $MNT. Prints the path to use as FC_ROOT.
# Idempotent: if already mounted, just prints it. If NO spare disk exists, prints
# the boot-disk fallback and warns (density will be limited by disk space).
set -uo pipefail
MNT="${1:-/mnt/fcbench}"

log(){ echo "[oci-storage] $*" >&2; }

if mountpoint -q "$MNT"; then
  log "already mounted at $MNT"; echo "$MNT"; exit 0
fi

# boot disk = the device backing "/"
BOOT_SRC=$(findmnt -no SOURCE / 2>/dev/null)
BOOT_DISK=$(lsblk -no PKNAME "$BOOT_SRC" 2>/dev/null)
[ -n "$BOOT_DISK" ] && BOOT_DISK="/dev/$BOOT_DISK"

# candidate = largest whole disk that is not the boot disk and has no mounted parts
pick_disk() {
  lsblk -dpbno NAME,SIZE,TYPE | awk '$3=="disk"{print $1, $2}' | sort -k2 -n -r | while read -r dev size; do
    [ "$dev" = "$BOOT_DISK" ] && continue
    # skip if any child partition is mounted
    if lsblk -no MOUNTPOINT "$dev" 2>/dev/null | grep -q '[^[:space:]]'; then continue; fi
    echo "$dev"; return
  done
}
DEV=$(pick_disk | head -1)

if [ -z "$DEV" ]; then
  log "WARN: no spare disk (no local NVMe / attached block volume) — falling back to boot disk /opt/fcbench. Density may fill the disk."
  echo "/opt/fcbench"; exit 0
fi

command -v mkfs.xfs >/dev/null 2>&1 || { export DEBIAN_FRONTEND=noninteractive; apt-get install -y -qq xfsprogs >/dev/null 2>&1 || true; }

# format only if it isn't already an xfs volume (idempotent across re-runs)
if ! blkid "$DEV" 2>/dev/null | grep -q 'TYPE="xfs"'; then
  log "formatting $DEV as XFS (reflink=1)"
  mkfs.xfs -f -m reflink=1 "$DEV" >/dev/null 2>&1 || { log "mkfs.xfs failed on $DEV"; echo "/opt/fcbench"; exit 0; }
fi
mkdir -p "$MNT"
mount "$DEV" "$MNT" || { log "mount failed"; echo "/opt/fcbench"; exit 0; }
# verify reflink actually works here
if ! ( dd if=/dev/zero of="$MNT/.rl" bs=1M count=8 status=none && cp --reflink=always "$MNT/.rl" "$MNT/.rl2" ) >/dev/null 2>&1; then
  log "WARN: reflink not working on $MNT despite XFS"
fi
rm -f "$MNT/.rl" "$MNT/.rl2" 2>/dev/null
log "using $DEV mounted at $MNT ($(lsblk -dno SIZE "$DEV" 2>/dev/null))"
echo "$MNT"
