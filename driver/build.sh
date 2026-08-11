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
SAFETY_REVISION="wpr-safe-r3"
ALLOW_HOT_RELOAD="${CMPUNLOCKER_ALLOW_HOT_RELOAD:-0}"

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
[[ ! -e "${PATCH_DIR}/late-pma.patch" ]] || die "Unsafe legacy patch must be removed: ${PATCH_DIR}/late-pma.patch"
[[ -d "${KSRC}" ]] || die "Kernel headers not found at ${KSRC}. Install linux-headers-${KVER} (or kernel-devel)."
command -v python3 &>/dev/null || die "python3 is required to apply the card memory profile"
command -v sha256sum &>/dev/null || die "sha256sum is required"
[[ "${ALLOW_HOT_RELOAD}" == "0" || "${ALLOW_HOT_RELOAD}" == "1" ]] || die "CMPUNLOCKER_ALLOW_HOT_RELOAD must be 0 or 1"
[[ -x "${SCRIPT_DIR}/apply_profile.py" ]] || die "Missing executable profile helper: ${SCRIPT_DIR}/apply_profile.py"
info "Building against open-gpu-kernel-modules ${VERSION}"

PATCH_ORDER=(
    sec2-postbl-plm-ss-cfg.patch
    booter-verify.patch
    memory-layout-safety.patch
    bar0-pramin-clamp.patch
    ce-scrub-workarounds.patch
    ss-config4-override.patch
    feat-restore.patch
    early-lmr-write-p1a.patch
    extra-booter-run-p1c.patch
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
    10gb64|10GB64|64gb|64GB)
        PROFILE="10gb64"
        UNLOCK_LABEL="64GB-experimental"
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

PROFILE_HELPER_HASH="$(sha256sum "${SCRIPT_DIR}/apply_profile.py" "${SCRIPT_DIR}/apply_phantom_reserve.py" "${SCRIPT_DIR}/apply_tail_steer.py" "${SCRIPT_DIR}/apply_tail_steer_host_free.py" "${SCRIPT_DIR}/apply_tail_steer_pin.py" "${SCRIPT_DIR}/apply_pt_log.py" "${SCRIPT_DIR}/apply_pma_alloc_log.py" "${SCRIPT_DIR}/apply_pte_map_log.py" "${SCRIPT_DIR}/apply_wpr_rmw_probe.py" "${SCRIPT_DIR}/apply_gsp_radix3_patch.py" "${SCRIPT_DIR}/apply_gsp_postboot_patch.py" "${SCRIPT_DIR}/apply_booter_debug_force.py" "${SCRIPT_DIR}/apply_booter_imem_dump.py" "${SCRIPT_DIR}/apply_booter_sec2_poll_dump.py" "${SCRIPT_DIR}/apply_sig_dmem_dump.py" "${SCRIPT_DIR}/apply_booter_verify_bypass.py" "${SCRIPT_DIR}/apply_booter_os_postplm_patch.py" "${SCRIPT_DIR}/apply_booter_os_force_mbox.py" "${SCRIPT_DIR}/apply_booter_mbox_forgive.py" "${SCRIPT_DIR}/apply_os_path_write.py" "${SCRIPT_DIR}/apply_sec2_dma_probe.py" 2>/dev/null | sha256sum | cut -d' ' -f1)"
BUILD_STAMP="${VERSION}:${KVER}:${PROFILE}:${PATCH_HASH}:${PROFILE_HELPER_HASH}:${CMPUNLOCKER_PRODUCTION:-0}:$(sha256sum "${SCRIPT_DIR}/build.sh" | cut -d' ' -f1)"

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
    info "Applying optional GSP-RM RAM patch hook to kernel_gsp.c..."
    python3 "${SCRIPT_DIR}/apply_gsp_radix3_patch.py" "${GSP_C}"
    ok "GSP-RM RAM patch hook applied (RMCmpGspFwPatchA=1 to enable)"
    if [[ "${EXPERIMENTAL_80GB}" -eq 1 ]]; then
        warn "10 GB -> 80 GB is experimental; capacity recognition does not prove workload stability"
    fi

    # Phantom guard: pin the GSP-metadata collision zone out of the PMA.
    # No-op on non-0x2082 cards and on profiles whose heap does not cover it.
    MEM_MGR_C_PREP="${SRC_DIR}/src/nvidia/src/kernel/gpu/mem_mgr/mem_mgr.c"
    [[ -f "${MEM_MGR_C_PREP}" ]] || die "Missing ${MEM_MGR_C_PREP} after patching"
    info "Applying phantom reserve (PMA pin) to mem_mgr.c..."
    python3 "${SCRIPT_DIR}/apply_phantom_reserve.py" "${MEM_MGR_C_PREP}"
    ok "Phantom reserve applied"

    # Tail-steer (P0/P1): log regionTag; optionally squeeze GSP PMA free space
    # into a FB-tail corridor (RMCmpTailSteer=1) and pin that corridor on host.
    info "Applying tail-steer probe to kernel_gsp.c..."
    python3 "${SCRIPT_DIR}/apply_tail_steer.py" "${GSP_C}"
    GSP_CLIENT_C_PREP="${SRC_DIR}/src/nvidia/src/kernel/gpu/mem_mgr/mem_mgr_gsp_client.c"
    [[ -f "${GSP_CLIENT_C_PREP}" ]] || die "Missing ${GSP_CLIENT_C_PREP} after patching"
    info "Applying tail-steer host-free reopen to mem_mgr_gsp_client.c..."
    python3 "${SCRIPT_DIR}/apply_tail_steer_host_free.py" "${GSP_CLIENT_C_PREP}"
    info "Applying optional tail-steer host pin to mem_mgr.c..."
    python3 "${SCRIPT_DIR}/apply_tail_steer_pin.py" "${MEM_MGR_C_PREP}"
    ok "Tail-steer helpers applied (RMCmpTailSteer=1; host-free reopen; RMCmpTailPin optional)"

    GSP_TU102_C_PREP="${SRC_DIR}/src/nvidia/src/kernel/gpu/gsp/arch/turing/kernel_gsp_tu102.c"
    GSP_GA100_C_PREP="${SRC_DIR}/src/nvidia/src/kernel/gpu/gsp/arch/ampere/kernel_gsp_ga100.c"
    [[ -f "${GSP_TU102_C_PREP}" ]] || die "Missing ${GSP_TU102_C_PREP} after patching"
    [[ -f "${GSP_GA100_C_PREP}" ]] || die "Missing ${GSP_GA100_C_PREP} after patching"

    # Debug instrumentation (2026-08-08 32G-wrap hunt): log every page-table
    # page allocation (PA + VA range) and every large PMA allocation.
    # Skipped entirely in production builds (CMPUNLOCKER_PRODUCTION=1): these
    # are pure loggers that fire at runtime on every allocation/map.
    if [[ "${CMPUNLOCKER_PRODUCTION:-0}" != "1" ]]; then
    GMMU_WALK_C_PREP="${SRC_DIR}/src/nvidia/src/kernel/gpu/mmu/gmmu_walk.c"
    PMA_C_PREP="${SRC_DIR}/src/nvidia/src/kernel/gpu/mem_mgr/phys_mem_allocator/phys_mem_allocator.c"
    [[ -f "${GMMU_WALK_C_PREP}" ]] || die "Missing ${GMMU_WALK_C_PREP} after patching"
    [[ -f "${PMA_C_PREP}" ]] || die "Missing ${PMA_C_PREP} after patching"
    info "Applying page-table allocation logging to gmmu_walk.c..."
    python3 "${SCRIPT_DIR}/apply_pt_log.py" "${GMMU_WALK_C_PREP}"
    info "Applying PMA allocation logging to phys_mem_allocator.c..."
    python3 "${SCRIPT_DIR}/apply_pma_alloc_log.py" "${PMA_C_PREP}"
    MMU_WALK_MAP_C_PREP="${SRC_DIR}/src/nvidia/src/libraries/mmu/mmu_walk_map.c"
    VMA_C_PREP="${SRC_DIR}/src/nvidia/src/kernel/gpu/mem_mgr/arch/maxwell/virt_mem_allocator_gm107.c"
    [[ -f "${MMU_WALK_MAP_C_PREP}" ]] || die "Missing ${MMU_WALK_MAP_C_PREP} after patching"
    [[ -f "${VMA_C_PREP}" ]] || die "Missing ${VMA_C_PREP} after patching"
    info "Applying PTE map logging to mmu_walk_map.c + virt_mem_allocator_gm107.c..."
    python3 "${SCRIPT_DIR}/apply_pte_map_log.py" "${MMU_WALK_MAP_C_PREP}" "${VMA_C_PREP}"
    info "Applying post-BooterLoad WPR RMW probe to kernel_gsp_tu102.c..."
    python3 "${SCRIPT_DIR}/apply_wpr_rmw_probe.py" "${GSP_TU102_C_PREP}"
    fi
    if [[ "${CMPUNLOCKER_STRIP_POST0808:-0}" != "1" ]]; then
    info "Applying optional post-scheduling GSP patch A to mem_mgr.c..."
    python3 "${SCRIPT_DIR}/apply_gsp_postboot_patch.py" "${MEM_MGR_C_PREP}" "${GSP_C}" "${GSP_TU102_C_PREP}"
    info "Applying optional Booter DBG force hook to kernel_gsp_ga100.c + kernel_gsp_tu102.c..."
    python3 "${SCRIPT_DIR}/apply_booter_debug_force.py" "${GSP_GA100_C_PREP}" "${GSP_TU102_C_PREP}"
    OS_IF_C_PREP="${SRC_DIR}/kernel-open/nvidia/os-interface.c"
    GSP_BOOTER_TU102_C_PREP="${SRC_DIR}/src/nvidia/src/kernel/gpu/gsp/arch/turing/kernel_gsp_booter_tu102.c"
    [[ -f "${OS_IF_C_PREP}" ]] || die "Missing ${OS_IF_C_PREP} after patching"
    [[ -f "${GSP_BOOTER_TU102_C_PREP}" ]] || die "Missing ${GSP_BOOTER_TU102_C_PREP} after patching"
    info "Applying os_cmpWritePathFile helper to os-interface.c..."
    python3 "${SCRIPT_DIR}/apply_os_path_write.py" "${OS_IF_C_PREP}"
    GSP_FALCON_TU102_C_PREP="${SRC_DIR}/src/nvidia/src/kernel/gpu/gsp/arch/turing/kernel_gsp_falcon_tu102.c"
    GSP_FALCON_GA102_C_PREP="${SRC_DIR}/src/nvidia/src/kernel/gpu/gsp/arch/ampere/kernel_gsp_falcon_ga102.c"
    [[ -f "${GSP_FALCON_TU102_C_PREP}" ]] || die "Missing ${GSP_FALCON_TU102_C_PREP} after patching"
    [[ -f "${GSP_FALCON_GA102_C_PREP}" ]] || die "Missing ${GSP_FALCON_GA102_C_PREP} after patching"
    info "Applying optional SEC2 post-halt dump to kernel_gsp_falcon_tu102.c (GA100 uses TU102 HAL)..."
    python3 "${SCRIPT_DIR}/apply_booter_sec2_poll_dump.py" "${GSP_FALCON_TU102_C_PREP}"
    GSP_C_PREP="${SRC_DIR}/src/nvidia/src/kernel/gpu/gsp/kernel_gsp.c"
    [[ -f "${GSP_C_PREP}" ]] || die "Missing ${GSP_C_PREP} after patching"
    info "Applying optional sig DMEM template dump to kernel_gsp.c..."
    python3 "${SCRIPT_DIR}/apply_sig_dmem_dump.py" "${GSP_C_PREP}"
    info "Applying optional DMEM slot experiment to kernel_gsp.c..."
    python3 "${SCRIPT_DIR}/apply_booter_verify_bypass.py" "${GSP_C_PREP}"
    info "Applying optional post-PLM Booter OS skip-app patch to kernel_gsp_tu102.c..."
    python3 "${SCRIPT_DIR}/apply_booter_os_postplm_patch.py" "${GSP_TU102_C_PREP}"
    info "Applying optional post-PLM Booter OS force-mbox0 patch to kernel_gsp_tu102.c..."
    python3 "${SCRIPT_DIR}/apply_booter_os_force_mbox.py" "${GSP_TU102_C_PREP}"
    info "Applying optional Booter mbox 0xb forgive to kernel_gsp_booter_tu102.c..."
    python3 "${SCRIPT_DIR}/apply_booter_mbox_forgive.py" "${GSP_BOOTER_TU102_C_PREP}"
    fi
    GSP_C_PREP="${SRC_DIR}/src/nvidia/src/kernel/gpu/gsp/kernel_gsp.c"
    [[ -f "${GSP_C_PREP}" ]] || die "Missing ${GSP_C_PREP} after patching"
    if [[ "${CMPUNLOCKER_PRODUCTION:-0}" != "1" ]]; then
    info "Applying optional SEC2 DMA Step-0 probe to kernel_gsp.c + kernel_gsp_tu102.c..."
    python3 "${SCRIPT_DIR}/apply_sec2_dma_probe.py" "${GSP_C_PREP}" "${GSP_TU102_C_PREP}"
    ok "Debug instrumentation applied (RMCmpBooterDbg=1; RMCmpBooterImemDump=1; RMCmpSigDmemDump=1; RMCmpDmemSlotOff/Val; RMCmpBooterSkipApp=1; RMCmpBooterForceMbox0=1; RMCmpSec2DmaProbe=1)"
    else
    ok "Production build: probe/instrumentation generators skipped"
    fi

    printf '%s\n' "${BUILD_STAMP}" > "${STAMP_FILE}"
fi

GSP_C="${SRC_DIR}/src/nvidia/src/kernel/gpu/gsp/kernel_gsp.c"
MEM_MGR_C="${SRC_DIR}/src/nvidia/src/kernel/gpu/mem_mgr/mem_mgr.c"
[[ -f "${GSP_C}" ]] || die "Missing ${GSP_C} after preparation"
[[ -f "${MEM_MGR_C}" ]] || die "Missing ${MEM_MGR_C} after preparation"

# Always re-run the source gate, including incremental/cache reuse.  The build
# stamp is not treated as authority if someone modified the extracted tree.
if grep -R -Fq 'memmgrSec2DebugLateExtendHighPmaRegion' \
        "${SRC_DIR}/src/nvidia" "${SRC_DIR}/kernel-open" 2>/dev/null ||
   grep -R -Fq 'SEC2_DEBUG_LATE_PMA:' \
        "${SRC_DIR}/src/nvidia" "${SRC_DIR}/kernel-open" 2>/dev/null; then
    die "Prepared NVIDIA source still contains the removed late-PMA extension"
fi
if ! grep -Fq 'CMP_MEM_SAFE_PMA: revision=wpr-safe-r3' "${MEM_MGR_C}"; then
    die "Patched mem_mgr.c lacks the ${SAFETY_REVISION} diagnostic marker"
fi
ok "Source safety gate passed: no reserved-region late registration"

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

# Refuse to install a module that still contains the known WPR-corrupting
# late-PMA path.  The positive marker also prevents accidentally reusing an
# old cached module that predates this safety revision.
validated_core=0
for ko in "${KO_FILES[@]}"; do
    if [[ "$(basename "${ko}")" != "nvidia.ko" ]]; then
        continue
    fi
    if grep -aFq 'SEC2_DEBUG_LATE_PMA:' "${ko}" ||
       grep -aFq 'memmgrSec2DebugLateExtendHighPmaRegion' "${ko}"; then
        die "Unsafe nvidia.ko contains the removed late-PMA extension path: ${ko}"
    fi
    if ! grep -aFq 'CMP_MEM_SAFE_PMA: revision=wpr-safe-r3' "${ko}"; then
        die "nvidia.ko lacks the ${SAFETY_REVISION} safety marker: ${ko}"
    fi
    validated_core=$((validated_core + 1))
done
(( validated_core > 0 )) || die "Built nvidia.ko was not found for safety validation"
ok "Validated ${validated_core} nvidia.ko artifact(s): reserved memory is not late-registered with PMA"

for ko in "${KO_FILES[@]}"; do
    base="$(basename "${ko}")"
    install -m 0644 "${ko}" "${INSTALL_MOD_DIR}/${base}"
    ok "Installed ${base}"
done
printf '%s\n' "${SAFETY_REVISION}" > "${INSTALL_MOD_DIR}/safety_revision"
ok "Recorded safety revision ${SAFETY_REVISION}"

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
if [[ "${ALLOW_HOT_RELOAD}" != "1" ]]; then
    warn "Hot reload is disabled by default for WPR safety."
    info "The patched modules are installed but will not be activated in the current GPU boot state."
    info "Perform a complete power-off: shutdown -h now; remove standby power if the platform retains GPU state."
    echo ""
    exit 0
fi

warn "Developer override CMPUNLOCKER_ALLOW_HOT_RELOAD=1 is active."
warn "A hot reload cannot prove that stale GSP/WPR state was cleared; do not use it for memory stress qualification."
info "Attempting the explicitly requested NVIDIA module reload..."
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
        ok "Patched NVIDIA modules loaded by developer override"
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
    warn "Could not unload all NVIDIA modules"
fi

echo ""
if [[ "${reload_ok}" -eq 1 ]]; then
    warn "The module is loaded, but a complete power-off is still required before stress testing."
else
    warn "Modules are installed but not active."
fi
info "Perform a complete power-off before verification: shutdown -h now"
echo ""
