#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KVER="$(uname -r)"
INSTALL_MOD_DIR="/lib/modules/${KVER}/updates/cmpunlocker"
INVENTORY_FILE="${INSTALL_MOD_DIR}/gpu_inventory"
INSTALLED_PROFILE="$(cat "${INSTALL_MOD_DIR}/card_profile" 2>/dev/null || true)"

source "${SCRIPT_DIR}/common/lib.sh"

[[ "${EUID}" -eq 0 ]] || die "Run verification as root: sudo ${SCRIPT_DIR}/verify.sh"

is_unlocked_memory() {
    local profile="$1"
    local mem_mib="$2"
    [[ "${mem_mib}" =~ ^[0-9]+$ ]] || return 1
    case "${profile}" in
        8gb)
            (( mem_mib >= 60000 )) && return 0
            ;;
        10gb)
            (( mem_mib >= 35000 && mem_mib < 60000 )) && return 0
            ;;
        10gb80)
            (( mem_mib >= 75000 )) && return 0
            ;;
    esac
    return 1
}

is_stock_memory() {
    local profile="$1"
    local mem_mib="$2"
    [[ "${mem_mib}" =~ ^[0-9]+$ ]] || return 1
    case "${profile}" in
        8gb)
            (( mem_mib >= 7680 && mem_mib <= 8704 )) && return 0
            ;;
        10gb)
            (( mem_mib >= 9728 && mem_mib <= 10752 )) && return 0
            ;;
        10gb80)
            (( mem_mib >= 9728 && mem_mib <= 10752 )) && return 0
            ;;
    esac
    return 1
}

banner
step_init 4

step "Locating GPU inventory"
command -v nvidia-smi &>/dev/null || die "nvidia-smi not found"
SMI_MEM_CACHE="$(nvidia-smi --query-gpu=pci.bus_id,memory.total --format=csv,noheader,nounits 2>/dev/null || true)"
[[ -n "${SMI_MEM_CACHE}" ]] || die "nvidia-smi returned no GPU memory data"

GPU_BDFS=()
GPU_DEVIDS=()
GPU_PROFILES=()
GPU_EXPECTED=()

if [[ -r "${INVENTORY_FILE}" ]] && [[ -s "${INVENTORY_FILE}" ]]; then
    info "Using inventory: ${INVENTORY_FILE}"
    while read -r bdf devid profile expected || [[ -n "${bdf:-}" ]]; do
        [[ -n "${bdf:-}" ]] || continue
        [[ "${bdf}" =~ ^# ]] && continue
        GPU_BDFS+=("$(normalize_bus_id "${bdf}")")
        GPU_DEVIDS+=("${devid}")
        GPU_PROFILES+=("${profile}")
        GPU_EXPECTED+=("${expected}")
    done < "${INVENTORY_FILE}"
else
    info "No installed gpu_inventory; enumerating via lspci"
    mapfile -t PCI_LINES < <(lspci -nn 2>/dev/null | grep -iE '10de:20c2|10de:2082' || true)
    [[ ${#PCI_LINES[@]} -gt 0 ]] || die "No unlockable CMP 170HX GPU found (10de:20c2 / 10de:2082)"
    for PCI_LINE in "${PCI_LINES[@]}"; do
        PCI="$(echo "${PCI_LINE}" | awk '{print $1}')"
        PCI_FULL="$(normalize_bus_id "${PCI}")"
        DEVID="$(echo "${PCI_LINE}" | grep -oE '10de:[0-9a-fA-F]{4}' | head -1 | cut -d: -f2 | tr '[:upper:]' '[:lower:]')"
        PROF="$(profile_from_devid "${DEVID}")"
        if [[ "${DEVID}" == "2082" && ( "${INSTALLED_PROFILE}" == "10gb80" || "${INSTALLED_PROFILE}" == "mixed80" ) ]]; then
            PROF="10gb80"
        fi
        [[ "${PROF}" != "unsupported" ]] || continue
        EXP="$(expected_mib_for_profile "${PROF}")"
        GPU_BDFS+=("${PCI_FULL}")
        GPU_DEVIDS+=("${DEVID}")
        GPU_PROFILES+=("${PROF}")
        GPU_EXPECTED+=("${EXP}")
    done
fi

[[ ${#GPU_BDFS[@]} -gt 0 ]] || die "No unlockable GPUs to verify"

step "Checking WPR/PMA safety revision"
CORE_MODULE="${INSTALL_MOD_DIR}/nvidia.ko"
[[ -f "${CORE_MODULE}" ]] || die "Patched core module not found: ${CORE_MODULE}"
if grep -aFq 'SEC2_DEBUG_LATE_PMA:' "${CORE_MODULE}" ||
   grep -aFq 'memmgrSec2DebugLateExtendHighPmaRegion' "${CORE_MODULE}"; then
    die "Installed nvidia.ko still contains the unsafe late-PMA extension; do not stress the GPU"
fi
if ! grep -aFq 'CMP_MEM_SAFE_PMA: revision=wpr-safe-r3' "${CORE_MODULE}"; then
    die "Installed nvidia.ko lacks the wpr-safe-r3 marker; rebuild and cold reboot"
fi
ok "Installed nvidia.ko contains wpr-safe-r3 and no late-PMA extension marker"

running_src="$(cat /sys/module/nvidia/srcversion 2>/dev/null || true)"
installed_src="$(modinfo -F srcversion "${CORE_MODULE}" 2>/dev/null || true)"
if [[ -n "${running_src}" && -n "${installed_src}" ]]; then
    if [[ "${running_src}" != "${installed_src}" ]]; then
        die "Running nvidia module (${running_src}) is not the installed wpr-safe-r3 module (${installed_src}); cold reboot before testing"
    fi
    ok "Running and installed nvidia.ko srcversion match: ${running_src}"
else
    warn "Could not compare running and installed nvidia.ko srcversion"
fi

resolved_module="$(modprobe -n -v nvidia 2>/dev/null | awk '/insmod/ { print $2; exit }' || true)"
if [[ -n "${resolved_module}" && "${resolved_module}" != *"/updates/cmpunlocker/nvidia.ko" ]]; then
    die "modprobe resolves nvidia to ${resolved_module}, not the cmpunlocker safety build"
fi

if [[ -r "${INSTALL_MOD_DIR}/safety_revision" ]]; then
    revision="$(cat "${INSTALL_MOD_DIR}/safety_revision")"
    [[ "${revision}" == "wpr-safe-r3" ]] || die "Unexpected safety revision: ${revision}"
    ok "Installed safety revision: ${revision}"
else
    warn "safety_revision metadata is missing; module binary marker is authoritative"
fi

unsafe_boot_logs="$(dmesg 2>/dev/null | grep -E 'SEC2_DEBUG_LATE_PMA:|late PMA extension status=' || true)"
if [[ -n "${unsafe_boot_logs}" ]]; then
    printf '%s\n' "${unsafe_boot_logs}" | tail -n 20 | sed 's/^/  /'
    die "Current boot executed the removed late-PMA extension; cold reboot into the new module before testing"
fi
safe_pma_logs="$(dmesg 2>/dev/null | grep 'CMP_MEM_SAFE_PMA: revision=wpr-safe-r3' || true)"
latest_safe_pma="$(printf '%s\n' "${safe_pma_logs}" | tail -n 1)"
if [[ -n "${latest_safe_pma}" ]]; then
    [[ "${latest_safe_pma}" == *"late_extension=disabled"* ]] ||
        die "Current boot safety line does not confirm late_extension=disabled"
    ok "Current boot logged diagnostic-only WPR/PMA handling"
    printf '%s\n' "${latest_safe_pma}" | sed 's/^/  /'

    if [[ "${latest_safe_pma}" != *"pmaReady=1"* ||
          "${latest_safe_pma}" != *"pmaRegionStatus=0x0"* ||
          "${latest_safe_pma}" != *"wprAvailable=1"* ]]; then
        if [[ "${INSTALLED_PROFILE}" == "10gb80" || "${INSTALLED_PROFILE}" == "mixed80" ]]; then
            die "80GB stress gate failed: PMA/WPR diagnostics are incomplete"
        fi
        warn "PMA/WPR diagnostics are incomplete; save a diagnostic bundle before testing"
    fi

    if [[ "${latest_safe_pma}" =~ wprDescriptorCovers=([0-9]+) ]]; then
        info "WPR address covered by a PMA descriptor: ${BASH_REMATCH[1]} (diagnostic only; pinned/reserved pages may still be unavailable)"
    fi
    if [[ "${latest_safe_pma}" =~ pmaWprOverlapCount=([0-9]+) ]]; then
        info "PMA region descriptors overlapping WPR: ${BASH_REMATCH[1]} (inspect reservation logs if non-zero)"
    fi
else
    if [[ "${INSTALLED_PROFILE}" == "10gb80" || "${INSTALLED_PROFILE}" == "mixed80" ]]; then
        die "80GB stress gate failed: no current-boot wpr-safe-r3 PMA diagnostic line; cold reboot or collect as root"
    fi
    warn "No current-boot CMP_MEM_SAFE_PMA line retained; collect diagnostics before stress testing"
fi

step "Checking memory unlock status"
failures=0
printf "\n%-16s %-8s %-8s %-12s %-12s %s\n" "BDF" "PCI ID" "Variant" "Expect" "Actual" "Status"
for i in "${!GPU_BDFS[@]}"; do
    bdf="${GPU_BDFS[$i]}"
    devid="${GPU_DEVIDS[$i]}"
    profile="${GPU_PROFILES[$i]}"
    expected="${GPU_EXPECTED[$i]}"
    actual="$(smi_memory_for_bus "${bdf}" || true)"
    [[ -n "${actual}" ]] || actual="?"

    status="FAIL"
    if is_unlocked_memory "${profile}" "${actual}"; then
        status="OK"
        ok "${bdf}: ${actual} MiB (unlocked ${profile})"
    elif is_stock_memory "${profile}" "${actual}"; then
        status="STOCK"
        err "${bdf}: still stock ${actual} MiB (expect ~${expected})"
        failures=$((failures + 1))
    elif [[ "${actual}" == "?" ]]; then
        status="MISSING"
        err "${bdf}: not found in nvidia-smi"
        failures=$((failures + 1))
    else
        status="UNEXPECTED"
        err "${bdf}: unexpected ${actual} MiB (expect ~${expected} for ${profile})"
        failures=$((failures + 1))
    fi

    printf "%-16s %-8s %-8s ~%-11s %-12s %s\n" "${bdf}" "${devid}" "${profile}" "${expected}" "${actual}" "${status}"
done

step "Checking unlock logs and installed profile"
sec2_logs="$(dmesg 2>/dev/null | grep -E 'SEC2_DEBUG|CMP_MEM_' || true)"
if [[ -n "${sec2_logs}" ]]; then
    ok "dmesg contains unlock and memory-layout diagnostics"
    info "Sample:"
    printf '%s\n' "${sec2_logs}" | tail -n 8 | sed 's/^/  /'
else
    warn "No SEC2_DEBUG lines in dmesg (logs may have rotated; unlock can still be OK if memory is unlocked)"
fi

echo ""
if [[ -r "${INSTALL_MOD_DIR}/card_profile" ]]; then
    info "Installed profile: $(cat "${INSTALL_MOD_DIR}/card_profile") / geometry: $(cat "${INSTALL_MOD_DIR}/unlock_geometry" 2>/dev/null || echo '?')"
fi

if [[ "${INSTALLED_PROFILE}" == "10gb80" || "${INSTALLED_PROFILE}" == "mixed80" ]]; then
    warn "80GB is an experimental geometry; this check confirms capacity and boot-time ownership diagnostics only"
    if printf '%s\n' "${sec2_logs}" | grep -Eqi 'CFG1=0x02779000 LMR=0x0*28b .*devId=0x2082'; then
        ok "Latest available logs contain coherent 2082 80GB CFG1/LMR readback"
    else
        warn "Could not confirm CFG1=0x02779000 and LMR=0x0000028B from retained dmesg logs"
    fi

    if printf '%s\n' "${sec2_logs}" | grep -Eq 'CMP_MEM_FBPA_SUMMARY: phase=post_gsp_static_info live=20 disabled=4'; then
        ok "Post-GSP diagnostics report the expected 20 live / 4 disabled FBPAs"
    else
        warn "Could not confirm the expected 20 live FBPAs from retained dmesg logs"
    fi

    cstatus_4g_count="$(printf '%s\n' "${sec2_logs}" | awk '/CMP_MEM_FBPA: phase=post_gsp_static_info/ && /disabled=0/ && /cstatus=0x0*1000/ { count++ } END { print count + 0 }')"
    if [[ "${cstatus_4g_count}" =~ ^[0-9]+$ ]] && (( cstatus_4g_count >= 20 )); then
        ok "At least 20 live FBPA records report the 4GiB tier (CSTATUS=0x1000)"
    else
        warn "Only ${cstatus_4g_count:-0} retained post-GSP FBPA records show CSTATUS=0x1000"
    fi

    if printf '%s\n' "${sec2_logs}" | grep -q 'CMP_MEM_WPR:' &&
       printf '%s\n' "${sec2_logs}" | grep -q 'CMP_MEM_GSP_REGION:'; then
        ok "WPR and GSP region ownership diagnostics are present"
    else
        warn "WPR/GSP region diagnostics are incomplete; run tools/collect-diagnostics.sh before stress"
    fi
fi

if (( failures > 0 )); then
    echo ""
    die "${failures} GPU(s) failed unlock verification. Cold reboot if modules were just installed."
fi

echo ""
ok "All ${#GPU_BDFS[@]} unlockable GPU(s) report the selected capacity target"

if [[ -x "${SCRIPT_DIR}/tools/service.sh" ]]; then
    echo ""
    info "Checking negotiated PCIe generation"
    if ! "${SCRIPT_DIR}/tools/service.sh" verify; then
        warn "Memory capacity verification passed, but PCIe Gen2 is not active"
        exit 1
    fi
fi
exit 0
