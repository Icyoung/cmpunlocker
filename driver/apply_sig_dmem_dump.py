#!/usr/bin/env python3
"""One-shot dump of SEC2 signature memdesc (refill DMEM template) after fill.

Regkey: RMCmpSigDmemDump=1  (via NVreg_RegistryDwords)
Output: /home/icy/cmpunlocker/gsp_analysis/sig_dmem_live.bin

Safe: dumps once per boot in _kgspCreateSignatureMemdesc (before unmap).
"""
from __future__ import annotations

import pathlib
import sys

MARK = "CMP_SIG_DMEM_DUMP"

HELPER = """
extern NV_STATUS os_cmpWritePathFile(const char *path, NvU8 *pBuffer, NvU64 size);

static void
s_cmpMaybeDumpSigDmemTemplate
(
    OBJGPU *pGpu,
    NvU8 *pSignatureVa,
    NvU64 signatureSize
)
{
    NvU32 dump = 0;
    static NvBool s_dumped = NV_FALSE;

    (void)osReadRegistryDword(pGpu, "RMCmpSigDmemDump", &dump);
    if ((dump == 0) || s_dumped || (pSignatureVa == NULL) || (signatureSize == 0))
        return;
    if (((pGpu->idInfo.PCIDeviceID >> 16) != 0x2082) &&
        ((pGpu->idInfo.PCIDeviceID >> 16) != 0x20C2))
        return;

    if (os_cmpWritePathFile("/home/icy/cmpunlocker/gsp_analysis/sig_dmem_live.bin",
                            pSignatureVa, signatureSize) == NV_OK)
    {
        NV_PRINTF(LEVEL_ERROR,
                  "CMP_SIG_DMEM_DUMP: wrote %llu bytes\\n",
                  (unsigned long long)signatureSize);
        s_dumped = NV_TRUE;
    }
}

"""

ANCHOR = (
    "    else\n"
    "    {\n"
    "        portMemCopy(pSignatureVa, memdescGetSize(pKernelGsp->pSignatureMemdesc),\n"
    "            pGspFw->pSignatureData, pGspFw->signatureSize);\n"
    "    }\n"
    "\n"
    "    memdescUnmapInternal(pGpu, pKernelGsp->pSignatureMemdesc, 0);\n"
)

PATCH = (
    "    else\n"
    "    {\n"
    "        portMemCopy(pSignatureVa, memdescGetSize(pKernelGsp->pSignatureMemdesc),\n"
    "            pGspFw->pSignatureData, pGspFw->signatureSize);\n"
    "    }\n"
    "\n"
    "    s_cmpMaybeDumpSigDmemTemplate(pGpu, pSignatureVa,\n"
    "        memdescGetSize(pKernelGsp->pSignatureMemdesc));\n"
    "\n"
    "    memdescUnmapInternal(pGpu, pKernelGsp->pSignatureMemdesc, 0);\n"
)

FUNC_ANCHOR = "static NV_STATUS\n_kgspCreateSignatureMemdesc\n"


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <kernel_gsp.c>", file=sys.stderr)
        return 2
    path = pathlib.Path(sys.argv[1])
    text = path.read_text()
    if MARK in text:
        print(f"{path}: already patched")
        return 0
    if ANCHOR not in text:
        print(f"{path}: anchor not found (is sec2-postbl patch applied?)", file=sys.stderr)
        return 1
    if FUNC_ANCHOR not in text:
        print(f"{path}: function anchor not found", file=sys.stderr)
        return 1
    text = text.replace(FUNC_ANCHOR, HELPER + FUNC_ANCHOR, 1)
    text = text.replace(ANCHOR, PATCH, 1)
    path.write_text(text)
    print(f"{path}: inserted sig DMEM dump (RMCmpSigDmemDump=1)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
