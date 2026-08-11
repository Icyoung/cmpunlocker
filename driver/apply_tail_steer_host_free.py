#!/usr/bin/env python3
"""Host-side undo for tail-steer: keep one contiguous ~80G free region.

fbRegionInfo in GSCI is consumed by memmgrInitBaseFbRegions_FWCLIENT for the
HOST heap/PMA.  P1's GSCI split must not leave host with two free spans —
that creates an extra reserved island and trips Ampere's
"More than two discontiguous rsvd regions" assert in memmgrSetPartitionableMem.

P1c: reopen the synthetic low reserved span, then merge it with the adjacent
free-tail into a single free region (same geometry as pre-steer host map).

Idempotent via CMP_TAIL_HOST_FREE.  Apply after patches extract.
"""
from __future__ import annotations

import pathlib
import sys

MARKER = "CMP_TAIL_HOST_FREE"

ANCHOR = (
    '    // Dump some stats, region table is dumped in memsysStateLoad\n'
    '    NV_PRINTF(LEVEL_INFO, "FB Memory from Static info:\\n");\n'
    '    NV_PRINTF(LEVEL_INFO, "Reserved Memory=0x%llx, Usable Memory=0x%llx\\n",\n'
    '              pMemoryManager->Ram.reservedMemSize, pMemoryManager->Ram.fbUsableMemSize);\n'
    '    NV_PRINTF(LEVEL_INFO, "fbTotalMemSizeMb=0x%llx, fbAddrSpaceSizeMb=0x%llx\\n",\n'
    '              pMemoryManager->Ram.fbTotalMemSizeMb, pMemoryManager->Ram.fbAddrSpaceSizeMb);\n'
    '\n'
    '    return NV_OK;\n'
    '}\n'
)

BLOCK = r'''
    /*
     * cmpunlocker: host keeps one large free region (P1c).
     * Re-open CMP_TAIL_STEER's synthetic low reserved span, then merge it
     * with the adjacent free-tail so CreateHeap sees a single contiguous
     * client region (avoids >2 discontiguous rsvd islands on Ampere).
     */
    {
        NvU32 hostFreeEn = 0;
        NvU32 hostDevId = pGpu->idInfo.PCIDeviceID >> 16;
        (void)osReadRegistryDword(pGpu, "RMCmpTailSteer", &hostFreeEn);
        if (hostDevId == 0x2082 && hostFreeEn != 0)
        {
            NvU32 hi;
            NvU32 restored = 0;
            NvU32 merged = 0;
            for (hi = 0; hi < pMemoryManager->Ram.numFBRegions; hi++)
            {
                FB_REGION_DESCRIPTOR *pR = &pMemoryManager->Ram.fbRegion[hi];
                NvU64 span;
                if (!pR->bRsvdRegion)
                    continue;
                if (pR->regionTag != NV2080_FB_REGION_TAG_GSP_RM_RESERVED)
                    continue;
                if (pR->performance != 0)
                    continue;
                if (pR->limit < pR->base)
                    continue;
                span = pR->limit - pR->base + 1;
                if (span < 0x1000000000ULL)
                    continue;
                if (pR->rsvdSize != span)
                    continue;

                pMemoryManager->Ram.reservedMemSize -= pR->rsvdSize;
                pR->bRsvdRegion = NV_FALSE;
                pR->rsvdSize = 0;
                pR->bSupportCompressed = NV_TRUE;
                pR->bSupportISO = NV_TRUE;
                pR->performance = 20;
                pR->regionTag = NV2080_FB_REGION_TAG_NONE;
                pMemoryManager->Ram.fbUsableMemSize += span;
                restored++;
                NV_PRINTF(LEVEL_ERROR,
                          "CMP_TAIL_HOST_FREE: reopened idx=%u "
                          "[0x%llx,0x%llx] span=0x%llx for host PMA\n",
                          hi, pR->base, pR->limit, span);

                /* Merge adjacent free-tail (steer left it as the next entry). */
                if (hi + 1 < pMemoryManager->Ram.numFBRegions)
                {
                    FB_REGION_DESCRIPTOR *pN =
                        &pMemoryManager->Ram.fbRegion[hi + 1];
                    if (!pN->bRsvdRegion &&
                        pN->base == pR->limit + 1 &&
                        pN->limit >= pN->base)
                    {
                        NvU64 oldLimit = pR->limit;
                        NvU32 kj;
                        pR->limit = pN->limit;
                        /* usable already counted both spans; no size change */
                        for (kj = hi + 1;
                             kj + 1 < pMemoryManager->Ram.numFBRegions;
                             kj++)
                        {
                            pMemoryManager->Ram.fbRegion[kj] =
                                pMemoryManager->Ram.fbRegion[kj + 1];
                        }
                        pMemoryManager->Ram.numFBRegions--;
                        merged++;
                        NV_PRINTF(LEVEL_ERROR,
                                  "CMP_TAIL_HOST_FREE: merged free-tail "
                                  "into idx=%u -> [0x%llx,0x%llx] "
                                  "(was limit 0x%llx) numRegions=%u\n",
                                  hi, pR->base, pR->limit, oldLimit,
                                  pMemoryManager->Ram.numFBRegions);
                    }
                }
            }
            NV_PRINTF(LEVEL_ERROR,
                      "CMP_TAIL_HOST_FREE: restored=%u merged=%u "
                      "usable=0x%llx reserved=0x%llx numRegions=%u\n",
                      restored, merged,
                      pMemoryManager->Ram.fbUsableMemSize,
                      pMemoryManager->Ram.reservedMemSize,
                      pMemoryManager->Ram.numFBRegions);
        }
    }

    // Dump some stats, region table is dumped in memsysStateLoad
    NV_PRINTF(LEVEL_INFO, "FB Memory from Static info:\n");
    NV_PRINTF(LEVEL_INFO, "Reserved Memory=0x%llx, Usable Memory=0x%llx\n",
              pMemoryManager->Ram.reservedMemSize, pMemoryManager->Ram.fbUsableMemSize);
    NV_PRINTF(LEVEL_INFO, "fbTotalMemSizeMb=0x%llx, fbAddrSpaceSizeMb=0x%llx\n",
              pMemoryManager->Ram.fbTotalMemSizeMb, pMemoryManager->Ram.fbAddrSpaceSizeMb);

    return NV_OK;
}
'''


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <mem_mgr_gsp_client.c>", file=sys.stderr)
        return 2
    path = pathlib.Path(sys.argv[1])
    text = path.read_text(encoding="utf-8")

    # Force upgrade if an older host-free block lacks the merge step.
    if MARKER in text and "merged=%u" not in text:
        start = text.find("    /*\n     * cmpunlocker: host keeps")
        if start < 0:
            start = text.find("CMP_TAIL_HOST_FREE")
            # fall back: strip from comment above the marker printf's block
        if start >= 0:
            end = text.find("    // Dump some stats, region table is dumped in memsysStateLoad\n", start)
            if end > start:
                text = text[:start] + text[end:]
                # drop marker by removing old block; fall through to inject
            else:
                print("old host-free block could not be stripped", file=sys.stderr)
                return 1
        else:
            print("old host-free present but unstrippable", file=sys.stderr)
            return 1

    if MARKER in text and "merged=%u" in text:
        print(f"{path}: already applied (with merge)")
        return 0

    if text.count(ANCHOR) != 1:
        print(f"anchor not unique ({text.count(ANCHOR)})", file=sys.stderr)
        return 1

    if '#include "os/os.h"' not in text:
        needle = '#include "gpu/mem_mgr/mem_mgr.h"\n'
        if text.count(needle) != 1:
            print("mem_mgr.h include anchor missing", file=sys.stderr)
            return 1
        text = text.replace(needle, needle + '#include "os/os.h"\n', 1)

    text = text.replace(ANCHOR, BLOCK, 1)
    path.write_text(text, encoding="utf-8")
    print(f"{path}: injected CMP_TAIL_HOST_FREE (reopen+merge)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
