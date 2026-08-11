#!/usr/bin/env python3
"""Post-BooterLoad WPR visibility probe (read-only, post-scheduling).

Hook: mem_mgr.c memmgrInitInternalChannels_IMPL, after memmgrInitCeUtils.
Compares BAR1 / CPU / CE / BAR0-PRAMIN reads at WPR sites vs sysmem radix3.

Regkey: RMCmpGspFwPatchPostBoot=1

Removes legacy hooks from kernel_gsp.c (kgspInitRm) and kernel_gsp_tu102.c
(kgspBootstrap) when upgrading.
"""
from __future__ import annotations

import pathlib
import sys

MARK = "CMP_GSP_POSTBOOT"
PROBE_MARK = "CMP_GSP_POSTBOOT: PROBE"
PROBE_V2_MARK = "radix3@+A"

HELPER = """
static NV_STATUS
s_cmpPostBootReadFb
(
    MemoryManager *pMemoryManager,
    OBJGPU *pGpu,
    NvU64 fbOff,
    NvU32 flags,
    NvU8 *pOut4
)
{
    MEMORY_DESCRIPTOR *pDesc = NULL;
    TRANSFER_SURFACE surf = {0};
    NV_STATUS st;

    st = memdescCreate(&pDesc, pGpu, 4, RM_PAGE_SIZE, NV_TRUE,
                       ADDR_FBMEM, NV_MEMORY_UNCACHED, MEMDESC_FLAGS_NONE);
    if (st != NV_OK)
        return st;
    memdescTagAlloc(st, NV_FB_ALLOC_RM_INTERNAL_OWNER_UNNAMED_TAG_16, pDesc);
    if (st != NV_OK)
    {
        memdescDestroy(pDesc);
        return st;
    }
    memdescDescribe(pDesc, ADDR_FBMEM, fbOff, 4);
    surf.pMemDesc = pDesc;
    st = memmgrMemRead(pMemoryManager, &surf, pOut4, 4, flags);
    memdescFree(pDesc);
    memdescDestroy(pDesc);
    return st;
}

static void
s_cmpPostBootLogProbe2
(
    const char *tag,
    NV_STATUS stBar1,
    const NvU8 b1[4],
    NV_STATUS stProc,
    const NvU8 pr[4]
)
{
    NV_PRINTF(LEVEL_ERROR,
              "CMP_GSP_POSTBOOT: PROBE %-14s BAR1=%c%02x%02x%02x%02x PROC=%c%02x%02x%02x%02x\\n",
              tag,
              (stBar1 == NV_OK) ? 'o' : 'e', b1[0], b1[1], b1[2], b1[3],
              (stProc == NV_OK) ? 'o' : 'e', pr[0], pr[1], pr[2], pr[3]);
}

static NV_STATUS
s_cmpPostBootReadRadix3
(
    MemoryManager *pMemoryManager,
    KernelGsp *pKernelGsp,
    NvU64 off,
    NvU8 *pOut4
)
{
    TRANSFER_SURFACE surf = {0};

    if (pKernelGsp == NULL || pKernelGsp->pGspUCodeRadix3Descriptor == NULL)
        return NV_ERR_INVALID_STATE;

    surf.pMemDesc = pKernelGsp->pGspUCodeRadix3Descriptor;
    surf.offset = off;
    return memmgrMemRead(pMemoryManager, &surf, pOut4, 4, TRANSFER_FLAGS_NONE);
}

static void
s_cmpPostBootPatchGspA
(
    OBJGPU *pGpu,
    MemoryManager *pMemoryManager
)
{
    KernelGsp *pKernelGsp = GPU_GET_KERNEL_GSP(pGpu);
    KernelBus *pKernelBus = GPU_GET_KERNEL_BUS(pGpu);
    GspFwWprMeta *pWprMeta = (pKernelGsp != NULL) ? pKernelGsp->pWprMeta : NULL;
    static NvBool s_attempted = NV_FALSE;
    NvU32 postBoot = 0;
    NvU32 gspDevId = pGpu->idInfo.PCIDeviceID >> 16;
    NvU8 b1[4] = {0}, pr[4] = {0};
    NvU8 bar0b = 0;
    NvU64 patchOff = 0x1b54664ULL;
    NvU64 ctrlOff = 0x100000ULL;
    NV_STATUS stBar1, stProc, stBar0, stRadix;
    NvU64 radixSize = 0;

    if (s_attempted || gspDevId != 0x2082)
        return;

    (void)osReadRegistryDword(pGpu, "RMCmpGspFwPatchPostBoot", &postBoot);
    if (postBoot == 0 || pWprMeta == NULL || pMemoryManager == NULL)
        return;

    s_attempted = NV_TRUE;

    if (pKernelGsp != NULL && pKernelGsp->pGspUCodeRadix3Descriptor != NULL)
        radixSize = memdescGetSize(pKernelGsp->pGspUCodeRadix3Descriptor);

    NV_PRINTF(LEVEL_ERROR,
              "CMP_GSP_POSTBOOT: meta wpr=[0x%llx,0x%llx] heap=[0x%llx+0x%llx] "
              "gspFw=0x%llx radix=0x%llx bootBin=0x%llx nonWprHeap=0x%llx fb=0x%llx "
              "radixDesc=%p size=0x%llx metaPa=0x%llx\\n",
              pWprMeta->gspFwWprStart, pWprMeta->gspFwWprEnd,
              pWprMeta->gspFwHeapOffset, pWprMeta->gspFwHeapSize,
              pWprMeta->gspFwOffset, pWprMeta->sizeOfRadix3Elf,
              pWprMeta->bootBinOffset, pWprMeta->nonWprHeapOffset,
              pWprMeta->fbSize,
              (pKernelGsp != NULL) ? pKernelGsp->pGspUCodeRadix3Descriptor : NULL,
              radixSize, pWprMeta->sysmemAddrOfRadix3Elf);

    stRadix = s_cmpPostBootReadRadix3(pMemoryManager, pKernelGsp, 0, b1);
    NV_PRINTF(LEVEL_ERROR,
              "CMP_GSP_POSTBOOT: PROBE radix3@+0      st=0x%x %02x%02x%02x%02x "
              "(expect 7f454c46 ELF)\\n",
              stRadix, b1[0], b1[1], b1[2], b1[3]);

    stRadix = s_cmpPostBootReadRadix3(pMemoryManager, pKernelGsp, patchOff, b1);
    NV_PRINTF(LEVEL_ERROR,
              "CMP_GSP_POSTBOOT: PROBE radix3@+A      st=0x%x %02x%02x%02x%02x "
              "(expect e780404f jalr)\\n",
              stRadix, b1[0], b1[1], b1[2], b1[3]);

    stBar1 = s_cmpPostBootReadFb(pMemoryManager, pGpu, ctrlOff,
                                 TRANSFER_FLAGS_USE_BAR1, b1);
    stProc = s_cmpPostBootReadFb(pMemoryManager, pGpu, ctrlOff,
                                 TRANSFER_FLAGS_NONE, pr);
    stBar0 = (pKernelBus != NULL)
        ? kbusMemAccessBar0Window_HAL(pGpu, pKernelBus, ctrlOff, &bar0b, 1,
                                      NV_TRUE, ADDR_FBMEM)
        : NV_ERR_INVALID_STATE;
    NV_PRINTF(LEVEL_ERROR,
              "CMP_GSP_POSTBOOT: PROBE ctrl@+1MiB     BAR1=%c%02x%02x%02x%02x "
              "PROC=%c%02x%02x%02x%02x BAR0=%c%02x\\n",
              (stBar1 == NV_OK) ? 'o' : 'e', b1[0], b1[1], b1[2], b1[3],
              (stProc == NV_OK) ? 'o' : 'e', pr[0], pr[1], pr[2], pr[3],
              (stBar0 == NV_OK) ? 'o' : 'e', bar0b);

    stBar1 = s_cmpPostBootReadFb(pMemoryManager, pGpu, pWprMeta->nonWprHeapOffset,
                                 TRANSFER_FLAGS_USE_BAR1, b1);
    stProc = s_cmpPostBootReadFb(pMemoryManager, pGpu, pWprMeta->nonWprHeapOffset,
                                 TRANSFER_FLAGS_NONE, pr);
    s_cmpPostBootLogProbe2("nonWprHeap", stBar1, b1, stProc, pr);

    stBar1 = s_cmpPostBootReadFb(pMemoryManager, pGpu, pWprMeta->gspFwOffset,
                                 TRANSFER_FLAGS_USE_BAR1, b1);
    stProc = s_cmpPostBootReadFb(pMemoryManager, pGpu, pWprMeta->gspFwOffset,
                                 TRANSFER_FLAGS_NONE, pr);
    s_cmpPostBootLogProbe2("elf@gspFw", stBar1, b1, stProc, pr);

    stBar1 = s_cmpPostBootReadFb(pMemoryManager, pGpu,
                                 pWprMeta->gspFwOffset + patchOff,
                                 TRANSFER_FLAGS_USE_BAR1, b1);
    stProc = s_cmpPostBootReadFb(pMemoryManager, pGpu,
                                 pWprMeta->gspFwOffset + patchOff,
                                 TRANSFER_FLAGS_NONE, pr);
    s_cmpPostBootLogProbe2("patch@A", stBar1, b1, stProc, pr);

    stBar1 = s_cmpPostBootReadFb(pMemoryManager, pGpu, pWprMeta->gspFwWprStart,
                                 TRANSFER_FLAGS_USE_BAR1, b1);
    stProc = s_cmpPostBootReadFb(pMemoryManager, pGpu, pWprMeta->gspFwWprStart,
                                 TRANSFER_FLAGS_NONE, pr);
    s_cmpPostBootLogProbe2("meta@wprStart", stBar1, b1, stProc, pr);

    NV_PRINTF(LEVEL_ERROR,
              "CMP_GSP_POSTBOOT: verdict WPR host-invisible (BAR1/PROC!=radix3); "
              "patch-A must land pre-Booter or via SEC2\\n");
}

"""

MEMMGR_ANCHOR = (
    "    NV_ASSERT_OK_OR_RETURN(memmgrInitCeUtils(pMemoryManager, NV_FALSE, NV_TRUE));\n"
    "\n"
    "    return NV_OK;\n"
    "}\n"
    "\n"
    "NV_STATUS\n"
    "memmgrDestroyInternalChannels_IMPL\n"
)

MEMMGR_PATCH = (
    "    NV_ASSERT_OK_OR_RETURN(memmgrInitCeUtils(pMemoryManager, NV_FALSE, NV_TRUE));\n"
    "\n"
    "    s_cmpPostBootPatchGspA(pGpu, pMemoryManager);\n"
    "\n"
    "    return NV_OK;\n"
    "}\n"
    "\n"
    "NV_STATUS\n"
    "memmgrDestroyInternalChannels_IMPL\n"
)

MEMMGR_FUNC_ANCHOR = "NV_STATUS\nmemmgrInitInternalChannels_IMPL\n"

# --- legacy strip helpers ---

GSP_INITRM_PATCH = (
    "    if (status == NV_OK)\n"
    "        s_cmpPostBootPatchGspA(pGpu, pKernelGsp);\n"
    "\n"
)

TU102_BOOT_PATCH = (
    "    if (bootMode == KGSP_BOOT_MODE_NORMAL)\n"
    "        s_cmpPostBootPatchGspA(pGpu, pKernelGsp);\n"
    "\n"
)


def _strip_legacy_gsp(path: pathlib.Path) -> None:
    text = path.read_text()
    if MARK not in text and "s_cmpPostBootPatchGspA" not in text:
        return
    changed = False
    if GSP_INITRM_PATCH in text:
        text = text.replace(GSP_INITRM_PATCH, "", 1)
        changed = True
    for sig in ("static void\ns_cmpPostBootPatchGspA", "static NV_STATUS\ns_cmpPostBootPatchGspA"):
        start = text.find(sig)
        if start == -1:
            continue
        for end_marker in ("NV_STATUS\nkgspInitRm_IMPL\n", "NV_STATUS\nmemmgrInitInternalChannels_IMPL\n"):
            end = text.find(end_marker, start)
            if end != -1 and end > start:
                text = text[:start] + text[end:]
                changed = True
                break
    if changed:
        path.write_text(text)
        print(f"{path}: stripped legacy postboot hook")


def _strip_legacy_tu102(path: pathlib.Path) -> None:
    text = path.read_text()
    if TU102_BOOT_PATCH not in text and "s_cmpPostBootPatchGspA" not in text:
        return
    changed = False
    if TU102_BOOT_PATCH in text:
        text = text.replace(TU102_BOOT_PATCH, "", 1)
        changed = True
    for sig in ("static void\ns_cmpPostBootPatchGspA", "static NV_STATUS\ns_cmpPostBootPatchGspA"):
        start = text.find(sig)
        if start == -1:
            continue
        end = text.find("NV_STATUS\nkgspBootstrap_TU102\n", start)
        if end != -1:
            text = text[:start] + text[end:]
            changed = True
    if changed:
        path.write_text(text)
        print(f"{path}: stripped bootstrap-era postboot hook")


def main() -> int:
    if len(sys.argv) < 2:
        print(f"usage: {sys.argv[0]} <mem_mgr.c> [kernel_gsp.c] [kernel_gsp_tu102.c]", file=sys.stderr)
        return 2

    memmgr_c = pathlib.Path(sys.argv[1])
    text = memmgr_c.read_text()

    if len(sys.argv) >= 3:
        _strip_legacy_gsp(pathlib.Path(sys.argv[2]))
    if len(sys.argv) >= 4:
        _strip_legacy_tu102(pathlib.Path(sys.argv[3]))

    if MARK in text and PROBE_V2_MARK in text:
        print(f"{memmgr_c}: already patched (WPR read probe v2)")
        return 0

    if MARK in text:
        start = text.find("static void\ns_cmpPostBootPatchGspA")
        if start == -1:
            start = text.find("static NV_STATUS\ns_cmpPostBootPatchGspA")
        end = text.find(MEMMGR_FUNC_ANCHOR, start if start != -1 else 0)
        if start != -1 and end != -1:
            text = text[:start] + text[end:]

    if MEMMGR_ANCHOR not in text:
        print(f"{memmgr_c}: memmgr anchor not found", file=sys.stderr)
        return 1
    if MEMMGR_FUNC_ANCHOR not in text:
        print(f"{memmgr_c}: memmgr function anchor not found", file=sys.stderr)
        return 1

    if "s_cmpPostBootPatchGspA(pGpu, pMemoryManager)" not in text:
        text = text.replace(MEMMGR_ANCHOR, MEMMGR_PATCH, 1)
    if HELPER.strip() not in text:
        text = text.replace(MEMMGR_FUNC_ANCHOR, HELPER + MEMMGR_FUNC_ANCHOR, 1)
    memmgr_c.write_text(text)
    print(f"{memmgr_c}: post-scheduling WPR read probe (RMCmpGspFwPatchPostBoot=1)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
