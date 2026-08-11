#!/usr/bin/env python3
"""Post-PLM verify-bypass payload (SEC2 refill gadget on normal BooterLoad path).

Runs AFTER kgspSec2PostblTimingRebuildStockSignature, BEFORE kgspPopulateWprMeta.
Does NOT touch PLM refill loops.

HARDWARE RESULT (2026-08-09, CMP 170HX): NEGATIVE — replacing stock signature
with the 0xf800 gadget template causes PLM 0x31 storm and normal BooterLoad
status=0xffff.  Do not enable RMCmpBooterVerifyBypass with patch A on this card.

Working combo for BooterLoad status=0x0: RMCmpGspFwPatchA=1 + RMCmpBooterForceMbox0=1
(forgive mbox 0xb) — but GSP init still hangs (patched image not in WPR).

Regkeys (NVreg_RegistryDwords):
  RMCmpBooterVerifyBypass=1   install 0xf800 gadget template (master enable)
  RMCmpDmemSlotOff=<off>      optional extra dword (e.g. 63480 = 0xf7f8)
  RMCmpDmemSlotVal=<val>
  RMCmpDmemGadget2=1          clone tail gadget to 0xe754
  RMCmpDmemGadget2Addr=<u32>  second MMIO write address (0xe76c)
  RMCmpDmemGadget2Val=<u32>   second MMIO write value   (0xe754)
"""
from __future__ import annotations

import pathlib
import sys

MARK = "CMP_POSTPLM_VERIFY_BYPASS"
OLD_MARK = "CMP_DMEM_SLOT_EXP"

HELPER = """
static void _kgspSec2PostblTimingPutU32(NvU8 *pBuffer, NvU32 offset, NvU32 value);
static void _kgspSec2PostblTimingFillPayload(NvU8 *pSignatureVa, NvU64 signatureSize,
                                             NvU32 writeAddr, NvU32 writeValue);

static void
s_cmpMaybePatchDmemSlot
(
    OBJGPU *pGpu,
    NvU8 *pSignatureVa,
    NvU64 signatureSize
)
{
    NvU32 slotOff = 0;
    NvU32 slotVal = 0;

    if ((pSignatureVa == NULL) || (signatureSize == 0))
        return;

    (void)osReadRegistryDword(pGpu, "RMCmpDmemSlotOff", &slotOff);
    (void)osReadRegistryDword(pGpu, "RMCmpDmemSlotVal", &slotVal);
    if (slotOff == 0 || (slotOff + sizeof(NvU32) > signatureSize))
        return;

    _kgspSec2PostblTimingPutU32(pSignatureVa, slotOff, slotVal);
    NV_PRINTF(LEVEL_ERROR,
              "CMP_DMEM_SLOT_EXP: wrote DMEM[0x%x]=0x%x\\n",
              slotOff, slotVal);
}

static NV_STATUS
s_cmpPostPlmVerifyBypass
(
    OBJGPU *pGpu,
    KernelGsp *pKernelGsp
)
{
    NvU32 bypass = 0;
    NvU32 slotOff = 0;
    NvU32 gadget2 = 0;
    NvU32 g2Addr = 0;
    NvU32 g2Val = 0;
    NvU32 gspDevId = pGpu->idInfo.PCIDeviceID >> 16;
    NvU8 *pSignatureVa = NULL;
    NV_STATUS status = NV_OK;
    NvU64 sigSize = SEC2_POSTBL_TIMING_SIGNATURE_SIZE;
    NvU64 flags = MEMDESC_FLAGS_NONE;
    NvU32 off;

    if (gspDevId != 0x2082)
        return NV_OK;

    (void)osReadRegistryDword(pGpu, "RMCmpBooterVerifyBypass", &bypass);
    (void)osReadRegistryDword(pGpu, "RMCmpDmemSlotOff", &slotOff);
    (void)osReadRegistryDword(pGpu, "RMCmpDmemGadget2", &gadget2);
    (void)osReadRegistryDword(pGpu, "RMCmpDmemGadget2Addr", &g2Addr);
    (void)osReadRegistryDword(pGpu, "RMCmpDmemGadget2Val", &g2Val);

    if (bypass == 0 && slotOff == 0 && gadget2 == 0)
        return NV_OK;

    if (pKernelGsp->pSignatureMemdesc != NULL)
    {
        memdescFree(pKernelGsp->pSignatureMemdesc);
        memdescDestroy(pKernelGsp->pSignatureMemdesc);
        pKernelGsp->pSignatureMemdesc = NULL;
    }

    flags |= MEMDESC_FLAGS_ALLOC_IN_UNPROTECTED_MEMORY;
    NV_CHECK_OK_OR_RETURN(LEVEL_ERROR,
        memdescCreate(&pKernelGsp->pSignatureMemdesc, pGpu, sigSize, 256,
            NV_TRUE, ADDR_SYSMEM, NV_MEMORY_CACHED, flags));
    memdescTagAlloc(status, NV_FB_ALLOC_RM_INTERNAL_OWNER_UNNAMED_TAG_16,
                    pKernelGsp->pSignatureMemdesc);
    NV_CHECK_OK_OR_RETURN(LEVEL_ERROR, status);

    pSignatureVa = memdescMapInternal(pGpu, pKernelGsp->pSignatureMemdesc,
                                      TRANSFER_FLAGS_NONE);
    NV_CHECK_OK_OR_RETURN(LEVEL_ERROR,
        (pSignatureVa != NULL) ? NV_OK : NV_ERR_INSUFFICIENT_RESOURCES);

    _kgspSec2PostblTimingFillPayload(pSignatureVa, sigSize,
                                     0x009a0148U, 0xffffffffU);
    s_cmpMaybePatchDmemSlot(pGpu, pSignatureVa, sigSize);

    if (gadget2 != 0)
    {
        for (off = 0; off < 0xac; off += sizeof(NvU32))
        {
            NvU32 v = *(NvU32 *)(pSignatureVa + 0xf754 + off);
            _kgspSec2PostblTimingPutU32(pSignatureVa, 0xe754 + off, v);
        }
        if (g2Addr != 0)
            _kgspSec2PostblTimingPutU32(pSignatureVa, 0xe76c, g2Addr);
        if (g2Val != 0)
            _kgspSec2PostblTimingPutU32(pSignatureVa, 0xe754, g2Val);
        NV_PRINTF(LEVEL_ERROR,
                  "CMP_GADGET2: cloned tail -> 0xe754 addr=0x%x val=0x%x\\n",
                  g2Addr, g2Val);
    }

    memdescUnmapInternal(pGpu, pKernelGsp->pSignatureMemdesc, 0);
    memdescFlushCpuCaches(pGpu, pKernelGsp->pSignatureMemdesc);

    if (pKernelGsp->pWprMeta != NULL)
    {
        pKernelGsp->pWprMeta->sysmemAddrOfSignature =
            memdescGetPhysAddr(pKernelGsp->pSignatureMemdesc, AT_GPU, 0);
        pKernelGsp->pWprMeta->sizeOfSignature = sigSize;
    }
    if (pKernelGsp->pWprMetaDescriptor != NULL)
        memdescFlushCpuCaches(pGpu, pKernelGsp->pWprMetaDescriptor);

    NV_PRINTF(LEVEL_ERROR,
              "CMP_POSTPLM_VERIFY_BYPASS: 0xf800 template installed "
              "(bypass=%u slotOff=0x%x gadget2=%u)\\n",
              bypass, slotOff, gadget2);
    return NV_OK;
}

"""

# v1 hooks to remove when upgrading
OLD_REFILL_PATCH = (
    "    _kgspSec2PostblTimingFillPayload(pSignatureVa,\n"
    "        memdescGetSize(pKernelGsp->pSignatureMemdesc), writeAddr, writeValue);\n"
    "\n"
    "    s_cmpMaybePatchDmemSlot(pGpu, pSignatureVa,\n"
    "        memdescGetSize(pKernelGsp->pSignatureMemdesc));\n"
    "\n"
    "    memdescUnmapInternal(pGpu, pKernelGsp->pSignatureMemdesc, 0);\n"
)
OLD_REFILL_ANCHOR = (
    "    _kgspSec2PostblTimingFillPayload(pSignatureVa,\n"
    "        memdescGetSize(pKernelGsp->pSignatureMemdesc), writeAddr, writeValue);\n"
    "\n"
    "    memdescUnmapInternal(pGpu, pKernelGsp->pSignatureMemdesc, 0);\n"
)
OLD_CREATE_PATCH = (
    "    s_cmpMaybeDumpSigDmemTemplate(pGpu, pSignatureVa,\n"
    "        memdescGetSize(pKernelGsp->pSignatureMemdesc));\n"
    "\n"
    "    s_cmpMaybePatchDmemSlot(pGpu, pSignatureVa,\n"
    "        memdescGetSize(pKernelGsp->pSignatureMemdesc));\n"
    "\n"
    "    memdescUnmapInternal(pGpu, pKernelGsp->pSignatureMemdesc, 0);\n"
)
OLD_CREATE_ANCHOR = (
    "    s_cmpMaybeDumpSigDmemTemplate(pGpu, pSignatureVa,\n"
    "        memdescGetSize(pKernelGsp->pSignatureMemdesc));\n"
    "\n"
    "    memdescUnmapInternal(pGpu, pKernelGsp->pSignatureMemdesc, 0);\n"
)

POSTPLM_ANCHOR = (
    "        plmStatus = kgspSec2PostblTimingRebuildStockSignature(pGpu, pKernelGsp);\n"
    "        if (plmStatus != NV_OK)\n"
    "        {\n"
    "            NV_PRINTF(LEVEL_ERROR,\n"
    '                      "SEC2_DEBUG: rebuild stock signature failed: 0x%x\\n", plmStatus);\n'
    "            return plmStatus;\n"
    "        }\n"
    "\n"
    "        NV_CHECK_OK_OR_RETURN(LEVEL_ERROR, kgspPopulateWprMeta_HAL(pGpu, pKernelGsp, pGspFw));\n"
)

POSTPLM_PATCH = (
    "        plmStatus = kgspSec2PostblTimingRebuildStockSignature(pGpu, pKernelGsp);\n"
    "        if (plmStatus != NV_OK)\n"
    "        {\n"
    "            NV_PRINTF(LEVEL_ERROR,\n"
    '                      "SEC2_DEBUG: rebuild stock signature failed: 0x%x\\n", plmStatus);\n'
    "            return plmStatus;\n"
    "        }\n"
    "\n"
    "        plmStatus = s_cmpPostPlmVerifyBypass(pGpu, pKernelGsp);\n"
    "        if (plmStatus != NV_OK)\n"
    "        {\n"
    "            NV_PRINTF(LEVEL_ERROR,\n"
    '                      "SEC2_DEBUG: post-PLM verify bypass failed: 0x%x\\n", plmStatus);\n'
    "            return plmStatus;\n"
    "        }\n"
    "\n"
    "        NV_CHECK_OK_OR_RETURN(LEVEL_ERROR, kgspPopulateWprMeta_HAL(pGpu, pKernelGsp, pGspFw));\n"
)

HELPER_ANCHOR = "static void\n_kgspSec2PostblTimingPutU32(NvU8 *pBuffer, NvU32 offset, NvU32 value)\n"

FWD_ANCHOR = "static NvBool\n_kgspSec2PostblTimingEnabled(OBJGPU *pGpu)\n"
FWD_DECL = "static NV_STATUS s_cmpPostPlmVerifyBypass(OBJGPU *pGpu, KernelGsp *pKernelGsp);\n\n"


def _strip_v1(text: str) -> str:
    if OLD_REFILL_PATCH in text:
        text = text.replace(OLD_REFILL_PATCH, OLD_REFILL_ANCHOR, 1)
    if OLD_CREATE_PATCH in text:
        text = text.replace(OLD_CREATE_PATCH, OLD_CREATE_ANCHOR, 1)
    # Remove v1-only helper if post-PLM helper not yet present
    v1_helper_start = text.find("static void\ns_cmpMaybePatchDmemSlot")
    if v1_helper_start != -1 and MARK not in text:
        v1_helper_end = text.find("static void\n_kgspSec2PostblTimingPutU32")
        if v1_helper_end > v1_helper_start:
            text = text[:v1_helper_start] + text[v1_helper_end:]
    return text


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <kernel_gsp.c>", file=sys.stderr)
        return 2
    path = pathlib.Path(sys.argv[1])
    text = path.read_text()
    if MARK in text:
        print(f"{path}: already patched (post-PLM)")
        return 0

    if POSTPLM_ANCHOR not in text:
        print(f"{path}: post-PLM anchor not found", file=sys.stderr)
        return 1
    if HELPER_ANCHOR not in text:
        print(f"{path}: helper insert anchor not found", file=sys.stderr)
        return 1
    if FWD_ANCHOR not in text:
        print(f"{path}: forward-decl anchor not found", file=sys.stderr)
        return 1

    text = _strip_v1(text)
    if MARK in text:
        print(f"{path}: already patched (post-PLM)")
        return 0

    text = text.replace(FWD_ANCHOR, FWD_DECL + FWD_ANCHOR, 1)
    text = text.replace(HELPER_ANCHOR, HELPER + HELPER_ANCHOR, 1)
    text = text.replace(POSTPLM_ANCHOR, POSTPLM_PATCH, 1)
    path.write_text(text)
    print(f"{path}: post-PLM verify bypass hook (RMCmpBooterVerifyBypass=1)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
