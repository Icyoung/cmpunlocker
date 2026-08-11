#!/usr/bin/env python3
"""Post-PLM Booter OS patch — final normal BooterLoad only (PLM loop unaffected).

Regkey: RMCmpBooterSkipApp=1

Patches pKernelGsp->pBooterLoadUcode->ucodeBootDirect.pImage immediately before
kgspBootstrap_TU102's normal BooterLoad (after PLM completes):

  image+0x7b: 0x31 -> 0x00   theater mbox success (was intentional 0x31 fail)
  image+0xcc: lcall 0x100 -> exit   skip encrypted app GSP-RM load/verify

Use with RMCmpGspFwPatchA=1 to test whether patch A is viable once verify is skipped.
Do NOT enable during PLM-only boots — hook is post-PLM / bootstrap only.
"""
from __future__ import annotations

import pathlib
import sys

MARK = "CMP_BOOTER_SKIP_APP"

HELPER = """
static void
s_cmpPatchBooterOsPostPlm
(
    OBJGPU *pGpu,
    KernelGsp *pKernelGsp
)
{
    NvU32 skipApp = 0;
    NvU32 gspDevId = pGpu->idInfo.PCIDeviceID >> 16;
    KernelGspFlcnUcode *pUcode;
    NvU8 *pImage;

    if (gspDevId != 0x2082)
        return;

    (void)osReadRegistryDword(pGpu, "RMCmpBooterSkipApp", &skipApp);
    if (skipApp == 0)
        return;

    pUcode = pKernelGsp->pBooterLoadUcode;
    if ((pUcode == NULL) || (pUcode->bootType != KGSP_FLCN_UCODE_BOOT_DIRECT))
        return;

    pImage = pUcode->ucodeBootDirect.pImage;
    if (pImage == NULL)
        return;

    /* mov r15, 0x31 -> mov r15, 0x00 @ OS IMEM 0x7a (image+0x7b) */
    if (pImage[0x7b] == 0x31U)
        pImage[0x7b] = 0x00U;

    /* lcall 0x100 (7e 00 01 00) -> exit (f8 02) + pad @ OS 0xcc */
    pImage[0xcc] = 0xf8U;
    pImage[0xcd] = 0x02U;
    pImage[0xce] = 0x00U;
    pImage[0xcf] = 0x00U;

    NV_PRINTF(LEVEL_ERROR,
              "CMP_BOOTER_SKIP_APP: post-PLM OS patch (mbox0 + skip lcall 0x100)\\n");
}

"""

ANCHOR = (
    "    // Execute Booter Load\n"
    "    status = kgspExecuteBooterLoad_HAL(pGpu, pKernelGsp,\n"
    "                                       _kgspGetBooterLoadArgs(pKernelGsp, bootMode));\n"
)

PATCH = (
    "    if (bootMode == KGSP_BOOT_MODE_NORMAL)\n"
    "        s_cmpPatchBooterOsPostPlm(pGpu, pKernelGsp);\n"
    "\n"
    "    // Execute Booter Load\n"
    "    status = kgspExecuteBooterLoad_HAL(pGpu, pKernelGsp,\n"
    "                                       _kgspGetBooterLoadArgs(pKernelGsp, bootMode));\n"
)

FUNC_ANCHOR = "NV_STATUS\nkgspBootstrap_TU102\n"


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <kernel_gsp_tu102.c>", file=sys.stderr)
        return 2
    path = pathlib.Path(sys.argv[1])
    text = path.read_text()
    if MARK in text:
        print(f"{path}: already patched")
        return 0
    if ANCHOR not in text:
        print(f"{path}: anchor not found", file=sys.stderr)
        return 1
    if FUNC_ANCHOR not in text:
        print(f"{path}: function anchor not found", file=sys.stderr)
        return 1
    text = text.replace(FUNC_ANCHOR, HELPER + FUNC_ANCHOR, 1)
    text = text.replace(ANCHOR, PATCH, 1)
    path.write_text(text)
    print(f"{path}: inserted post-PLM skip-app patch (RMCmpBooterSkipApp=1)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
