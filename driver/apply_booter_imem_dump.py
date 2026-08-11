#!/usr/bin/env python3
"""Dump SEC2 IMEM/DMEM after Booter halt (decrypted app code for RE).

Regkeys:
  RMCmpBooterImemDump=1   dump on BooterLoad (mboxArg!=0) when mboxRet==0xb
  RMCmpGspFwPatchA=1      optional: force verify-fail path

Outputs under /home/icy/cmpunlocker/gsp_analysis/:
  booter_imem_live.bin   secure IMEM @ 0x100 (often zeroed post-halt)
  booter_dmem_live.bin   DMEM @ 0x0 size 0x6200
"""
from __future__ import annotations

import pathlib
import sys

HELPER = r"""
#include "gpu/falcon/falcon_common.h"

extern NV_STATUS os_cmpWritePathFile(const char *path, NvU8 *pBuffer, NvU64 size);

static NV_STATUS
s_cmpReadSec2Imem_TU102
(
    OBJGPU *pGpu,
    KernelFalcon *pKernelFlcn,
    NvU32 imemOffset,
    NvU32 sizeBytes,
    NvBool bSecure,
    NvU8 *pBuf
)
{
    NvU8 port = 0;
    NvU32 wordIdx;
    NvU32 numWords;
    NvU32 *pWords = (NvU32 *)pBuf;
    NvU32 reg32;
    NvU32 tag;

    if ((pBuf == NULL) || ((imemOffset & 0x3) != 0) || ((sizeBytes & 0x3) != 0) || (sizeBytes == 0))
        return NV_ERR_INVALID_ARGUMENT;

    numWords = sizeBytes >> 2;
    tag = imemOffset >> 8;

    reg32 = kflcnMaskImemAddr_HAL(pGpu, pKernelFlcn, imemOffset);
    reg32 = FLD_SET_DRF_NUM(_PFALCON_FALCON, _IMEMC, _AINCW, 0x0, reg32);
    reg32 = FLD_SET_DRF_NUM(_PFALCON_FALCON, _IMEMC, _SECURE, bSecure ? 0x1 : 0x0, reg32);
    kflcnRegWrite_HAL(pGpu, pKernelFlcn, NV_PFALCON_FALCON_IMEMC(port), reg32);

    for (wordIdx = 0; wordIdx < numWords; wordIdx++)
    {
        NvU32 addr = imemOffset + (wordIdx << 2);

        if ((addr & (FLCN_BLK_ALIGNMENT - 1)) == 0)
        {
            tag = addr >> 8;
            kflcnRegWrite_HAL(pGpu, pKernelFlcn, NV_PFALCON_FALCON_IMEMT(port),
                              DRF_NUM(_PFALCON_FALCON, _IMEMT, _TAG, tag));
        }

        pWords[wordIdx] = kflcnRegRead_HAL(pGpu, pKernelFlcn, NV_PFALCON_FALCON_IMEMD(port));
    }

    return NV_OK;
}

static NV_STATUS
s_cmpReadSec2Dmem_TU102
(
    OBJGPU *pGpu,
    KernelFalcon *pKernelFlcn,
    NvU32 dmemOffset,
    NvU32 sizeBytes,
    NvU8 *pBuf
)
{
    NvU8 port = 0;
    NvU32 wordIdx;
    NvU32 numWords;
    NvU32 *pWords = (NvU32 *)pBuf;
    NvU32 reg32;

    if ((pBuf == NULL) || ((dmemOffset & 0x3) != 0) || ((sizeBytes & 0x3) != 0) || (sizeBytes == 0))
        return NV_ERR_INVALID_ARGUMENT;

    numWords = sizeBytes >> 2;

    reg32 = kflcnMaskDmemAddr_HAL(pGpu, pKernelFlcn, dmemOffset);
    reg32 = FLD_SET_DRF(_PFALCON, _FALCON_DMEMC, _AINCR, _TRUE, reg32);
    kflcnRegWrite_HAL(pGpu, pKernelFlcn, NV_PFALCON_FALCON_DMEMC(port), reg32);

    for (wordIdx = 0; wordIdx < numWords; wordIdx++)
    {
        pWords[wordIdx] = kflcnRegRead_HAL(pGpu, pKernelFlcn, NV_PFALCON_FALCON_DMEMD(port));
    }

    return NV_OK;
}

static void
s_cmpMaybeDumpBooterImem_TU102
(
    OBJGPU *pGpu,
    KernelFalcon *pKernelFlcn,
    NvU32 mailbox0Arg,
    NvU32 mailbox0Ret
)
{
    NvU32 dump = 0;
    NvU8 *pImem = NULL;
    NvU8 *pDmem = NULL;
    const NvU32 appOff = 0x100;
    const NvU32 appSize = 0x8400;
    const NvU32 dmemSize = 0x6200;
    static NvBool dumped = NV_FALSE;

    (void)osReadRegistryDword(pGpu, "RMCmpBooterImemDump", &dump);
    if ((dump == 0) || dumped ||
        ((pGpu->idInfo.PCIDeviceID >> 16) != 0x2082) ||
        (mailbox0Arg == 0) ||
        (mailbox0Ret != 0xb))
    {
        return;
    }

    pImem = portMemAllocNonPaged(appSize);
    pDmem = portMemAllocNonPaged(dmemSize);
    if ((pImem == NULL) || (pDmem == NULL))
        goto done;

    if (s_cmpReadSec2Imem_TU102(pGpu, pKernelFlcn, appOff, appSize, NV_TRUE, pImem) != NV_OK)
        goto done;

    if (s_cmpReadSec2Dmem_TU102(pGpu, pKernelFlcn, 0, dmemSize, pDmem) != NV_OK)
        goto done;

  {
    NvU32 nzImem = 0;
    NvU32 nzDmem = 0;
    NvU32 i;

    for (i = 0; i < appSize; i++)
        if (pImem[i] != 0)
            nzImem++;
    for (i = 0; i < dmemSize; i++)
        if (pDmem[i] != 0)
            nzDmem++;

    if (os_cmpWritePathFile("/home/icy/cmpunlocker/gsp_analysis/booter_imem_live.bin",
                            pImem, appSize) == NV_OK &&
        os_cmpWritePathFile("/home/icy/cmpunlocker/gsp_analysis/booter_dmem_live.bin",
                            pDmem, dmemSize) == NV_OK)
    {
        NV_PRINTF(LEVEL_ERROR,
                  "CMP_BOOTER_IMEM: wrote imem=%u dmem=%u nzImem=%u nzDmem=%u mboxArg=0x%x mboxRet=0x%x\\n",
                  appSize, dmemSize, nzImem, nzDmem, mailbox0Arg, mailbox0Ret);
        dumped = NV_TRUE;
    }
  }

done:
    if (pImem != NULL)
        portMemFree(pImem);
    if (pDmem != NULL)
        portMemFree(pDmem);
}

"""

HOOK_ANCHOR = (
    "    status = kgspExecuteHsFalcon_HAL(pGpu, pKernelGsp,\n"
    "                                     pBooterUcode, pKernelFlcn,\n"
    "                                     &mailbox0, &mailbox1);\n"
    "\n"
    "    NV_PRINTF(LEVEL_INFO, \"after Booter mailbox0 0x%08x, mailbox1 0x%08x\\n\", mailbox0, mailbox1);\n"
)

HOOK_PATCH = (
    "    status = kgspExecuteHsFalcon_HAL(pGpu, pKernelGsp,\n"
    "                                     pBooterUcode, pKernelFlcn,\n"
    "                                     &mailbox0, &mailbox1);\n"
    "\n"
    "    s_cmpMaybeDumpBooterImem_TU102(pGpu, pKernelFlcn, mailbox0Arg, mailbox0);\n"
    "\n"
    "    NV_PRINTF(LEVEL_INFO, \"after Booter mailbox0 0x%08x, mailbox1 0x%08x\\n\", mailbox0, mailbox1);\n"
)

FUNC_ANCHOR = "static NV_STATUS\ns_executeBooterUcode_TU102\n"


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <kernel_gsp_booter_tu102.c>", file=sys.stderr)
        return 2
    path = pathlib.Path(sys.argv[1])
    text = path.read_text()
    if "CMP_BOOTER_IMEM:" in text and "s_cmpReadSec2Dmem_TU102" in text:
        print(f"{path}: already patched")
        return 0
    if "CMP_BOOTER_IMEM:" in text:
        print(f"{path}: stale patch (re-extract tree to refresh)", file=sys.stderr)
        return 1
    if HOOK_ANCHOR not in text:
        print(f"{path}: hook anchor not found", file=sys.stderr)
        return 1
    if FUNC_ANCHOR not in text:
        print(f"{path}: function anchor not found", file=sys.stderr)
        return 1
    text = text.replace(FUNC_ANCHOR, HELPER + FUNC_ANCHOR, 1)
    text = text.replace(HOOK_ANCHOR, HOOK_PATCH, 1)
    path.write_text(text)
    print(f"{path}: inserted SEC2 IMEM/DMEM dump hook (RMCmpBooterImemDump=1)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
