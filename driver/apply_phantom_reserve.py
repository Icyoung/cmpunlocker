#!/usr/bin/env python3
"""Phantom reserve via PMA pin (10GB SKU / 0x2082 only).

Supersedes the region-split carve (apply_phantom_carve.py): splitting the
GSP-visible fbRegionInfo map kills GSP during boot init.  This approach
leaves the map untouched and instead pins a physical range in the
CPU-side PMA regmap (STATE_PIN), so no user allocation can ever receive
the pages that host the GSP-owned phantom structures.

Hook: end of memmgrCreateHeap_IMPL (mem_mgr.c), after the CMP diagnostic
block, before the final return.  Idempotent via "CMP_MEM_RSV" marker.

Profile gating (2026-08-12): the phantom structure sits at 40 GiB + 64 KiB,
so only profiles whose 2082 heap extends past 40 GiB (10gb64, 10gb80,
mixed80) need the pin.  For the stable 40 GiB profile (8gb/10gb/mixed) the
structure is above the heap top and this script is a no-op — previously the
pin + MIG tolerance were applied unconditionally, which both stole 4 GiB of
the 40 GiB heap and broke the MIG zero-usage check when the pin was not
PMA-managed.  build.sh passes --profile; without it the legacy behavior
(apply) is kept for manual/dev use.

Current range: [0x900000000, 0xA3FFFFFFF) = [36 GiB, 41 GiB), a 5 GiB hole.
History: [32,44) 12G → [36,44) 8G → [36,41) 5G (2026-08-08) → back to 8G
(same day: SM corruption at ~44.8G) → **5G again (2026-08-09)** for ~72G user heap.
Runtime kill-switch: NVreg RMCmpPhantomReserve=0 disables the pin.
"""
import argparse
import pathlib
import sys

# Profiles whose 2082 heap covers the phantom structure at 40 GiB + 64 KiB.
PINNED_PROFILES = {"10gb64", "10gb80", "mixed80"}

ANCHOR = (
    '                          pmaWprOverlapCount);\n'
    '            }\n'
    '        }\n'
    '    }\n'
)

PIN = """
    /*
     * cmpunlocker phantom reserve (10GB SKU, 80G profile only).
     *
     * 2026-08-07 drip experiments: a GSP-managed structure lives at a fixed
     * physical address ~0xA00010000 (40 GiB + 64 KiB) inside the
     * user-allocatable heap.  When a user allocation receives those pages
     * and is written, GSP dereferences the user data as pointers and dies
     * (Xid 1 / channel wedge).  The structure is invisible to the CPU-side
     * PMA, so pin the enclosing range here; the GSP-visible fbRegionInfo
     * map is deliberately left untouched (splitting it crashes GSP at boot).
     * Runtime kill-switch: NVreg RMCmpPhantomReserve=0 skips the pin (both
     * here and in the MIG zero-usage tolerance below).
     */
    {
        NvU32 rsvDevId = pGpu->idInfo.PCIDeviceID >> 16;
        NvU32 rsvEnable = 1;
        /* runtime kill-switch for root-cause experiments:
         * NVreg_RegistryDwords="RMCmpPhantomReserve=0" disables the pin */
        (void)osReadRegistryDword(pGpu, "RMCmpPhantomReserve", &rsvEnable);
        if (rsvDevId == 0x2082 && rsvEnable != 0 && status == NV_OK &&
            pMemoryManager->pHeap != NULL &&
            pMemoryManager->pHeap->pPmaObject != NULL &&
            memmgrIsPmaInitialized(pMemoryManager))
        {
            PMA *pRsvPma = pMemoryManager->pHeap->pPmaObject;
            /* phantom hole: [36G, 41G) — 5G. Fatal structure at 40G+64K;
             * upper edge 41G leaves ~0.94G margin (validated 2026-08-08). */
            if (pmaIsPmaManaged(pRsvPma, 0x900000000ULL, 0xA3FFFFFFFULL))
            {
                pmaSetBlockStateAttrib(pRsvPma, 0x900000000ULL, 0x140000000ULL,
                                       STATE_PIN, STATE_MASK);
                NV_PRINTF(LEVEL_ERROR,
                          "CMP_MEM_RSV: pinned [0x900000000,0xa3ffffff] in PMA (phantom guard, 5G hole)\\n");
            }
            else
            {
                NV_PRINTF(LEVEL_ERROR,
                          "CMP_MEM_RSV: phantom range not PMA-managed, guard inactive\\n");
            }
        }
        else if (rsvDevId == 0x2082 && rsvEnable == 0)
        {
            NV_PRINTF(LEVEL_ERROR,
                      "CMP_MEM_RSV: phantom guard DISABLED via RMCmpPhantomReserve=0\\n");
        }
    }
"""

# The MIG init path asserts the PMA is 100% free; our 128 MiB pin trips it
# and RmInitAdapter bails.  Teach the check to tolerate exactly our range.
ZERO_CHECK_OLD = (
    "        if (freeMem != totalMem)\n"
)
ZERO_CHECK_NEW = (
    "        /* cmpunlocker: tolerate the phantom-reserve pin when enabled */\n"
    "        NvU32 cmpRsvEnable = 1;\n"
    "        NvU64 cmpPhantomRsv;\n"
    "        (void)osReadRegistryDword(pGpu, \"RMCmpPhantomReserve\", &cmpRsvEnable);\n"
    "        cmpPhantomRsv =\n"
    "            (((pGpu->idInfo.PCIDeviceID >> 16) == 0x2082) && cmpRsvEnable != 0)\n"
    "                ? 0x140000000ULL : 0;\n"
    "        if (freeMem + cmpPhantomRsv != totalMem)\n"
)

# 8G hole → 5G hole upgrade tokens (2026-08-09)
_UPGRADE_8G_TO_5G = (
    ("0xAFFFFFFFULL", "0xA3FFFFFFFULL"),
    ("0x200000000ULL", "0x140000000ULL"),
    ("0xafffffff", "0xa3ffffff"),
    ("phantom guard, 8G hole", "phantom guard, 5G hole"),
    ("[36G, 44G)", "[36G, 41G)"),
)


def _upgrade_hole_size(txt: str) -> tuple[str, bool]:
    changed = False
    for old, new in _UPGRADE_8G_TO_5G:
        if old in txt:
            txt = txt.replace(old, new)
            changed = True
    return txt, changed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=pathlib.Path, help="path/to/mem_mgr.c")
    parser.add_argument(
        "--profile",
        default=None,
        help="card profile (8gb/10gb/mixed/10gb64/10gb80/mixed80); "
        "the pin is only applied for profiles whose 2082 heap exceeds 40 GiB",
    )
    args = parser.parse_args()

    if args.profile is not None and args.profile not in PINNED_PROFILES:
        print(
            f"profile {args.profile}: 2082 heap is 40 GiB, phantom structure "
            "(40 GiB + 64 KiB) is above the heap top — skipping pin"
        )
        return 0

    src = args.source
    txt = src.read_text(encoding="utf-8")
    changed = False

    if "CMP_MEM_RSV" in txt:
        txt, upgraded = _upgrade_hole_size(txt)
        if upgraded:
            changed = True
            print("upgraded phantom hole 8G -> 5G [36G,41G)")

    # pin block
    if "CMP_MEM_RSV" not in txt:
        if txt.count(ANCHOR) != 1:
            print(f"anchor not unique ({txt.count(ANCHOR)} matches)", file=sys.stderr)
            return 1
        txt = txt.replace(ANCHOR, ANCHOR + PIN, 1)
        changed = True
    # include for pmaSetBlockStateAttrib
    INC_OLD = '#include "gpu/mem_mgr/phys_mem_allocator/numa.h"\n'
    INC_NEW = (INC_OLD +
               '#include "gpu/mem_mgr/phys_mem_allocator/phys_mem_allocator_util.h"\n')
    if INC_NEW not in txt:
        if txt.count(INC_OLD) != 1:
            print("numa.h include anchor missing", file=sys.stderr)
            return 1
        txt = txt.replace(INC_OLD, INC_NEW, 1)
        changed = True
    # MIG zero-usage check tolerance for the pinned range
    if "cmpPhantomRsv" not in txt:
        if txt.count(ZERO_CHECK_OLD) != 1:
            print("zero-usage check anchor missing", file=sys.stderr)
            return 1
        txt = txt.replace(ZERO_CHECK_OLD, ZERO_CHECK_NEW, 1)
        changed = True

    if not changed:
        print("already applied; skipping")
        return 0
    src.write_text(txt, encoding="utf-8")
    print(f"inserted; new size: {len(txt)} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
