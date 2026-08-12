#!/usr/bin/env python3
"""Apply the P1a early-LMR-write insertion to a build tree's kernel_gsp.c.

Idempotent: skips if the CMP_MEM_EARLY_WRITE marker is already present.
"""
import pathlib
import sys

def main():
    if len(sys.argv) != 2:
        print("usage: apply_p1a.py <path/to/kernel_gsp.c>", file=sys.stderr)
        return 2
    src = pathlib.Path(sys.argv[1])
    txt = src.read_text(encoding="utf-8")
    if "CMP_MEM_EARLY_WRITE" in txt:
        print("already applied; skipping")
        return 0
    insert_after = (
        '    if (kgspIsWpr2Up_HAL(pGpu, pKernelGsp) &&\n'
        '        (!pGpu->getProperty(pGpu, PDB_PROP_GPU_PREINITIALIZED_WPR_REGION)))\n'
        '    {\n'
        '        NV_PRINTF(LEVEL_WARNING,\n'
        '                  "WPR2 already up before GSP boot; continuing for recovery\\n");\n'
        '    }\n'
    )
    if insert_after not in txt:
        print("insertion anchor not found — has the R3 sec2 patch been applied?", file=sys.stderr)
        return 1
    new_block = (
        '\n'
        '    //\n'
        '    // cmpunlocker P1a: EARLY LMR/CFG1 write so kgspPopulateWprMeta_HAL reads\n'
        '    // the unlocked fbSize rather than the stock 10 GiB.  Without this,\n'
        '    // WPR meta seen by the booter loop encodes only 10 GiB; even though R3\n'
        '    // re-populates to the unlocked size after the booter loop, internal GSP\n'
        '    // tables built during booter+bootstrap remain sized for 10 GiB and any\n'
        '    // access above ~40 GiB crashes GSP with Xid 1 illegal instruction.\n'
        '    //\n'
        '    // The 2082 targets below are SHIPPED DEFAULTS for the stable 40 GiB\n'
        '    // profile; apply_profile.py rewrites them per CMPUNLOCKER_CARD_PROFILE\n'
        '    // (40 GiB stable / 80 GiB experimental), same as the post-PLM block.\n'
        '    //\n'
        '    // These writes may fail silently if the required PLMs are still closed at\n'
        '    // this point; if so the values will be re-written by the existing PLM open\n'
        '    // loop later.  We log both attempts so we can compare via dmesg.\n'
        '    //\n'
        '    if (_kgspSec2PostblTimingEnabled(pGpu))\n'
        '    {\n'
        '        NvU32 devId = pGpu->idInfo.PCIDeviceID >> 16;\n'
        '        NvU32 cfg1Target;\n'
        '        NvU32 lmrTarget;\n'
        '        NvU32 cfg1Before, lmrBefore, cfg1After, lmrAfter;\n'
        '\n'
        '        if (devId == SEC2_POSTBL_TIMING_CMP_170HX_8GB_PCI_DEVICE_ID)\n'
        '        {\n'
        '            cfg1Target = 0x02779000U;\n'
        '            lmrTarget  = 0x0000020BU;\n'
        '        }\n'
        '        else\n'
        '        {\n'
        '            cfg1Target = 0x02669000U;\n'
        '            lmrTarget  = 0x0000028AU;\n'
        '        }\n'
        '\n'
        '        cfg1Before = GPU_REG_RD32(pGpu, CMP_MEM_CFG1_BCAST);\n'
        '        lmrBefore  = GPU_REG_RD32(pGpu, CMP_MEM_LMR);\n'
        '        GPU_REG_WR32(pGpu, CMP_MEM_CFG1_BCAST, cfg1Target);\n'
        '        GPU_REG_WR32(pGpu, CMP_MEM_LMR,        lmrTarget);\n'
        '        cfg1After  = GPU_REG_RD32(pGpu, CMP_MEM_CFG1_BCAST);\n'
        '        lmrAfter   = GPU_REG_RD32(pGpu, CMP_MEM_LMR);\n'
        '\n'
        '        NV_PRINTF(LEVEL_ERROR,\n'
        '                  "CMP_MEM_EARLY_WRITE: devId=0x%x cfg1 before=0x%08x target=0x%08x "\n'
        '                  "after=0x%08x lmr before=0x%08x target=0x%08x after=0x%08x\\n",\n'
        '                  devId, cfg1Before, cfg1Target, cfg1After,\n'
        '                  lmrBefore, lmrTarget, lmrAfter);\n'
        '    }\n'
    )
    txt = txt.replace(insert_after, insert_after + new_block, 1)
    src.write_text(txt, encoding="utf-8")
    print(f"inserted; new size: {len(txt)} bytes")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
