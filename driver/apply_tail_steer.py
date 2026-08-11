#!/usr/bin/env python3
"""Tail-steer probe: push GSP page-table PMA free space into a top corridor.

Live layout (80G unlock) has a large user region with reserved==0 spanning
~320MiB..~79.1G (idx=1). GSP PT pools land inside it (~37-40G).  Splitting
that map at boot previously killed GSP (CMP_CARVE); this probe is gated by
RMCmpTailSteer (default OFF) and only runs when explicitly enabled.

Strategy (P1 / P1b):
  Split the largest reserved==0 region at tail_base = limit+1-tail_size:
    [base, tail_base-1]  → reserved = full span  (GSCI view)
    [tail_base, limit]   → reserved = 0, tag = GSP_RM_RESERVED_HEAP
  Host PMA must NOT starve: apply_tail_steer_host_free.py reopens the
  synthetic low span after InitBaseFbRegions copies GSCI.  Optional
  RMCmpTailPin=1 (apply_tail_steer_pin.py) pins the tail corridor later.

Idempotent via CMP_TAIL_STEER marker.  Hooks after CMP_MEM_GSP_REGION dump.
"""
from __future__ import annotations

import pathlib
import sys

MARKER = "CMP_TAIL_STEER"

ANCHOR = (
    '                }\n'
    '            }\n'
    '\n'
    '            _kgspCmpDumpGeometry(pGpu, "post_gsp_static_info");\n'
)

BLOCK = r'''
            /*
             * cmpunlocker tail-steer (P1): squeeze GSP PMA free space into a
             * corridor just below the top of the big user region so PT pools
             * preferentially land at the FB tail.  OFF by default —
             * NVreg_RegistryDwords="RMCmpTailSteer=1" enables.
             * Optional: RMCmpTailSizeMB=<mb> (default 4096).
             */
            {
                NvU32 tailEnable = 0;
                NvU32 tailSizeMb = 4096;
                (void)osReadRegistryDword(pGpu, "RMCmpTailSteer", &tailEnable);
                (void)osReadRegistryDword(pGpu, "RMCmpTailSizeMB", &tailSizeMb);
                if (tailSizeMb < 512)
                    tailSizeMb = 512;
                if (tailSizeMb > 16384)
                    tailSizeMb = 16384;

                if (devId == SEC2_POSTBL_TIMING_CMP_170HX_10GB_PCI_DEVICE_ID &&
                    targetFbBytes == 0x0000001400000000ULL &&
                    tailEnable != 0)
                {
                    NvU64 tailSize = ((NvU64)tailSizeMb) << 20;
                    NvU32 ri;
                    NvS32 bigIdx = -1;
                    NvU64 bigSpan = 0;

                    numRegions = pFbRegionInfoParams->numFBRegions;
                    for (ri = 0; ri < numRegions; ri++)
                    {
                        NV2080_CTRL_CMD_FB_GET_FB_REGION_FB_REGION_INFO *pR =
                            &pFbRegionInfoParams->fbRegion[ri];
                        NvU64 span;
                        if (pR->reserved != 0)
                            continue;
                        if (pR->limit < pR->base)
                            continue;
                        span = pR->limit - pR->base + 1;
                        if (span > bigSpan)
                        {
                            bigSpan = span;
                            bigIdx = (NvS32)ri;
                        }
                    }

                    NV_PRINTF(LEVEL_ERROR,
                              "CMP_TAIL_STEER: enable=1 tailSizeMb=%u bigIdx=%d "
                              "bigSpan=0x%llx numRegions=%u\n",
                              tailSizeMb, bigIdx, bigSpan, numRegions);

                    if (bigIdx >= 0 &&
                        bigSpan > (tailSize + 0x100000000ULL) &&
                        numRegions + 1 <=
                            NV2080_CTRL_CMD_FB_GET_FB_REGION_INFO_MAX_ENTRIES)
                    {
                        NV2080_CTRL_CMD_FB_GET_FB_REGION_FB_REGION_INFO *pBig =
                            &pFbRegionInfoParams->fbRegion[bigIdx];
                        NvU64 origBase = pBig->base;
                        NvU64 origLimit = pBig->limit;
                        NvU64 tailBase = origLimit - tailSize + 1;
                        NvU32 tj;

                        if (tailBase > origBase + 0x100000000ULL)
                        {
                            for (tj = numRegions; tj-- > (NvU32)bigIdx + 1;)
                                pFbRegionInfoParams->fbRegion[tj + 1] =
                                    pFbRegionInfoParams->fbRegion[tj];

                            pFbRegionInfoParams->fbRegion[bigIdx + 1] = *pBig;

                            /* low: mark fully reserved so GSP PMA should skip */
                            pBig->limit = tailBase - 1;
                            pBig->reserved = pBig->limit - pBig->base + 1;
                            pBig->supportCompressed = NV_FALSE;
                            pBig->supportISO = NV_FALSE;
                            pBig->performance = 0;
                            pBig->regionTag = NV2080_FB_REGION_TAG_GSP_RM_RESERVED;

                            /* high/tail: keep allocatable for GSP PMA */
                            {
                                NV2080_CTRL_CMD_FB_GET_FB_REGION_FB_REGION_INFO *pTail =
                                    &pFbRegionInfoParams->fbRegion[bigIdx + 1];
                                pTail->base = tailBase;
                                pTail->limit = origLimit;
                                pTail->reserved = 0;
                                pTail->supportCompressed = NV_TRUE;
                                pTail->supportISO = NV_TRUE;
                                pTail->performance = 20;
                                pTail->regionTag =
                                    NV2080_FB_REGION_TAG_GSP_RM_RESERVED_HEAP;
                            }

                            numRegions += 1;
                            pFbRegionInfoParams->numFBRegions = numRegions;

                            NV_PRINTF(LEVEL_ERROR,
                                      "CMP_TAIL_STEER: split big region "
                                      "[0x%llx,0x%llx] -> reserved[0x%llx,0x%llx] "
                                      "+ free-tail[0x%llx,0x%llx] numRegions=%u\n",
                                      origBase, origLimit,
                                      origBase, tailBase - 1,
                                      tailBase, origLimit, numRegions);

                            for (ri = 0; ri < numRegions; ri++)
                            {
                                NV2080_CTRL_CMD_FB_GET_FB_REGION_FB_REGION_INFO *pR =
                                    &pFbRegionInfoParams->fbRegion[ri];
                                NV_PRINTF(LEVEL_ERROR,
                                          "CMP_TAIL_STEER: idx=%u base=0x%llx "
                                          "limit=0x%llx reserved=0x%llx tag=%u "
                                          "perf=%u\n",
                                          ri, pR->base, pR->limit, pR->reserved,
                                          (NvU32)pR->regionTag, pR->performance);
                            }
                        }
                        else
                        {
                            NV_PRINTF(LEVEL_ERROR,
                                      "CMP_TAIL_STEER: tailBase too low "
                                      "(0x%llx), skip\n",
                                      tailBase);
                        }
                    }
                    else
                    {
                        NV_PRINTF(LEVEL_ERROR,
                                  "CMP_TAIL_STEER: cannot split (bigIdx=%d "
                                  "span=0x%llx regions=%u)\n",
                                  bigIdx, bigSpan, numRegions);
                    }
                }
                else if (devId == SEC2_POSTBL_TIMING_CMP_170HX_10GB_PCI_DEVICE_ID)
                {
                    NV_PRINTF(LEVEL_ERROR,
                              "CMP_TAIL_STEER: idle (RMCmpTailSteer=%u)\n",
                              tailEnable);
                }
            }

'''


# P0: include regionTag in the existing GSP region dump.
DUMP_OLD = (
    '                    NV_PRINTF(LEVEL_ERROR,\n'
    '                              "CMP_MEM_GSP_REGION: idx=%u base=0x%llx limit=0x%llx "\n'
    '                              "reserved=0x%llx compressed=%u iso=%u performance=%u\\n",\n'
    '                              regionIdx, pRegion->base, pRegion->limit,\n'
    '                              pRegion->reserved, pRegion->supportCompressed,\n'
    '                              pRegion->supportISO, pRegion->performance);\n'
)

DUMP_NEW = (
    '                    NV_PRINTF(LEVEL_ERROR,\n'
    '                              "CMP_MEM_GSP_REGION: idx=%u base=0x%llx limit=0x%llx "\n'
    '                              "reserved=0x%llx compressed=%u iso=%u performance=%u "\n'
    '                              "tag=%u\\n",\n'
    '                              regionIdx, pRegion->base, pRegion->limit,\n'
    '                              pRegion->reserved, pRegion->supportCompressed,\n'
    '                              pRegion->supportISO, pRegion->performance,\n'
    '                              (NvU32)pRegion->regionTag);\n'
)


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <kernel_gsp.c>", file=sys.stderr)
        return 2
    path = pathlib.Path(sys.argv[1])
    text = path.read_text(encoding="utf-8")
    changed = False

    if "tag=%u" not in text or "pRegion->regionTag" not in text:
        if text.count(DUMP_OLD) != 1:
            print(f"region dump anchor not unique ({text.count(DUMP_OLD)})", file=sys.stderr)
            return 1
        text = text.replace(DUMP_OLD, DUMP_NEW, 1)
        changed = True

    if MARKER not in text:
        if text.count(ANCHOR) != 1:
            print(f"steer anchor not unique ({text.count(ANCHOR)})", file=sys.stderr)
            return 1
        text = text.replace(ANCHOR, ANCHOR + BLOCK, 1)
        changed = True

    if not changed:
        print(f"{path}: already applied")
        return 0
    path.write_text(text, encoding="utf-8")
    print(f"{path}: injected CMP_TAIL_STEER (+ regionTag dump)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
