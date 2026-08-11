#!/usr/bin/env python3
"""Optional in-RAM GSP-RM patch before radix3 upload (CMP 170HX / tu10x).

Patches the mapped .fwimage buffer in host memory after section parse,
before kgspCreateRadix3 copies it for Booter.  Controlled by regkey:

  NVreg_RegistryDwords="RMCmpGspFwPatchA=1"

Patch A (tu10x): NOP chunkloop jalr → dmaUpdateVASpace at image+0x1b54664
(stock bytes e7 80 40 4f → 13 05 00 00).

Expected: Booter still returns 0xb (signature mismatch) unless verify is
bypassed — this probe confirms the RAM hook fires and isolates verify.
"""
from __future__ import annotations

import pathlib
import sys

ANCHOR = (
    "    NV_CHECK_OK_OR_RETURN(LEVEL_ERROR,\n"
    "        kgspCreateRadix3(pGpu, pKernelGsp, &pKernelGsp->pGspUCodeRadix3Descriptor,\n"
    "            NULL, pGspFw->pImageData, pGspFw->imageSize));\n"
)

PATCH = """
    /* cmpunlocker: optional GSP-RM tu10x patch A in RAM (pre-radix3 / pre-Booter) */
    {
        NvU32 gspPatchA = 0;
        NvU32 gspDevId = pGpu->idInfo.PCIDeviceID >> 16;

        (void)osReadRegistryDword(pGpu, "RMCmpGspFwPatchA", &gspPatchA);
        if (gspDevId == 0x2082 && gspPatchA != 0 &&
            pGspFw->pImageData != NULL &&
            pGspFw->imageSize > 0x1b54668ULL)
        {
            NvU8 *pGspImg = (NvU8 *)pGspFw->pImageData;
            const NvU64 patchOff = 0x1b54664ULL;

            if (pGspImg[patchOff]     == 0xe7 &&
                pGspImg[patchOff + 1] == 0x80 &&
                pGspImg[patchOff + 2] == 0x40 &&
                pGspImg[patchOff + 3] == 0x4f)
            {
                pGspImg[patchOff]     = 0x13;
                pGspImg[patchOff + 1] = 0x05;
                pGspImg[patchOff + 2] = 0x00;
                pGspImg[patchOff + 3] = 0x00;
                NV_PRINTF(LEVEL_ERROR,
                          "CMP_GSP_PATCH: patch A NOP jalr applied @ image+0x%llx\\n",
                          patchOff);
            }
            else
            {
                NV_PRINTF(LEVEL_ERROR,
                          "CMP_GSP_PATCH: stock bytes mismatch @ image+0x%llx "
                          "(%02x %02x %02x %02x)\\n",
                          patchOff,
                          pGspImg[patchOff], pGspImg[patchOff + 1],
                          pGspImg[patchOff + 2], pGspImg[patchOff + 3]);
            }
        }

        /* cmpunlocker: null-content patch — flip one byte in a vGPU license
         * string (".rodata", execution-irrelevant) purely to fail verify.
         * With RMCmpBooterForceMbox0=1 forgive: if GSP-RM boots fine, the
         * app copies the image into WPR BEFORE verifying (copy-then-verify),
         * and patch A's own semantics were what hung GSP on 08-09. */
        {
            NvU32 gspNull = 0;
            (void)osReadRegistryDword(pGpu, "RMCmpGspFwNullPatch", &gspNull);
            if (gspDevId == 0x2082 && gspNull != 0 &&
                pGspFw->pImageData != NULL &&
                pGspFw->imageSize > 0x83d28ULL)
            {
                NvU8 *pGspImg = (NvU8 *)pGspFw->pImageData;
                if (pGspImg[0x83d28] == 0x4e)   /* 'N' in "NVIDIA" */
                {
                    pGspImg[0x83d28] = 0x4f;    /* -> 'O' */
                    NV_PRINTF(LEVEL_ERROR,
                              "CMP_GSP_PATCH: null patch applied @ image+0x83d28\\n");
                }
                else
                {
                    NV_PRINTF(LEVEL_ERROR,
                              "CMP_GSP_PATCH: null patch byte mismatch (%02x)\\n",
                              pGspImg[0x83d28]);
                }
            }
        }
    }

"""


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <kernel_gsp.c>", file=sys.stderr)
        return 2
    path = pathlib.Path(sys.argv[1])
    text = path.read_text()
    if "CMP_GSP_PATCH:" in text:
        print(f"{path}: already patched")
        return 0
    if ANCHOR not in text:
        print(f"{path}: anchor not found", file=sys.stderr)
        return 1
    path.write_text(text.replace(ANCHOR, PATCH + ANCHOR, 1))
    print(f"{path}: inserted pre-radix3 GSP-RM patch A hook")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
