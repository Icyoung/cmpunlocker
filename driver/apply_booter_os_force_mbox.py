#!/usr/bin/env python3
"""Post-PLM Booter OS patch — keep lcall 0x100, force mailbox0=0 on success path.

Regkey: RMCmpBooterForceMbox0=1

After PLM, patches pKernelGsp->pBooterLoadUcode->ucodeBootDirect.pImage before
the final normal kgspExecuteBooterLoad (same timing as RMCmpBooterSkipApp):

  image+0x7b: 0x31 -> 0x00        theater mbox (PLM intentional fail -> success)
  image+0xd0: exit -> lcall 0xf4  trampoline after GSP-RM app returns
  image+0xf4: stub               mov mbox0=0; iowrs; exit

The app at lcall 0x100 still runs (load + verify).  On verify fail it leaves
mbox=0xb; the stub clears mailbox0 before exit so the host driver proceeds.
Use with RMCmpGspFwPatchA=1 to test patched GSP-RM once verify is ignored.

Do NOT combine with RMCmpBooterSkipApp=1 (skip-app replaces lcall 0x100).
"""
from __future__ import annotations

import pathlib
import sys

MARK = "CMP_BOOTER_FORCE_MBOX0"

# falcon: mov r15,0; mov r9,0x1000; iowrs I[r9],r15; exit
STUB_OFF = 0xF4
STUB = bytes([0x0F, 0x00, 0x49, 0x00, 0x10, 0xF7, 0x9F, 0x00, 0xF8, 0x02])
# falcon: lcall 0xf4
LCALL_STUB = bytes([0x7E, 0xF4, 0x00, 0x00])

HELPER = """
static const NvU8 s_cmpBooterForceMbox0Stub[] = {
    0x0f, 0x00, 0x49, 0x00, 0x10, 0xf7, 0x9f, 0x00, 0xf8, 0x02
};

static void
s_cmpPatchBooterOsForceMbox0
(
    OBJGPU *pGpu,
    KernelGsp *pKernelGsp
)
{
    NvU32 forceMbox0 = 0;
    NvU32 gspDevId = pGpu->idInfo.PCIDeviceID >> 16;
    KernelGspFlcnUcode *pUcode;
    NvU8 *pImage;
    NvU32 i;

    if (gspDevId != 0x2082)
        return;

    (void)osReadRegistryDword(pGpu, "RMCmpBooterForceMbox0", &forceMbox0);
    if (forceMbox0 == 0)
        return;

    pUcode = pKernelGsp->pBooterLoadUcode;
    if ((pUcode == NULL) || (pUcode->bootType != KGSP_FLCN_UCODE_BOOT_DIRECT))
        return;

    pImage = pUcode->ucodeBootDirect.pImage;
    if (pImage == NULL)
        return;

    if (pImage[0x7b] == 0x31U)
        pImage[0x7b] = 0x00U;

    /* after lcall 0x100: was exit (f8 02) -> lcall post-app mbox-clear stub */
    if (pImage[0xd0] == 0xf8U && pImage[0xd1] == 0x02U)
    {
        pImage[0xd0] = 0x7eU;
        pImage[0xd1] = 0xf4U;
        pImage[0xd2] = 0x00U;
        pImage[0xd3] = 0x00U;
    }

    for (i = 0; i < sizeof(s_cmpBooterForceMbox0Stub); i++)
        pImage[0xf4U + i] = s_cmpBooterForceMbox0Stub[i];

    NV_PRINTF(LEVEL_ERROR,
              "CMP_BOOTER_FORCE_MBOX0: post-PLM OS patch (theater mbox0 + lcall0x100 + mbox stub)\\n");
}

"""

ANCHOR = (
    "    if (bootMode == KGSP_BOOT_MODE_NORMAL)\n"
    "        s_cmpPatchBooterOsPostPlm(pGpu, pKernelGsp);\n"
    "\n"
    "    // Execute Booter Load\n"
)

PATCH = (
    "    if (bootMode == KGSP_BOOT_MODE_NORMAL)\n"
    "    {\n"
    "        s_cmpPatchBooterOsPostPlm(pGpu, pKernelGsp);\n"
    "        s_cmpPatchBooterOsForceMbox0(pGpu, pKernelGsp);\n"
    "    }\n"
    "\n"
    "    // Execute Booter Load\n"
)

FUNC_ANCHOR = "static void\ns_cmpPatchBooterOsPostPlm\n"


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
        print(f"{path}: anchor not found (is skip-app hook present?)", file=sys.stderr)
        return 1
    if FUNC_ANCHOR not in text:
        print(f"{path}: skip-app helper anchor not found", file=sys.stderr)
        return 1
    text = text.replace(FUNC_ANCHOR, HELPER + FUNC_ANCHOR, 1)
    text = text.replace(ANCHOR, PATCH, 1)
    path.write_text(text)
    print(f"{path}: inserted post-PLM force-mbox0 patch (RMCmpBooterForceMbox0=1)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
