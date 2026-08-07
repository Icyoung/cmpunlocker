#!/usr/bin/env python3
"""Post-R3 override extension: after SEC2 PLM open loop, restore FEAT08/0c/10/14/28
to real A100-80GB values (dumped from a real A100 card).

R3's PLM-open loop writes 0xffffffff to open various PLMs; that leaves several
FEAT registers holding debug patterns (0x00888888, 0x002aaaaa, 0x00000233)
instead of the DevInit-programmed A100 values (0x00000101, 0x00100105,
0xef8ff100). 8GB SKU tolerates the pollution because 64 GiB unlock only exercises
the "lower" FBPA mappings. 10GB SKU targeting 80 GiB requires the higher-order
FEAT mappings to be correct.

Adds writes right after the SS0/SS1/CFG1/LMR block already patched in by
apply_ss_config4.py. Idempotent via CMP_MEM_FEAT_RESTORE marker.
"""
import pathlib
import sys

def main():
    if len(sys.argv) != 2:
        print("usage: apply_feat_restore.py <path/to/kernel_gsp.c>", file=sys.stderr)
        return 2
    src = pathlib.Path(sys.argv[1])
    txt = src.read_text(encoding="utf-8")
    if "CMP_MEM_FEAT_RESTORE" in txt:
        print("already applied; skipping")
        return 0

    # Anchor: end of the CONFIG4-forcing block that apply_ss_config4.py added.
    # That block ends with a closing } after NV_PRINTF of CMP_MEM_CONFIG4_FORCE.
    anchor = (
        '                NV_PRINTF(LEVEL_ERROR,\n'
        '                          "CMP_MEM_CONFIG4_FORCE: before=0x%08x target=0xc4028033 after=0x%08x\\n",\n'
        '                          config4Before, config4After);\n'
        '            }\n'
    )
    if anchor not in txt:
        print("CONFIG4 anchor not found — is ss-config4-override.patch applied first?", file=sys.stderr)
        return 1

    block = (
        '\n'
        '            /*\n'
        '             * FEAT register restoration: R3\'s PLM-open loop pollutes\n'
        '             * FEAT08/0c/10/14/28 with debug patterns.  Restore them\n'
        '             * to A100-80GB real values (dumped from real A100 card at\n'
        '             * PCI 0x20B5).  Both 8GB and 10GB SKUs get these writes.\n'
        '             * On 8GB SKU this is a no-op-equivalent (already stable).\n'
        '             * On 10GB SKU targeting 80 GiB this is our hypothesis for\n'
        '             * the missing higher-order FBPA mapping.\n'
        '             */\n'
        '            {\n'
        '                NvU32 feat08Before = GPU_REG_RD32(pGpu, 0x00823808U);\n'
        '                NvU32 feat0cBefore = GPU_REG_RD32(pGpu, 0x0082380cU);\n'
        '                NvU32 feat10Before = GPU_REG_RD32(pGpu, 0x00823810U);\n'
        '                NvU32 feat14Before = GPU_REG_RD32(pGpu, 0x00823814U);\n'
        '                NvU32 feat28Before = GPU_REG_RD32(pGpu, 0x00823828U);\n'
        '\n'
        '                GPU_REG_WR32(pGpu, 0x00823808U, 0x01000282U);\n'
        '                GPU_REG_WR32(pGpu, 0x0082380cU, 0x00000101U);\n'
        '                GPU_REG_WR32(pGpu, 0x00823810U, 0x00100105U);\n'
        '                GPU_REG_WR32(pGpu, 0x00823814U, 0xef8ff100U);\n'
        '                GPU_REG_WR32(pGpu, 0x00823828U, 0x00000007U);\n'
        '\n'
        '                NV_PRINTF(LEVEL_ERROR,\n'
        '                          "CMP_MEM_FEAT_RESTORE: "\n'
        '                          "FEAT08 %08x->%08x  FEAT0c %08x->%08x  FEAT10 %08x->%08x  "\n'
        '                          "FEAT14 %08x->%08x  FEAT28 %08x->%08x\\n",\n'
        '                          feat08Before, GPU_REG_RD32(pGpu, 0x00823808U),\n'
        '                          feat0cBefore, GPU_REG_RD32(pGpu, 0x0082380cU),\n'
        '                          feat10Before, GPU_REG_RD32(pGpu, 0x00823810U),\n'
        '                          feat14Before, GPU_REG_RD32(pGpu, 0x00823814U),\n'
        '                          feat28Before, GPU_REG_RD32(pGpu, 0x00823828U));\n'
        '            }\n'
    )
    txt = txt.replace(anchor, anchor + block, 1)
    src.write_text(txt, encoding="utf-8")
    print(f"inserted; new size: {len(txt)} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
