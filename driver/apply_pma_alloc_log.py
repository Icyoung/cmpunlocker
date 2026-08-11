#!/usr/bin/env python3
"""PMA allocation logger — print physical placement of every >=1MiB PMA alloc.

The phantom structure (GSP-referenced metadata inside the user heap) is
invisible to memdescAlloc.  pmaAllocatePages is the choke point for ALL
PMA-served FB allocations (including client/CUDA vidmem via vidmemPmaAlloc).
Logging every large allocation's page span tells us:
  1. the exact PA base of the drip's 60G cudaMalloc (=> exact phantom PA
     when combined with the drip's death VA offset), and
  2. whether any boot-time PMA allocation covers the phantom PA
     (=> CPU-visible owner) or not (=> GSP-internal allocator).

Idempotent: marker "CMP_PMA_ALLOC" gates re-application.
"""
import pathlib
import sys

ANCHOR = (
    "    if (status == NV_OK)\n"
    "    {\n"
    "        NvU64 i;\n"
)

BLOCK = """
        /* cmpunlocker: log large PMA allocations with physical placement */
        if ((NvU64)allocationCount * pageSize >= 0x10000000ULL)   /* >=256MiB */
            NV_PRINTF(LEVEL_ERROR,
                      "CMP_PMA_ALLOC: count=%llu pageSize=0x%llx first=0x%llx last=0x%llx\\n",
                      (unsigned long long)allocationCount,
                      (unsigned long long)pageSize,
                      (unsigned long long)pPages[0],
                      (unsigned long long)pPages[allocationCount - 1]);
        /* for huge allocs, dump the page at every 1 GiB step (512 x 2MiB) */
        if (pageSize == 0x200000ULL && allocationCount >= 1024)
        {
            NvU64 pi;
            for (pi = 0; pi < allocationCount; pi += 512)
                NV_PRINTF(LEVEL_ERROR,
                          "CMP_PMA_MAP: step_gb=%llu pa=0x%llx\\n",
                          (unsigned long long)(pi / 512),
                          (unsigned long long)pPages[pi]);
        }
"""


ALLOC_ANCHOR_TAIL = (
    "                      (unsigned long long)pPages[allocationCount - 1]);\n"
)

MAP_BLOCK = """
        /* for huge allocs, dump the page at every 1 GiB step (512 x 2MiB) */
        if (pageSize == 0x200000ULL && allocationCount >= 1024)
        {
            NvU64 pi;
            for (pi = 0; pi < allocationCount; pi += 512)
                NV_PRINTF(LEVEL_ERROR,
                          "CMP_PMA_MAP: step_gb=%llu pa=0x%llx\\n",
                          (unsigned long long)(pi / 512),
                          (unsigned long long)pPages[pi]);
        }
"""


def main():
    if len(sys.argv) != 2:
        print("usage: apply_pma_alloc_log.py <path/to/phys_mem_allocator.c>", file=sys.stderr)
        return 2
    src = pathlib.Path(sys.argv[1])
    txt = src.read_text(encoding="utf-8")
    changed = False
    if "CMP_PMA_ALLOC" not in txt:
        if txt.count(ANCHOR) != 1:
            print(f"anchor not unique ({txt.count(ANCHOR)} matches)", file=sys.stderr)
            return 1
        txt = txt.replace(ANCHOR, ANCHOR + BLOCK, 1)
        changed = True
    if "CMP_PMA_MAP" not in txt:
        if txt.count(ALLOC_ANCHOR_TAIL) != 1:
            print("alloc-print tail not found", file=sys.stderr)
            return 1
        txt = txt.replace(ALLOC_ANCHOR_TAIL, ALLOC_ANCHOR_TAIL + MAP_BLOCK, 1)
        changed = True
    if not changed:
        print("already applied; skipping")
        return 0
    src.write_text(txt, encoding="utf-8")
    print(f"inserted; new size: {len(txt)} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
