#!/usr/bin/env python3
"""Dump SEC2 IMEM/DMEM after Booter halt on verify-fail (mbox=0xb).

GA100 (CMP 170HX / 0x2082) dispatches kgspExecuteHsFalcon to the TU102 HAL,
not GA102 — patch kernel_gsp_falcon_tu102.c for that chip.

Regkeys (via NVreg_RegistryDwords, not bare modprobe params):
  RMCmpBooterImemDump=1 (+ RMCmpGspFwPatchA=1 to trigger verify-fail mbox 0xb)
Outputs: gsp_analysis/booter_{imem,dmem}_live.bin
"""
from __future__ import annotations

import pathlib
import sys

TRACE_MARK = "CMP_BOOTER_IMEM_TRACE"

HELPER = r"""
#include "gpu/falcon/falcon_common.h"

extern NV_STATUS os_cmpWritePathFile(const char *path, NvU8 *pBuffer, NvU64 size);

static NV_STATUS
s_cmpReadSec2Imem
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
    NvU32 numWords = sizeBytes >> 2;
    NvU32 *pWords = (NvU32 *)pBuf;
    NvU32 reg32;
    NvU32 tag;

    if ((pBuf == NULL) || ((imemOffset & 0x3) != 0) || ((sizeBytes & 0x3) != 0) || (sizeBytes == 0))
        return NV_ERR_INVALID_ARGUMENT;

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
s_cmpReadSec2Dmem
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
    NvU32 numWords = sizeBytes >> 2;
    NvU32 *pWords = (NvU32 *)pBuf;
    NvU32 reg32;

    if ((pBuf == NULL) || ((dmemOffset & 0x3) != 0) || ((sizeBytes & 0x3) != 0) || (sizeBytes == 0))
        return NV_ERR_INVALID_ARGUMENT;

    for (wordIdx = 0; wordIdx < numWords; wordIdx++)
    {
        NvU32 addr = dmemOffset + (wordIdx << 2);

        reg32 = kflcnMaskDmemAddr_HAL(pGpu, pKernelFlcn, addr);
        reg32 = FLD_SET_DRF_NUM(_PFALCON_FALCON, _DMEMC, _AINCW, 0x0, reg32);
        kflcnRegWrite_HAL(pGpu, pKernelFlcn, NV_PFALCON_FALCON_DMEMC(port), reg32);
        pWords[wordIdx] = kflcnRegRead_HAL(pGpu, pKernelFlcn, NV_PFALCON_FALCON_DMEMD(port));
    }

    return NV_OK;
}

static void s_cmpDumpSec2WhileRunning(OBJGPU *pGpu, KernelFalcon *pKernelFlcn, NvU32 mailbox0);

static NvBool s_cmpBooterImemDumped = NV_FALSE;

static NvBool
s_cmpIsSec2CpuHalted
(
    OBJGPU *pGpu,
    KernelFalcon *pKernelFlcn
)
{
    return FLD_TEST_DRF(_PFALCON, _FALCON, _CPUCTL_HALTED, _TRUE,
                        kflcnRegRead_HAL(pGpu, pKernelFlcn, NV_PFALCON_FALCON_CPUCTL));
}

static void
s_cmpPreHaltPollDump
(
    OBJGPU *pGpu,
    KernelFalcon *pKernelFlcn
)
{
    NvU32 iter;
    NvU8 probe[256];
    NvU32 i;
    NvU32 nz;

    if (s_cmpBooterImemDumped)
        return;

    for (iter = 0; iter < 50000 && !s_cmpIsSec2CpuHalted(pGpu, pKernelFlcn); iter++)
    {
        if (s_cmpReadSec2Imem(pGpu, pKernelFlcn, 0x100, sizeof(probe), NV_TRUE, probe) != NV_OK)
            continue;

        nz = 0;
        for (i = 0; i < sizeof(probe); i++)
            if (probe[i] != 0)
                nz++;

        if (nz != 0)
        {
            NV_PRINTF(LEVEL_ERROR,
                      "CMP_BOOTER_PREHALT: probe nz=%u iter=%u\n", nz, iter);
            s_cmpDumpSec2WhileRunning(pGpu, pKernelFlcn, 0);
            s_cmpBooterImemDumped = NV_TRUE;
            return;
        }
        osSpinLoop();
    }
}

static void
s_cmpDumpSec2WhileRunning
(
    OBJGPU *pGpu,
    KernelFalcon *pKernelFlcn,
    NvU32 mailbox0
)
{
    NvU8 *pImem = NULL;
    NvU8 *pDmem = NULL;
    const NvU32 appSize = 0x8400;
    const NvU32 dmemSize = 0x6200;
    NvU32 nzImem = 0;
    NvU32 nzDmem = 0;
    NvU32 i;
    NV_STATUS stImem;
    NV_STATUS stDmem;
    NV_STATUS stWr1;
    NV_STATUS stWr2;

    pImem = portMemAllocNonPaged(appSize);
    pDmem = portMemAllocNonPaged(dmemSize);
    if ((pImem == NULL) || (pDmem == NULL))
    {
        NV_PRINTF(LEVEL_ERROR, "CMP_BOOTER_IMEM_TRACE: alloc failed mbox=0x%x\n", mailbox0);
        goto done;
    }

    stImem = s_cmpReadSec2Imem(pGpu, pKernelFlcn, 0x100, appSize, NV_TRUE, pImem);
    stDmem = s_cmpReadSec2Dmem(pGpu, pKernelFlcn, 0, dmemSize, pDmem);
    if ((stImem != NV_OK) || (stDmem != NV_OK))
    {
        NV_PRINTF(LEVEL_ERROR,
                  "CMP_BOOTER_IMEM_TRACE: read failed mbox=0x%x stImem=0x%x stDmem=0x%x\n",
                  mailbox0, stImem, stDmem);
        goto done;
    }

    for (i = 0; i < appSize; i++)
        if (pImem[i] != 0)
            nzImem++;
    for (i = 0; i < dmemSize; i++)
        if (pDmem[i] != 0)
            nzDmem++;

    stWr1 = os_cmpWritePathFile("/home/icy/cmpunlocker/gsp_analysis/booter_imem_live.bin",
                                pImem, appSize);
    stWr2 = os_cmpWritePathFile("/home/icy/cmpunlocker/gsp_analysis/booter_dmem_live.bin",
                                pDmem, dmemSize);
    if ((stWr1 == NV_OK) && (stWr2 == NV_OK))
    {
        NV_PRINTF(LEVEL_ERROR,
                  "CMP_BOOTER_IMEM: dump ok imem=%u dmem=%u nzImem=%u nzDmem=%u mbox=0x%x\n",
                  appSize, dmemSize, nzImem, nzDmem, mailbox0);
    }
    else
    {
        NV_PRINTF(LEVEL_ERROR,
                  "CMP_BOOTER_IMEM_TRACE: write failed mbox=0x%x stWr1=0x%x stWr2=0x%x "
                  "nzImem=%u nzDmem=%u\n",
                  mailbox0, stWr1, stWr2, nzImem, nzDmem);
    }

done:
    if (pImem != NULL)
        portMemFree(pImem);
    if (pDmem != NULL)
        portMemFree(pDmem);
}

"""

POLL_PATCH_BODY = (
    "    {\n"
    "        NvU32 dumpPoll = 0;\n"
    "        NvU32 mbox = (pMailbox0 != NULL)\n"
    "            ? *pMailbox0\n"
    "            : kflcnRegRead_HAL(pGpu, pKernelFlcn, NV_PFALCON_FALCON_MAILBOX0);\n"
    "\n"
    "        (void)osReadRegistryDword(pGpu, \"RMCmpBooterImemDump\", &dumpPoll);\n"
    "        if (dumpPoll != 0 && ((pGpu->idInfo.PCIDeviceID >> 16) == 0x2082))\n"
    "        {\n"
    "            NV_PRINTF(LEVEL_ERROR,\n"
    "                      \"CMP_BOOTER_IMEM_TRACE: mbox=0x%x dumped=%d dumpPoll=%u status=0x%x\\n\",\n"
    "                      mbox, (NvU32)s_cmpBooterImemDumped, dumpPoll, status);\n"
    "        }\n"
    "        if (!s_cmpBooterImemDumped && dumpPoll != 0 &&\n"
    "            ((pGpu->idInfo.PCIDeviceID >> 16) == 0x2082) &&\n"
    "            ((mbox == 0xb) || (mbox == 0x31)))\n"
    "        {\n"
    "            s_cmpDumpSec2WhileRunning(pGpu, pKernelFlcn, mbox);\n"
    "            s_cmpBooterImemDumped = NV_TRUE;\n"
    "        }\n"
    "    }\n"
    "\n"
)

PRE_HALT_PATCH_BODY = (
    "    {\n"
    "        NvU32 preDump = 0;\n"
    "        (void)osReadRegistryDword(pGpu, \"RMCmpBooterImemDump\", &preDump);\n"
    "        if (preDump != 0 && ((pGpu->idInfo.PCIDeviceID >> 16) == 0x2082))\n"
    "            s_cmpPreHaltPollDump(pGpu, pKernelFlcn);\n"
    "    }\n"
    "\n"
)

TARGETS = {
    "tu102": {
        "func_anchor": "NV_STATUS\nkgspExecuteHsFalcon_TU102\n",
        "pre_halt_anchor": (
            "    // Start CPU now\n"
            "    kflcnStartCpu_HAL(pGpu, pKernelFlcn);\n"
            "\n"
            "    // Wait for completion\n"
            "    status = kflcnWaitForHalt_HAL(pGpu, pKernelFlcn, GPU_TIMEOUT_DEFAULT, 0);\n"
        ),
        "poll_anchor": (
            "    // Read mailboxes if requested\n"
            "    if (pMailbox0 != NULL)\n"
            "        *pMailbox0 = kflcnRegRead_HAL(pGpu, pKernelFlcn, NV_PFALCON_FALCON_MAILBOX0);\n"
            "    if (pMailbox1 != NULL)\n"
            "        *pMailbox1 = kflcnRegRead_HAL(pGpu, pKernelFlcn, NV_PFALCON_FALCON_MAILBOX1);\n"
            "\n"
            "    return status;\n"
        ),
    },
    "ga102": {
        "func_anchor": "NV_STATUS\nkgspExecuteHsFalcon_GA102\n",
        "poll_anchor": (
            "    // Read mailboxes if requested.\n"
            "    if (pMailbox0 != NULL)\n"
            "        *pMailbox0 = kflcnRegRead_HAL(pGpu, pKernelFlcn, NV_PFALCON_FALCON_MAILBOX0);\n"
            "    if (pMailbox1 != NULL)\n"
            "        *pMailbox1 = kflcnRegRead_HAL(pGpu, pKernelFlcn, NV_PFALCON_FALCON_MAILBOX1);\n"
            "\n"
            "    return status;\n"
        ),
    },
}


def detect_target(text: str, path: pathlib.Path) -> str | None:
    if "kgspExecuteHsFalcon_TU102" in text:
        return "tu102"
    if "kgspExecuteHsFalcon_GA102" in text:
        return "ga102"
    name = path.name.lower()
    if "tu102" in name:
        return "tu102"
    if "ga102" in name:
        return "ga102"
    return None


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <kernel_gsp_falcon_*.c>", file=sys.stderr)
        return 2
    path = pathlib.Path(sys.argv[1])
    text = path.read_text()
    if TRACE_MARK in text:
        print(f"{path}: already patched")
        return 0

    target = detect_target(text, path)
    if target is None:
        print(f"{path}: unknown falcon HAL file", file=sys.stderr)
        return 1

    cfg = TARGETS[target]
    poll_anchor = cfg["poll_anchor"]
    func_anchor = cfg["func_anchor"]
    if poll_anchor not in text:
        print(f"{path}: poll anchor not found ({target})", file=sys.stderr)
        return 1
    if func_anchor not in text:
        print(f"{path}: function anchor not found ({target})", file=sys.stderr)
        return 1

    poll_patch = poll_anchor.replace(
        "\n    return status;\n",
        "\n" + POLL_PATCH_BODY + "    return status;\n",
        1,
    )
    text = text.replace(func_anchor, HELPER + func_anchor, 1)
    pre_halt_anchor = cfg.get("pre_halt_anchor")
    if pre_halt_anchor is not None:
        if pre_halt_anchor not in text:
            print(f"{path}: pre-halt anchor not found ({target})", file=sys.stderr)
            return 1
        pre_halt_patch = pre_halt_anchor.replace(
            "\n    // Wait for completion\n",
            "\n" + PRE_HALT_PATCH_BODY + "    // Wait for completion\n",
            1,
        )
        text = text.replace(pre_halt_anchor, pre_halt_patch, 1)
    text = text.replace(poll_anchor, poll_patch, 1)
    path.write_text(text)
    print(f"{path}: inserted SEC2 dump + trace hook ({target}, RMCmpBooterImemDump=1)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
