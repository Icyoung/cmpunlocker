#!/bin/bash
# install_gr_unlock.sh — one-shot GR-engine unlock installer for
# NVIDIA CMP 90HX / 70HX on Linux, using NVIDIA driver 610.43.02.
#
# Usage:
#   sudo ./install_gr_unlock.sh /path/to/NVIDIA-Linux-x86_64-610.43.02.run
#
# What it does:
#   1. Extracts the given stock .run installer into a scratch dir.
#   2. Patches the closed nv-kernel.o_binary in place (three byte
#      patches + 124-byte code cave; see patch_gr_unlock.py).
#   3. Repacks the installer under a new name so you can install it
#      normally with `sudo sh NVIDIA-...-gr-unlock.run`.
#
# What it does NOT do:
#   - Install the patched driver.  We hand you a self-extracting .run
#     that you review and run yourself.
#   - Touch anything the open-driver cmpunlocker build cares about.
#     GR unlock and cmpunlocker's open-driver 170HX unlock are two
#     separate driver installs that cannot coexist on the same host.
#   - Support any driver version other than 610.43.02.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STOCK_MD5="203b7f999395d22e041f6ed50e0d5646"

die() { echo "error: $*" >&2; exit 1; }
info() { echo "[info] $*"; }
ok()   { echo "[ok]   $*"; }

[ $# -eq 1 ] || die "usage: $0 /path/to/NVIDIA-Linux-x86_64-610.43.02.run"

INPUT="$1"
[ -f "$INPUT" ] || die "input file not found: $INPUT"

command -v python3 >/dev/null || die "python3 is required"
[ -f "$SCRIPT_DIR/patch_gr_unlock.py" ] || die "missing patch_gr_unlock.py alongside this script"

WORKDIR="$(mktemp -d /tmp/gr_unlock.XXXXXX)"
trap 'rm -rf "$WORKDIR"' EXIT

info "extracting $INPUT to $WORKDIR"
sh "$INPUT" --extract-only --target "$WORKDIR/src" >/dev/null
[ -d "$WORKDIR/src" ] || die "extraction failed (no src/)"

BLOB="$WORKDIR/src/kernel/nvidia/nv-kernel.o_binary"
[ -f "$BLOB" ] || die "extracted tree lacks kernel/nvidia/nv-kernel.o_binary"

got_md5="$(md5sum "$BLOB" | awk '{print $1}')"
if [ "$got_md5" != "$STOCK_MD5" ]; then
    die "input driver's nv-kernel.o_binary MD5=$got_md5 does not match \
610.43.02 (expected $STOCK_MD5).  This tool only supports the stock \
NVIDIA 610.43.02 closed driver."
fi
ok "input driver validated (MD5 $got_md5)"

info "patching nv-kernel.o_binary..."
python3 "$SCRIPT_DIR/patch_gr_unlock.py" "$BLOB" "$BLOB.new"
mv "$BLOB.new" "$BLOB"
ok "patch applied"

# Repack.  Makeself supports self-recreating an archive from an
# extracted directory, but we cannot rely on the extract-only mode
# preserving all metadata.  Simplest safe approach: use the extracted
# tree as an ordinary directory and let the user install from it.
OUT_RUN="$SCRIPT_DIR/NVIDIA-Linux-x86_64-610.43.02-gr-unlock.run"
info "repacking as self-extracting .run at $OUT_RUN"

if command -v makeself.sh >/dev/null; then
    makeself.sh --gzip "$WORKDIR/src" "$OUT_RUN" \
        "NVIDIA driver 610.43.02 (GR-unlock for CMP 90HX/70HX)" \
        ./nvidia-installer >/dev/null
    ok "produced $OUT_RUN"
    info "install with: sudo sh $OUT_RUN"
else
    # No makeself on this host — stage a directory the user installs
    # from directly.
    OUT_DIR="$SCRIPT_DIR/NVIDIA-Linux-x86_64-610.43.02-gr-unlock"
    rm -rf "$OUT_DIR"
    cp -a "$WORKDIR/src" "$OUT_DIR"
    ok "patched driver staged at $OUT_DIR"
    info "install with: cd '$OUT_DIR' && sudo ./nvidia-installer"
fi

cat <<EOF

Next steps:
  1. Reboot into a state with no nvidia_drm / no display server holding
     the GPU (multi-user.target or single-user).
  2. Verify \`cat /sys/module/nvidia_drm/parameters/modeset\` is 'N' at
     boot after the new driver is loaded — nvidia_drm.modeset=1 breaks
     the init path the GR unlock relies on.
  3. If the module refuses to load, check dmesg for RmInitAdapter
     errors; some 90HX boards need Resizable BAR disabled in BIOS.
EOF
