#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mapfile -t SUPPORTED_VERSIONS < <(grep -E '^[0-9]+\.[0-9]+\.[0-9]+$' "${SCRIPT_DIR}/VERSION")
DEFAULT_VERSION="${SUPPORTED_VERSIONS[0]:-}"
VERSION="${CMPUNLOCKER_DRIVER_VERSION:-${DEFAULT_VERSION}}"
PATCH_DIR="${SCRIPT_DIR}/patches"
BUILD_ROOT="${CMPUNLOCKER_BUILD_DIR:-${SCRIPT_DIR}/.build}"
SRC_NAME="open-gpu-kernel-modules-${VERSION}"
SRC_DIR="${BUILD_ROOT}/${SRC_NAME}"
TARBALL="${BUILD_ROOT}/${SRC_NAME}.tar.gz"
TARBALL_URL="https://github.com/NVIDIA/open-gpu-kernel-modules/archive/refs/tags/${VERSION}.tar.gz"
KVER="$(uname -r)"
KSRC="/lib/modules/${KVER}/build"
INSTALL_MOD_DIR="/lib/modules/${KVER}/updates/cmpunlocker"

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
else
    RED=""; GREEN=""; YELLOW=""; CYAN=""; NC=""
fi

info() { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()   { echo -e "${GREEN}[ OK ]${NC}  $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC}  $*"; }
die()  { echo -e "${RED}[FAIL]${NC}  $*" >&2; exit 1; }

version_supported() {
    local v="$1"
    local s
    for s in "${SUPPORTED_VERSIONS[@]}"; do
        [[ "${v}" == "${s}" ]] && return 0
    done
    return 1
}

[[ "${EUID}" -eq 0 ]] || die "Run as root: sudo ${SCRIPT_DIR}/build.sh"
[[ -n "${VERSION}" ]] || die "No driver version set (driver/VERSION empty and CMPUNLOCKER_DRIVER_VERSION unset)"
version_supported "${VERSION}" || die "Unsupported driver version '${VERSION}' (supported: ${SUPPORTED_VERSIONS[*]})"
[[ -d "${PATCH_DIR}" ]] || die "Missing patches directory: ${PATCH_DIR}"
[[ -d "${KSRC}" ]] || die "Kernel headers not found at ${KSRC}. Install linux-headers-${KVER} (or kernel-devel)."
command -v python3 &>/dev/null || die "python3 is required to apply the card memory profile"
command -v sha256sum &>/dev/null || die "sha256sum is required"
[[ -x "${SCRIPT_DIR}/apply_profile.py" ]] || die "Missing executable profile helper: ${SCRIPT_DIR}/apply_profile.py"
info "Building against open-gpu-kernel-modules ${VERSION}"

PATCH_ORDER=(
    sec2-postbl-plm-ss-cfg.patch
    booter-verify.patch
    late-pma.patch
    bar0-pramin-clamp.patch
    ce-scrub-workarounds.patch
    persistent-sw-state.patch
    pcie-gen2.patch
    pcie-gen2-probe-retrain.patch
    name-string.patch
)
PATCH_FILES=()
for name in "${PATCH_ORDER[@]}"; do
    p="${PATCH_DIR}/${name}"
    [[ -f "${p}" ]] || die "Missing patch: ${p}"
    PATCH_FILES+=("${p}")
done
PATCH_HASH="$(cat "${PATCH_FILES[@]}" | sha256sum | cut -d' ' -f1)"

PROFILE="${CMPUNLOCKER_CARD_PROFILE:-8gb}"
EXPERIMENTAL_80GB=0
case "${PROFILE}" in
    8gb|8GB)
        PROFILE="8gb"
        UNLOCK_LABEL="64GB"
        ;;
    10gb|10GB)
        PROFILE="10gb"
        UNLOCK_LABEL="40GB"
        ;;
    10gb80|10GB80|80gb|80GB)
        PROFILE="10gb80"
        UNLOCK_LABEL="80GB-experimental"
        EXPERIMENTAL_80GB=1
        ;;
    mixed|MIXED)
        PROFILE="mixed"
        UNLOCK_LABEL="20c2=64GB,2082=40GB"
        ;;
    mixed80|MIXED80)
        PROFILE="mixed80"
        UNLOCK_LABEL="20c2=64GB,2082=80GB-experimental"
        EXPERIMENTAL_80GB=1
        ;;
    *)
        die "Unknown CMPUNLOCKER_CARD_PROFILE='${PROFILE}' (use 8gb, 10gb, 10gb80, mixed, or mixed80)"
        ;;
esac

PROFILE_HELPER_HASH="$(sha256sum "${SCRIPT_DIR}/apply_profile.py" | cut -d' ' -f1)"
BUILD_STAMP="${VERSION}:${KVER}:${PROFILE}:${PATCH_HASH}:${PROFILE_HELPER_HASH}:$(sha256sum "${SCRIPT_DIR}/build.sh" | cut -d' ' -f1)"

mkdir -p "${BUILD_ROOT}"

if [[ ! -f "${TARBALL}" ]]; then
    info "Downloading open-gpu-kernel-modules ${VERSION}..."
    curl -L --fail -o "${TARBALL}.partial" "${TARBALL_URL}"
    mv "${TARBALL}.partial" "${TARBALL}"
    ok "Downloaded ${TARBALL}"
else
    ok "Using cached tarball ${TARBALL}"
fi

STAMP_FILE="${SRC_DIR}/.cmpunlocker-stamp"
if [[ -d "${SRC_DIR}" ]] && [[ "$(cat "${STAMP_FILE}" 2>/dev/null || true)" == "${BUILD_STAMP}" ]]; then
    SKIP_PREP=1
    ok "Source tree already extracted and patched for this exact build; reusing it"
else
    SKIP_PREP=0
    info "Extracting sources..."
    rm -rf "${SRC_DIR}"
    tar -xzf "${TARBALL}" -C "${BUILD_ROOT}"
    if [[ ! -d "${SRC_DIR}" ]]; then
        extracted="$(find "${BUILD_ROOT}" -maxdepth 1 -type d -name "${SRC_NAME}*" | head -1)"
        [[ -n "${extracted}" ]] || die "Extracted source tree not found"
        mv "${extracted}" "${SRC_DIR}"
    fi
    ok "Sources ready: ${SRC_DIR}"

    info "Applying unlock patches..."
    cd "${SRC_DIR}"
    for i in "${!PATCH_ORDER[@]}"; do
        info "  ${PATCH_ORDER[$i]}"
        patch -p1 < "${PATCH_FILES[$i]}"
    done
    ok "All patches applied"

    GSP_C="${SRC_DIR}/src/nvidia/src/kernel/gpu/gsp/kernel_gsp.c"
    [[ -f "${GSP_C}" ]] || die "Missing ${GSP_C} after patching"

    info "Applying memory profile ${PROFILE} (${UNLOCK_LABEL}) to compiled C constants..."
    python3 "${SCRIPT_DIR}/apply_profile.py" --source "${GSP_C}" --profile "${PROFILE}"
    ok "Memory profile ${PROFILE}: unlock_geometry=${UNLOCK_LABEL}"
    if [[ "${EXPERIMENTAL_80GB}" -eq 1 ]]; then
        warn "10 GB -> 80 GB is experimental; capacity recognition does not prove workload stability"
    fi

    printf '%s\n' "${BUILD_STAMP}" > "${STAMP_FILE}"
fi

cd "${SRC_DIR}"
mkdir -p "${INSTALL_MOD_DIR}"
printf '%s\n' "${VERSION}" > "${INSTALL_MOD_DIR}/driver_version"
printf '%s\n' "${PROFILE}" > "${INSTALL_MOD_DIR}/card_profile"
printf '%s\n' "${UNLOCK_LABEL}" > "${INSTALL_MOD_DIR}/unlock_geometry"
printf '%s\n' "${EXPERIMENTAL_80GB}" > "${INSTALL_MOD_DIR}/experimental_80gb"
if [[ -n "${CMPUNLOCKER_GPU_INVENTORY:-}" ]]; then
    printf '%s\n' "${CMPUNLOCKER_GPU_INVENTORY}" > "${INSTALL_MOD_DIR}/gpu_inventory"
    ok "Wrote gpu_inventory ($(echo "${CMPUNLOCKER_GPU_INVENTORY}" | grep -c . || true) GPU(s))"
else
    : > "${INSTALL_MOD_DIR}/gpu_inventory"
fi

info "Building modules for kernel ${KVER}..."
find . -name "*.sh" -exec chmod +x {} + 2>/dev/null || true
if [[ "${SKIP_PREP}" -eq 0 ]]; then
    rm -rf src/nvidia/_out src/nvidia-modeset/_out kernel-open/conftest 2>/dev/null || true
else
    info "Reusing prior build output — incremental rebuild"
fi

JOBS="$(nproc)"
CC_CMD="gcc"
if command -v ccache &>/dev/null; then
    CC_CMD="ccache gcc"
    info "ccache detected — compiler output will be cached for faster rebuilds"
fi
make -j"${JOBS}" modules SYSSRC="${KSRC}" CC="${CC_CMD}"
ok "Modules built"
if command -v ccache &>/dev/null; then
    ccache -s 2>/dev/null | sed 's/^/  /' || true
fi
info "Installing modules to ${INSTALL_MOD_DIR}..."
mkdir -p "${INSTALL_MOD_DIR}"

mapfile -t KO_FILES < <(find "${SRC_DIR}" -type f \( \
    -name 'nvidia.ko' -o -name 'nvidia-modeset.ko' -o -name 'nvidia-uvm.ko' \
    -o -name 'nvidia-drm.ko' -o -name 'nvidia-peermem.ko' \) \
    ! -path '*/conftest/*' | sort -u)
[[ ${#KO_FILES[@]} -gt 0 ]] || die "No built nvidia*.ko found"

for ko in "${KO_FILES[@]}"; do
    base="$(basename "${ko}")"
    install -m 0644 "${ko}" "${INSTALL_MOD_DIR}/${base}"
    ok "Installed ${base}"
done

depmod -a "${KVER}"
ok "depmod complete"
rebuild_initramfs() {
    if command -v update-initramfs &>/dev/null; then
        info "Rebuilding initramfs (update-initramfs)..."
        update-initramfs -u -k "${KVER}"
        ok "initramfs rebuilt"
        return 0
    fi
    if command -v dracut &>/dev/null; then
        info "Rebuilding initramfs (dracut)..."
        dracut --force --kver "${KVER}"
        ok "initramfs rebuilt"
        return 0
    fi
    if command -v mkinitcpio &>/dev/null; then
        info "Rebuilding initramfs (mkinitcpio)..."
        mkinitcpio -P
        ok "initramfs rebuilt"
        return 0
    fi
    warn "No initramfs tool found — rebuild manually before rebooting"
    return 1
}

rebuild_initramfs || true
resolved="$(modprobe -n -v nvidia 2>/dev/null | awk '/insmod/ {print $2; exit}' || true)"
if [[ -n "${resolved}" ]]; then
    info "modprobe will load: ${resolved}"
    if [[ "${resolved}" != *"/updates/cmpunlocker/"* ]]; then
        warn "Resolved nvidia.ko is not under updates/cmpunlocker/"
    fi
fi
info "Attempting to unload NVIDIA modules..."
systemctl stop nvidia-persistenced 2>/dev/null || true
systemctl stop nvidia-fabricmanager 2>/dev/null || true
reload_ok=0
if lsmod | grep -q '^nvidia'; then
    for mod in nvidia_drm nvidia_uvm nvidia_modeset nvidia; do
        modprobe -r "${mod}" 2>/dev/null || true
    done
    sleep 1
fi

if ! lsmod | grep -q '^nvidia '; then
    if modprobe nvidia && modprobe nvidia-modeset; then
        modprobe nvidia-uvm 2>/dev/null || true
        modprobe nvidia-drm 2>/dev/null || true
        reload_ok=1
        ok "Patched NVIDIA modules loaded"
        running_src="$(cat /sys/module/nvidia/srcversion 2>/dev/null || true)"
        patched_src="$(modinfo -F srcversion "${INSTALL_MOD_DIR}/nvidia.ko" 2>/dev/null || true)"
        if [[ -n "${running_src}" && -n "${patched_src}" && "${running_src}" != "${patched_src}" ]]; then
            warn "Loaded nvidia srcversion (${running_src}) != patched (${patched_src})"
            reload_ok=0
        fi
    else
        warn "modprobe failed"
    fi
else
    warn "Could not unload nvidia modules"
fi
echo ""
if [[ "${reload_ok}" -eq 1 ]]; then
    ok "Build and install finished. Verify with: nvidia-smi"
    info "If memory shows stock size, do cold reboot."
else
    warn "Modules installed but running driver is still stock."
    info "Perform cold reboot: shutdown -h now"
fi
echo ""
