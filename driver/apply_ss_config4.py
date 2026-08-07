#!/usr/bin/env python3
"""Post-R3 override:
  1. SS0/SS1 written by SEC2 hack use debug pattern 0x88888888/0x00000008 —
     replace with A100-80GB real values 0x00112011/0x00000002.
  2. For 10GB SKU (0x2082), also write CONFIG4_BCAST to the 8GB SKU's value
     (0xc4028033) to see if the bit-15/16 difference is the strap-4
     row-addressing lever that pins the 10GB unlock to ~40 GiB.

Idempotent: marker "CMP_MEM_CONFIG4_FORCE" gates re-application.
Requires R3 sec2-postbl-plm-ss-cfg.patch to already be applied.
"""
import pathlib
import sys

def main():
    if len(sys.argv) != 2:
        print("usage: apply_ss_config4.py <path/to/kernel_gsp.c>", file=sys.stderr)
        return 2
    src = pathlib.Path(sys.argv[1])
    txt = src.read_text(encoding="utf-8")
    if "CMP_MEM_CONFIG4_FORCE" in txt:
        print("already applied; skipping")
        return 0

    # Step 1: replace SS0/SS1 debug writes with A100-80GB real values.
    old_ss = (
        '            GPU_REG_WR32(pGpu, 0x0082381cU, 0x88888888U);\n'
        '            GPU_REG_WR32(pGpu, 0x00823820U, 0x00000008U);\n'
    )
    if old_ss not in txt:
        print("SS0/SS1 anchor not found — is sec2 patch applied?", file=sys.stderr)
        return 1
    new_ss = (
        '            /*\n'
        '             * SS0/SS1 real values dumped from an A100-80GB card:\n'
        '             *   SS0 (0x0082381c) = 0x00112011  (was 0x88888888 debug)\n'
        '             *   SS1 (0x00823820) = 0x00000002  (was 0x00000008 debug)\n'
        '             */\n'
        '            GPU_REG_WR32(pGpu, 0x0082381cU, 0x00112011U);\n'
        '            GPU_REG_WR32(pGpu, 0x00823820U, 0x00000002U);\n'
    )
    txt = txt.replace(old_ss, new_ss, 1)

    # Step 2: after CFG1/LMR writes, force CONFIG4 for 10GB SKU only.
    anchor = (
        '            GPU_REG_WR32(pGpu, 0x009a0204U, cfg1Value);\n'
        '            GPU_REG_WR32(pGpu, 0x00100ce0U, lmrValue);\n'
    )
    if anchor not in txt:
        print("CFG1/LMR anchor not found", file=sys.stderr)
        return 1
    block = (
        '\n'
        '            /*\n'
        '             * CONFIG4 experiment: 8GB SKU (0x20C2) unlocking to\n'
        '             * 64GB cleanly reads CONFIG4_BCAST = 0xc4028033.\n'
        '             * 10GB SKU (0x2082) reads 0xc4030033 (bits 15/16 differ).\n'
        '             * Force 8GB-flavored value on 10GB card only.\n'
        '             */\n'
        '            if (devId == SEC2_POSTBL_TIMING_CMP_170HX_10GB_PCI_DEVICE_ID)\n'
        '            {\n'
        '                NvU32 config4Before = GPU_REG_RD32(pGpu, CMP_MEM_CONFIG4_BCAST);\n'
        '                GPU_REG_WR32(pGpu, CMP_MEM_CONFIG4_BCAST, 0xc4028033U);\n'
        '                NvU32 config4After = GPU_REG_RD32(pGpu, CMP_MEM_CONFIG4_BCAST);\n'
        '                NV_PRINTF(LEVEL_ERROR,\n'
        '                          "CMP_MEM_CONFIG4_FORCE: before=0x%08x target=0xc4028033 after=0x%08x\\n",\n'
        '                          config4Before, config4After);\n'
        '            }\n'
    )
    txt = txt.replace(anchor, anchor + block, 1)

    src.write_text(txt, encoding="utf-8")
    print(f"inserted; new size: {len(txt)} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
