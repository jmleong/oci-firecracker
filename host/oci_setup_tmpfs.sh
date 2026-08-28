#!/usr/bin/env bash
# oci_setup_tmpfs.sh — put the Firecracker working dir on a RAM-backed, reflink-
# capable filesystem so STORAGE IS HELD CONSTANT across shapes (fair CPU/virtio
# comparison, isolating the Zen4-vs-Zen5 difference from disk type).
#
# Why the loop-image trick: tmpfs has no reflink, and the density bench makes one
# rootfs overlay per microVM (hundreds). Without reflink those are full 4 GB
# copies — hundreds of them would exhaust RAM. So we create an XFS image file on
# tmpfs and loop-mount it: XFS gives reflink (overlays are near-free CoW clones)
# and every byte lives in RAM. Result: no real disk touched, identical on every
# shape (all have >1.5 TB RAM).
#
# Prints the mount path to use as FC_ROOT. Idempotent.
set -uo pipefail
MNT="${1:-/mnt/fcbench}"
IMG_SIZE="${2:-60G}"     # XFS image (holds ~4GB rootfs + near-free reflink overlays)
TMPFS_SIZE="${3:-64G}"   # tmpfs cap backing the image
TDIR=/mnt/fctmp
log(){ echo "[tmpfs] $*" >&2; }

if mountpoint -q "$MNT"; then log "already mounted at $MNT"; echo "$MNT"; exit 0; fi

command -v mkfs.xfs >/dev/null 2>&1 || { export DEBIAN_FRONTEND=noninteractive; apt-get install -y -qq xfsprogs >/dev/null 2>&1 || true; }

mkdir -p "$TDIR"
mountpoint -q "$TDIR" || mount -t tmpfs -o size="$TMPFS_SIZE" tmpfs "$TDIR"
truncate -s "$IMG_SIZE" "$TDIR/xfs.img"
mkfs.xfs -f -m reflink=1 "$TDIR/xfs.img" >/dev/null 2>&1 || { log "mkfs.xfs failed"; echo "/opt/fcbench"; exit 1; }
mkdir -p "$MNT"
mount -o loop "$TDIR/xfs.img" "$MNT" || { log "loop mount failed"; echo "/opt/fcbench"; exit 1; }

# verify reflink truly works on the loop-mounted XFS
if ! ( dd if=/dev/zero of="$MNT/.rl" bs=1M count=8 status=none && cp --reflink=always "$MNT/.rl" "$MNT/.rl2" ) >/dev/null 2>&1; then
  log "WARN: reflink not working on $MNT"
fi
rm -f "$MNT/.rl" "$MNT/.rl2" 2>/dev/null
log "RAM-backed XFS (reflink) mounted at $MNT (img $IMG_SIZE on tmpfs $TMPFS_SIZE)"
echo "$MNT"
