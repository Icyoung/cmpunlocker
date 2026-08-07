#!/usr/bin/env python3
"""Phantom reserve via PMA pin (10GB SKU / 0x2082 only).

Supersedes the region-split carve (apply_phantom_carve.py): splitting the
GSP-visible fbRegionInfo map kills GSP during boot init.  This approach
leaves the map untouched and instead pins a physical range in the
CPU-side PMA regmap (STATE_PIN), so no user allocation can ever receive
the pages that host the GSP-owned phantom structures.

Hook: end of memmgrCreateHeap_IMPL (mem_mgr.c), after the CMP diagnostic
block, before the final return.  Idempotent via "CMP_MEM_RSV" marker.

Current range: [0x900000000, 0xA3FFFFFFF) = [36 GiB, 41 GiB), a 5 GiB hole.
History: [32,44) 12G → [36,44) 8G → [36,41) 5G (2026-08-08, behaviorally
qualified: 72G drip/torture/verify all PASS).  The known-deadly structure
sits at 40 GiB + 64 KiB; the 41G upper edge keeps ~0.94 GiB margin.
If instability ever appears, revert to the 8G hole (see FIX_PLAN_RECLAIM.md).
Runtime kill-switch: NVreg RMCmpPhantomReserve=0 disables the pin.
"""
import pathlib
import sys

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
            /* phantom hole EXPERIMENT: [36G, 41G) — 5G. The known-deadly
             * structure sits at 40G+64K; upper edge 41G keeps ~0.94G margin.
             * If stress passes, usable rises to ~74G; if GSP dies, revert to
             * the qualified 8G hole [36G,44G). */
            if (pmaIsPmaManaged(pRsvPma, 0x900000000ULL, 0xA3FFFFFFFULL))
            {
                pmaSetBlockStateAttrib(pRsvPma, 0x900000000ULL, 0x140000000ULL,
                                       STATE_PIN, STATE_MASK);
                NV_PRINTF(LEVEL_ERROR,
                          "CMP_MEM_RSV: pinned [0x900000000,0xa3fffffff] in PMA (phantom guard, 5G experiment)\\n");
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


def main():
    if len(sys.argv) != 2:
        print("usage: apply_phantom_reserve.py <path/to/mem_mgr.c>", file=sys.stderr)
        return 2
    src = pathlib.Path(sys.argv[1])
    txt = src.read_text(encoding="utf-8")
    changed = False

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
