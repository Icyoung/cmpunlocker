#!/usr/bin/env python3
"""Apply P1c: extra booter run with 80G LMR in place, after post-write CFG1/LMR.

Idempotent: skips if CMP_P1C marker present. Requires R3 sec2-postbl-plm-ss-cfg.patch
to already be applied (looks for the SEC2_DEBUG POST-WRITE anchor).
"""
import pathlib
import sys

def main():
    if len(sys.argv) != 2:
        print("usage: apply_p1c.py <path/to/kernel_gsp.c>", file=sys.stderr)
        return 2
    src = pathlib.Path(sys.argv[1])
    txt = src.read_text(encoding="utf-8")
    if "CMP_P1C" in txt:
        print("already applied; skipping")
        return 0

    # Anchor: the log line that comes right after the post-PLM CFG1/LMR write.
    # This ends R3's post-PLM write block, right before _kgspCmpDumpGeometry.
    anchor = (
        '            NV_PRINTF(LEVEL_ERROR,\n'
        '                      "SEC2_DEBUG: POST-WRITE SS0=0x%08x SS1=0x%08x "\n'
        '                      "CFG1=0x%08x LMR=0x%08x (devId=0x%x)\\n",\n'
        '                      GPU_REG_RD32(pGpu, 0x0082381cU),\n'
        '                      GPU_REG_RD32(pGpu, 0x00823820U),\n'
        '                      GPU_REG_RD32(pGpu, 0x009a0204U),\n'
        '                      GPU_REG_RD32(pGpu, 0x00100ce0U),\n'
        '                      devId);\n'
        '        }\n'
    )
    if anchor not in txt:
        print("insertion anchor not found — R3 sec2 patch must be applied first", file=sys.stderr)
        return 1

    new_block = (
        '\n'
        '        //\n'
        '        // cmpunlocker P1C: run booter ONCE MORE after LMR/CFG1 have been\n'
        '        // updated to the 80 GiB encoding.  Rationale: R3\'s PLM-open loop\n'
        '        // ran booter ~22 times while LMR still encoded 10 GiB, and any\n'
        '        // GSP-adjacent hardware state that booter derives from LMR was\n'
        '        // therefore latched at 10 GiB.  With FBPA PLM now open we can\n'
        '        // re-run booter with the 80 GiB LMR in place, giving it one\n'
        '        // chance to re-latch based on the new value before the final\n'
        '        // GSP bootstrap.\n'
        '        //\n'
        '        // We refill the payload to target FBPA (already-open PLM) so\n'
        '        // the run is effectively a no-op on protection state, but the\n'
        '        // booter\'s init sequence still executes end-to-end.\n'
        '        //\n'
        '        {\n'
        '            NV_STATUS p1cRefill;\n'
        '            NV_STATUS p1cBoot;\n'
        '            NvU32 cfg1PostBooter, lmrPostBooter;\n'
        '\n'
        '            GPU_REG_WR32(pGpu, 0x001fa824U, wpr2Lo);\n'
        '            GPU_REG_WR32(pGpu, 0x001fa828U, wpr2Hi);\n'
        '\n'
        '            p1cRefill = kgspSec2PostblTimingRefillPayload(pGpu, pKernelGsp,\n'
        '                0x009a0148U, 0xffffffffU);\n'
        '            if (p1cRefill == NV_OK)\n'
        '            {\n'
        '                p1cBoot = kgspExecuteBooterLoad_HAL(pGpu, pKernelGsp,\n'
        '                    memdescGetPhysAddr(pKernelGsp->pWprMetaDescriptor, AT_GPU, 0));\n'
        '            }\n'
        '            else\n'
        '            {\n'
        '                p1cBoot = p1cRefill;\n'
        '            }\n'
        '\n'
        '            cfg1PostBooter = GPU_REG_RD32(pGpu, 0x009a0204U);\n'
        '            lmrPostBooter  = GPU_REG_RD32(pGpu, 0x00100ce0U);\n'
        '\n'
        '            NV_PRINTF(LEVEL_ERROR,\n'
        '                      "CMP_P1C: extra booter run refill=0x%x boot=0x%x "\n'
        '                      "cfg1_now=0x%08x lmr_now=0x%08x\\n",\n'
        '                      p1cRefill, p1cBoot, cfg1PostBooter, lmrPostBooter);\n'
        '        }\n'
    )

    txt = txt.replace(anchor, anchor + new_block, 1)
    src.write_text(txt, encoding="utf-8")
    print(f"inserted; new size: {len(txt)} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
