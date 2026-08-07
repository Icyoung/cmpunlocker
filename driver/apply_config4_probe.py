#!/usr/bin/env python3
"""SUPERSEDED — DO NOT USE. 2026-08-07 VBIOS byte-diff (B10) proved
0xc4030033 is the correct A100-80G 20-FBPA CONFIG4 value and 0xc4028033
is the 40G/16-FBPA fallback.  This probe writes the WRONG value via the
SEC2 payload path (which may succeed where CPU writes failed).  Kept only
as a record of the attempt.  Removed from build.sh PATCH_ORDER.

CONFIG4 write-path probe (10GB SKU / 0x2082 only, diagnostic).

B6 showed the CPU BCAST write to CONFIG4 (0x009a02a0) is silently rejected
even after R3's 11-PLM open loop.  This probe tries the two write paths that
have never been tested, and prints a readback after every attempt:

  (a) CPU per-instance writes: 0x00900000 + i*0x4000 + 0x2a0 for each live FBPA
  (b) SEC2 booter payload writes (secure context), per instance + BCAST

No workload is run; the boot log alone tells us which path (if any) can flip
CONFIG4 from 0xc4030033 to 0xc4028033.

Idempotent: marker "CMP_CFG4_PROBE" gates re-application.
Requires the ss-config4-override block (CMP_MEM_CONFIG4_FORCE) to be present.
"""
import pathlib
import sys

ANCHOR = (
    '                          config4Before, config4After);\n'
    '            }\n'
)

PROBE = """
            /*
             * CONFIG4 write-path probe: the CPU BCAST write above is
             * silently rejected.  Try the two untested write paths:
             *   (a) CPU per-instance writes (0x00900000 + i*0x4000 + 0x2a0)
             *   (b) SEC2 booter payload writes (secure context)
             * Every attempt is read back and printed; no workload runs.
             */
            if (devId == SEC2_POSTBL_TIMING_CMP_170HX_10GB_PCI_DEVICE_ID)
            {
                NvU32 probeFbpaDisable = GPU_REG_RD32(pGpu, CMP_MEM_FBPA_DISABLE);
                NvU32 probeIdx;

                /*
                 * Live BAR0 dump shows a SECOND FBPA PLM at 0x009a014c
                 * still at its stock value 0xffffff8f (R3's open loop only
                 * covers 0x009a0148), plus 0x009a0168 = 0xffffffcf.
                 * CONFIG4 is very likely gated by one of these.
                 * Open both via SEC2 payload before attempting CPU writes.
                 */
                {
                    NvU32 plm2;
                    for (plm2 = 0; plm2 < 2; plm2++)
                    {
                        NvU32 plmAddr = (plm2 == 0) ? 0x009a014cU : 0x009a0168U;
                        GPU_REG_WR32(pGpu, 0x001fa824U, wpr2Lo);
                        GPU_REG_WR32(pGpu, 0x001fa828U, wpr2Hi);
                        plmStatus = kgspSec2PostblTimingRefillPayload(pGpu, pKernelGsp,
                                                                      plmAddr, 0xffffffffU);
                        if (plmStatus == NV_OK)
                            plmStatus = kgspExecuteBooterLoad_HAL(pGpu, pKernelGsp,
                                memdescGetPhysAddr(pKernelGsp->pWprMetaDescriptor, AT_GPU, 0));
                        NV_PRINTF(LEVEL_ERROR,
                                  "CMP_CFG4_PROBE: open PLM 0x%08x status=0x%x readback=0x%08x\\n",
                                  plmAddr, plmStatus, GPU_REG_RD32(pGpu, plmAddr));
                    }
                }

                for (probeIdx = 0; probeIdx < CMP_MEM_FBPA_COUNT; probeIdx++)
                {
                    NvU32 instAddr = CMP_MEM_FBPA_BASE
                                     + probeIdx * CMP_MEM_FBPA_STRIDE
                                     + CMP_MEM_FBPA_CONFIG4_OFFSET;
                    NvU32 rd;
                    if ((probeFbpaDisable & (1U << probeIdx)) != 0)
                        continue;
                    GPU_REG_WR32(pGpu, instAddr, 0xc4028033U);
                    rd = GPU_REG_RD32(pGpu, instAddr);
                    NV_PRINTF(LEVEL_ERROR,
                              "CMP_CFG4_PROBE: cpu inst=%u addr=0x%08x readback=0x%08x\\n",
                              probeIdx, instAddr, rd);
                }
                NV_PRINTF(LEVEL_ERROR,
                          "CMP_CFG4_PROBE: cpu bcast readback=0x%08x\\n",
                          GPU_REG_RD32(pGpu, CMP_MEM_CONFIG4_BCAST));

                for (probeIdx = 0; probeIdx < CMP_MEM_FBPA_COUNT + 1; probeIdx++)
                {
                    NvU32 instAddr = (probeIdx < CMP_MEM_FBPA_COUNT)
                        ? (CMP_MEM_FBPA_BASE + probeIdx * CMP_MEM_FBPA_STRIDE
                           + CMP_MEM_FBPA_CONFIG4_OFFSET)
                        : CMP_MEM_CONFIG4_BCAST;
                    NvU32 rd;
                    if (probeIdx < CMP_MEM_FBPA_COUNT &&
                        (probeFbpaDisable & (1U << probeIdx)) != 0)
                        continue;
                    GPU_REG_WR32(pGpu, 0x001fa824U, wpr2Lo);
                    GPU_REG_WR32(pGpu, 0x001fa828U, wpr2Hi);
                    plmStatus = kgspSec2PostblTimingRefillPayload(pGpu, pKernelGsp,
                                                                  instAddr, 0xc4028033U);
                    if (plmStatus != NV_OK)
                    {
                        NV_PRINTF(LEVEL_ERROR,
                                  "CMP_CFG4_PROBE: sec2 idx=%u refill failed 0x%x\\n",
                                  probeIdx, plmStatus);
                        continue;
                    }
                    plmStatus = kgspExecuteBooterLoad_HAL(pGpu, pKernelGsp,
                        memdescGetPhysAddr(pKernelGsp->pWprMetaDescriptor, AT_GPU, 0));
                    rd = GPU_REG_RD32(pGpu, instAddr);
                    NV_PRINTF(LEVEL_ERROR,
                              "CMP_CFG4_PROBE: sec2 idx=%u addr=0x%08x status=0x%x readback=0x%08x\\n",
                              probeIdx, instAddr, plmStatus, rd);
                }

                GPU_REG_WR32(pGpu, 0x001fa824U, wpr2Lo);
                GPU_REG_WR32(pGpu, 0x001fa828U, wpr2Hi);
            }
"""


def main():
    if len(sys.argv) != 2:
        print("usage: apply_config4_probe.py <path/to/kernel_gsp.c>", file=sys.stderr)
        return 2
    src = pathlib.Path(sys.argv[1])
    txt = src.read_text(encoding="utf-8")
    if "CMP_CFG4_PROBE" in txt:
        print("already applied; skipping")
        return 0
    if "CMP_MEM_CONFIG4_FORCE" not in txt:
        print("CONFIG4_FORCE anchor missing — is ss-config4-override applied?", file=sys.stderr)
        return 1
    if txt.count(ANCHOR) != 1:
        print(f"anchor not unique ({txt.count(ANCHOR)} matches)", file=sys.stderr)
        return 1
    txt = txt.replace(ANCHOR, ANCHOR + PROBE, 1)
    src.write_text(txt, encoding="utf-8")
    print(f"inserted; new size: {len(txt)} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
