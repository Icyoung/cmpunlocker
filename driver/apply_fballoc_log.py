#!/usr/bin/env python3
"""Phantom-owner diagnostic: log every FB memdesc allocation >= 1 MiB.

2026-08-07: drip experiments proved a GSP-owned structure sits inside the
user-allocatable heap (~PA 40 GiB on the 80G profile).  To identify the
owner we log every framebuffer memdesc allocation with its allocTag and
physical range.  The drip's own 60G cudaMalloc also shows up, giving the
exact VA->PA base needed to compute the phantom's physical address.

Idempotent: marker "CMP_FBALLOC" gates re-application.
"""
import pathlib
import sys

ANCHOR = (
    "    // Actually allocate the memory\n"
    "    NV_CHECK_OK(status, LEVEL_ERROR, _memdescAllocInternal(pMemDesc));\n"
    "\n"
    "    if (status != NV_OK)\n"
    "    {\n"
    "        pMemDesc->pHeap = NULL;\n"
    "    }\n"
)

BLOCK = """
    /* cmpunlocker phantom diagnostic: log big FB allocations */
    if (status == NV_OK && pMemDesc->_addressSpace == ADDR_FBMEM)
    {
        NvU64 mdBase = memdescGetPhysAddr(pMemDesc, AT_GPU, 0);
        NvU64 mdSize = memdescGetSize(pMemDesc);
        if (mdSize >= 0x100000ULL)
            NV_PRINTF(LEVEL_ERROR,
                      "CMP_FBALLOC: tag=0x%08x base=0x%016llx size=0x%016llx\\n",
                      pMemDesc->allocTag, mdBase, mdSize);
    }
"""


def main():
    if len(sys.argv) != 2:
        print("usage: apply_fballoc_log.py <path/to/mem_desc.c>", file=sys.stderr)
        return 2
    src = pathlib.Path(sys.argv[1])
    txt = src.read_text(encoding="utf-8")
    if "CMP_FBALLOC" in txt:
        print("already applied; skipping")
        return 0
    if txt.count(ANCHOR) != 1:
        print(f"anchor not unique ({txt.count(ANCHOR)} matches)", file=sys.stderr)
        return 1
    txt = txt.replace(ANCHOR, ANCHOR + BLOCK, 1)
    src.write_text(txt, encoding="utf-8")
    print(f"inserted; new size: {len(txt)} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
