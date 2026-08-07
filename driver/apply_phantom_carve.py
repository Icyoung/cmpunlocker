#!/usr/bin/env python3
"""Phantom carve-out for the 10GB SKU 80G profile.

2026-08-07 drip experiments localized a GSP-killing phantom structure to
alloc-VA [39G+704M, 39G+768M) (identical across 60G and 78G allocs =>
fixed physical address ~0xA00010000 = 40GiB+64KiB).  The structure lives
inside the user heap region and is handed out to user allocations;
overwriting it wedges the memory subsystem / kills GSP.

This patch splits the GSP fbRegionInfo user region around
[0xA0000000, 0xA8000000) (40GiB, +128MiB) so the PMA never allocates
those pages to users.  128 MiB of margin around the observed window.

Idempotent: marker "CMP_CARVE" gates re-application.
Requires the R3 sec2 static-info override block (static-info AFTER) to be
present in kernel_gsp.c.
"""
import pathlib
import sys

ANCHOR = (
    '                NV_PRINTF(LEVEL_ERROR,\n'
    '                          "SEC2_DEBUG: static-info AFTER: fb_length=0x%llx last_limit=0x%llx\\n",\n'
    '                          pGSCI->fb_length, pLastRegion->limit);\n'
    '            }\n'
)

BLOCK = """
            /*
             * cmpunlocker phantom carve-out (10GB SKU, 80G profile only):
             * drip experiments (2026-08-07) localized a GSP-killing
             * structure to alloc-VA [39G+704M, 39G+768M), identical across
             * 60G and 78G allocs => fixed PA ~0xA00010000 (40GiB+64KiB).
             * Split the user region around [0xA0000000, 0xA8000000) so
             * the PMA never hands those pages to user allocations.
             */
            if (devId == SEC2_POSTBL_TIMING_CMP_170HX_10GB_PCI_DEVICE_ID &&
                targetFbBytes == 0x0000001400000000ULL)
            {
                const NvU64 holeBase = 0x0000000A00000000ULL;
                const NvU64 holeEnd  = 0x0000000A07FFFFFFULL; /* +128 MiB */
                NvU32 ri;

                for (ri = 0; ri < numRegions; ri++)
                {
                    NV2080_CTRL_CMD_FB_GET_FB_REGION_FB_REGION_INFO *pR =
                        &pFbRegionInfoParams->fbRegion[ri];

                    if (pR->base <= holeBase && pR->limit >= holeEnd &&
                        pR->reserved == 0)
                    {
                        NvU64 origLimit = pR->limit;
                        NvU32 tj;

                        if (numRegions + 2 >
                            NV2080_CTRL_CMD_FB_GET_FB_REGION_INFO_MAX_ENTRIES)
                        {
                            NV_PRINTF(LEVEL_ERROR,
                                      "CMP_CARVE: no room to split region\\n");
                            break;
                        }

                        /* shift tail [ri+1, numRegions) up by two slots */
                        for (tj = numRegions; tj-- > ri + 1;)
                            pFbRegionInfoParams->fbRegion[tj + 2] =
                                pFbRegionInfoParams->fbRegion[tj];

                        pFbRegionInfoParams->fbRegion[ri + 2] = *pR;
                        pFbRegionInfoParams->fbRegion[ri + 1] = *pR;

                        /* ri:   [base, holeBase-1]  user (unchanged props) */
                        pFbRegionInfoParams->fbRegion[ri].limit = holeBase - 1;

                        /* ri+1: [holeBase, holeEnd] reserved hole */
                        pFbRegionInfoParams->fbRegion[ri + 1].base = holeBase;
                        pFbRegionInfoParams->fbRegion[ri + 1].limit = holeEnd;
                        pFbRegionInfoParams->fbRegion[ri + 1].reserved =
                            holeEnd - holeBase + 1;
                        pFbRegionInfoParams->fbRegion[ri + 1].supportCompressed =
                            NV_FALSE;
                        pFbRegionInfoParams->fbRegion[ri + 1].supportISO =
                            NV_FALSE;
                        pFbRegionInfoParams->fbRegion[ri + 1].performance = 0;

                        /* ri+2: [holeEnd+1, origLimit] user */
                        pFbRegionInfoParams->fbRegion[ri + 2].base = holeEnd + 1;
                        pFbRegionInfoParams->fbRegion[ri + 2].limit = origLimit;

                        numRegions += 2;
                        pFbRegionInfoParams->numFBRegions = numRegions;
                        NV_PRINTF(LEVEL_ERROR,
                                  "CMP_CARVE: split user region %u around "
                                  "[0x%llx,0x%llx], numRegions=%u\\n",
                                  ri, holeBase, holeEnd, numRegions);
                        break;
                    }
                }
                if (ri == numRegions)
                    NV_PRINTF(LEVEL_ERROR,
                              "CMP_CARVE: no user region covers the hole!\\n");
            }
"""


def main():
    if len(sys.argv) != 2:
        print("usage: apply_phantom_carve.py <path/to/kernel_gsp.c>", file=sys.stderr)
        return 2
    src = pathlib.Path(sys.argv[1])
    txt = src.read_text(encoding="utf-8")
    if "CMP_CARVE" in txt:
        print("already applied; skipping")
        return 0
    if "static-info AFTER" not in txt:
        print("static-info override block missing — is the sec2 patch applied?", file=sys.stderr)
        return 1
    if txt.count(ANCHOR) != 1:
        print(f"anchor not unique ({txt.count(ANCHOR)} matches)", file=sys.stderr)
        return 1
    txt = txt.replace(ANCHOR, ANCHOR + BLOCK, 1)
    src.write_text(txt, encoding="utf-8")
    print(f"inserted; new size: {len(txt)} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
