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
#   3. Disables GSP firmware by default (EnableGpuFirmware ->
#      MODE_DISABLED in kernel/nvidia/nv-reg.h) so the CPU-side RM
#      path we patched is the one that actually runs at init.  With
#      GSP on, GR-enable is decided by signed GSP-RM ucode on the
#      card and the .o_binary patch is a no-op.
#   4. Flips nvidia_drm modeset default to false in
#      kernel/nvidia-drm/nvidia-drm-os-interface.c so nvidia-drm's
#      atomic modeset does not take over the init ordering the GR
#      unlock hooks into.  Previously users had to set this via
#      modprobe.d; now it's baked in.
#   5. Repacks the installer under a new name so you can install it
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

# --- Source-level patches -------------------------------------------------
#
# The three byte patches above target the CPU-side RM entrypoints
# (_nv029797rm / _nv032289rm / _nv031940rm).  On GA102, those only run
# when GSP firmware is off AND nvidia-drm modeset is off.  Otherwise:
#
#  * GSP on  → GR-enable is decided by signed GSP-RM ucode on the card;
#              the CPU-side RM is skipped and our patches are dead code.
#  * modeset on → nvidia-drm atomic modeset takes over the init ordering
#                 the code cave hooks into.
#
# So we flip both defaults in the extracted source tree.  Fail loud if
# NVIDIA changed the exact wording (indicates a version drift).

REG_H="$WORKDIR/src/kernel/nvidia/nv-reg.h"
DRM_C="$WORKDIR/src/kernel/nvidia-drm/nvidia-drm-os-interface.c"

[ -f "$REG_H" ] || die "extracted tree lacks kernel/nvidia/nv-reg.h"
[ -f "$DRM_C" ] || die "extracted tree lacks kernel/nvidia-drm/nvidia-drm-os-interface.c"

REG_STOCK='NV_DEFINE_REG_ENTRY(__NV_ENABLE_GPU_FIRMWARE, NV_REG_ENABLE_GPU_FIRMWARE_DEFAULT_VALUE);'
REG_NEW='NV_DEFINE_REG_ENTRY(__NV_ENABLE_GPU_FIRMWARE, NV_REG_ENABLE_GPU_FIRMWARE_MODE_DISABLED);'
grep -qF -- "$REG_STOCK" "$REG_H" \
    || die "nv-reg.h does not contain the expected EnableGpuFirmware default line"
python3 - "$REG_H" "$REG_STOCK" "$REG_NEW" <<'PY'
import sys, pathlib
path, old, new = sys.argv[1], sys.argv[2], sys.argv[3]
p = pathlib.Path(path)
data = p.read_text()
if data.count(old) != 1:
    sys.exit(f"error: expected exactly one match of stock line in {path}")
p.write_text(data.replace(old, new))
PY
grep -qF -- "$REG_NEW" "$REG_H" \
    || die "post-patch verify failed: EnableGpuFirmware default not updated"
ok "GSP firmware disabled by default (nv-reg.h)"

DRM_STOCK='bool nv_drm_modeset_module_param = true;'
DRM_NEW='bool nv_drm_modeset_module_param = false;'
grep -qF -- "$DRM_STOCK" "$DRM_C" \
    || die "nvidia-drm-os-interface.c does not contain the expected modeset default"
python3 - "$DRM_C" "$DRM_STOCK" "$DRM_NEW" <<'PY'
import sys, pathlib
path, old, new = sys.argv[1], sys.argv[2], sys.argv[3]
p = pathlib.Path(path)
data = p.read_text()
if data.count(old) != 1:
    sys.exit(f"error: expected exactly one match of stock line in {path}")
p.write_text(data.replace(old, new))
PY
grep -qF -- "$DRM_NEW" "$DRM_C" \
    || die "post-patch verify failed: nvidia_drm modeset default not updated"
ok "nvidia_drm modeset default flipped to false (nvidia-drm-os-interface.c)"


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
     the GPU (multi-user.target or single-user), install, reboot again.
  2. Sanity checks after the new driver loads:
       * cat /sys/module/nvidia/parameters/NVreg_EnableGpuFirmware  # expect 0
       * cat /sys/module/nvidia_drm/parameters/modeset              # expect N
     Both defaults are baked into this build; a kernel-cmdline or
     modprobe.d option can still override them, so double-check.
  3. If the module refuses to load, check dmesg for RmInitAdapter
     errors; some 90HX boards need Resizable BAR disabled in BIOS.
EOF
