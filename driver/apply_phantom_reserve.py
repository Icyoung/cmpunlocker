#!/usr/bin/env python3
"""Phantom reserve via PMA pin (10GB SKU / 0x2082 only).

Supersedes the region-split carve (apply_phantom_carve.py): splitting the
GSP-visible fbRegionInfo map kills GSP during boot init.  This approach
leaves the map untouched and instead pins a physical range in the
CPU-side PMA regmap (STATE_PIN), so no user allocation can ever receive
the pages that host the GSP-owned phantom structures.

Hook: end of memmgrCreateHeap_IMPL (mem_mgr.c), after the CMP diagnostic
block, before the final return.  Idempotent via "CMP_MEM_RSV" marker.

Current range: [0x900000000, 0xAFFFFFFF) = [36 GiB, 44 GiB), an 8 GiB
hole (4 GiB-aligned, deliberately NOT die-aligned: the measured 37-40 GiB
phantom activity band straddles the 40 GiB die boundary — the known
structure sits at 40 GiB + 64 KiB — so a die-aligned 8 GiB window cannot
cover it).  Narrowed from the original [32 GiB, 44 GiB) wide hole.
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
     * PMA, so pin the enclosing 128 MiB here; the GSP-visible fbRegionInfo
     * map is deliberately left untouched (splitting it crashes GSP at boot).
     */
    {
        NvU32 rsvDevId = pGpu->idInfo.PCIDeviceID >> 16;
        if (rsvDevId == 0x2082 && status == NV_OK &&
            pMemoryManager->pHeap != NULL &&
            pMemoryManager->pHeap->pPmaObject != NULL &&
            memmgrIsPmaInitialized(pMemoryManager))
        {
            PMA *pRsvPma = pMemoryManager->pHeap->pPmaObject;
            /* phantom hole: [36G, 44G) — 8G die-aligned; covers the measured
             * 37-40G phantom activity band with ~1G/4G margin.
             * (narrowed 2026-08-07 from [32G,44G) 12G; costs 8G of 80G) */
            if (pmaIsPmaManaged(pRsvPma, 0x900000000ULL, 0xAFFFFFFFULL))
            {
                pmaSetBlockStateAttrib(pRsvPma, 0x900000000ULL, 0x200000000ULL,
                                       STATE_PIN, STATE_MASK);
                NV_PRINTF(LEVEL_ERROR,
                          "CMP_MEM_RSV: pinned [0x900000000,0xafffffff] in PMA (phantom guard)\\n");
            }
            else
            {
                NV_PRINTF(LEVEL_ERROR,
                          "CMP_MEM_RSV: phantom range not PMA-managed, guard inactive\\n");
            }
        }
    }
"""

# The MIG init path asserts the PMA is 100% free; our 128 MiB pin trips it
# and RmInitAdapter bails.  Teach the check to tolerate exactly our range.
ZERO_CHECK_OLD = (
    "        if (freeMem != totalMem)\n"
)
ZERO_CHECK_NEW = (
    "        /* cmpunlocker: tolerate the 10GB-SKU phantom-reserve pin */\n"
    "        NvU64 cmpPhantomRsv =\n"
    "            ((pGpu->idInfo.PCIDeviceID >> 16) == 0x2082) ? 0x200000000ULL : 0;\n"
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
