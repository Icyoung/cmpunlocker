#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KVER="$(uname -r)"
INSTALL_MOD_DIR="/lib/modules/${KVER}/updates/cmpunlocker"
INVENTORY_FILE="${INSTALL_MOD_DIR}/gpu_inventory"
INSTALLED_PROFILE="$(cat "${INSTALL_MOD_DIR}/card_profile" 2>/dev/null || true)"

source "${SCRIPT_DIR}/common/lib.sh"

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
step_init 3

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
sec2_logs="$(dmesg 2>/dev/null | grep 'SEC2_DEBUG' || true)"
if [[ -n "${sec2_logs}" ]]; then
    ok "dmesg contains SEC2_DEBUG unlock logs"
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
    warn "80GB is an experimental geometry; this check confirms enumeration/capacity only"
    if printf '%s\n' "${sec2_logs}" | grep -Eqi 'CFG1=0x02779000 LMR=0x0*28b .*devId=0x2082'; then
        ok "Latest available SEC2 logs contain coherent 2082 80GB CFG1/LMR readback"
    else
        warn "Could not confirm CFG1=0x02779000 and LMR=0x0000028B from retained dmesg logs"
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
