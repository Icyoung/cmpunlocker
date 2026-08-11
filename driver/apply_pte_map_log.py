#!/usr/bin/env python3
"""Instrumentation: log every GMMU PTE batch fill (VA range -> PT page + phys).

2026-08-08 "32G wall" hunt.  Facts established by the 48G flood test:
  * bad VA band [base+3.4G, base+(T-32G)) for total mapping T > ~35G;
  * the buffer is physically linear (PA = 0x26600000 + VA_offset, hole-skip),
    and the bad band's physical pages are all BELOW 32G -- so this is not a
    ">32G PTE encoding" bug;
  * the leaf page-table allocations for the buffer never hit the PMA log
    (they come from the PMA-managed page-table pool).

This logger answers the next decisive question: when the walk maps VA x and
VA x+32G, do the PTEs land in the same leaf page-table page / entry index
(page-table aliasing), or in distinct pages with correct values (=> the
corruption happens after the fill, or in hardware)?

Two tags, emitted as an ordered pair per batch:
  CMP_PTE_VA  (mmu_walk_map.c, _mmuWalkMap):  VA range + entry indices.
  CMP_PTE_MAP (virt_mem_allocator_gm107.c,
               _gmmuWalkCBMapNextEntries_RmAperture): leaf PT page PA, entry
               indices, first page's physAddr, and the first PTE qword that
               was actually written into the shadow buffer.

Idempotent via "CMP_PTE_VA" / "CMP_PTE_MAP" markers.
"""
import pathlib
import sys

MAP_OLD = (
    "        // Map the next batch of entry values.\n"
    "        pTarget->MapNextEntries(pWalk->pUserCtx,\n"
)
MAP_NEW = (
    "        /* cmpunlocker CMP_PTE_VA */\n"
    "        NV_PRINTF(LEVEL_ERROR,\n"
    '                  "CMP_PTE_VA: VA 0x%llX-0x%llX entries 0x%X-0x%X\\n",\n'
    "                  vaLo, vaHi, entryIndexLo, entryIndexHi);\n"
    "        // Map the next batch of entry values.\n"
    "        pTarget->MapNextEntries(pWalk->pUserCtx,\n"
)

FILL_OLD = (
    "    pIter->pMap = memmgrMemBeginTransfer(pMemoryManager, &surf, sizeOfEntries, transferFlags);\n"
    "    NV_ASSERT_OR_RETURN_VOID(NULL != pIter->pMap);\n"
    "\n"
    "    _gmmuWalkCBMapNextEntries_Direct(pUserCtx, pTarget, pLevelMem,\n"
    "                                     entryIndexLo, entryIndexHi, pProgress);\n"
    "\n"
    "    memmgrMemEndTransfer(pMemoryManager, &surf, sizeOfEntries, transferFlags);\n"
)
FILL_NEW = (
    "    pIter->pMap = memmgrMemBeginTransfer(pMemoryManager, &surf, sizeOfEntries, transferFlags);\n"
    "    NV_ASSERT_OR_RETURN_VOID(NULL != pIter->pMap);\n"
    "\n"
    "    {\n"
    "        /* cmpunlocker CMP_PTE_MAP: record where this PTE batch lands */\n"
    "        NvU64 cmpFirstPhys = pIter->physAddr;\n"
    "        _gmmuWalkCBMapNextEntries_Direct(pUserCtx, pTarget, pLevelMem,\n"
    "                                         entryIndexLo, entryIndexHi, pProgress);\n"
    "        NV_PRINTF(LEVEL_ERROR,\n"
    '                  "CMP_PTE_MAP: ptPA 0x%llX entries 0x%X-0x%X firstPhys 0x%llX pte0 0x%llX\\n",\n'
    "                  memdescGetPhysAddr(pMemDesc, AT_GPU, 0),\n"
    "                  entryIndexLo, entryIndexHi, cmpFirstPhys,\n"
    "                  *((NvU64 *)pIter->pMap));\n"
    "    }\n"
    "\n"
    "    memmgrMemEndTransfer(pMemoryManager, &surf, sizeOfEntries, transferFlags);\n"
)


def patch(path, marker, old, new):
    src = pathlib.Path(path)
    txt = src.read_text(encoding="utf-8")
    if marker in txt:
        print(f"{src.name}: already applied; skipping")
        return 0
    if txt.count(old) != 1:
        print(f"{src.name}: anchor count={txt.count(old)}", file=sys.stderr)
        return 1
    txt = txt.replace(old, new, 1)
    src.write_text(txt, encoding="utf-8")
    print(f"{src.name}: inserted; new size: {len(txt)} bytes")
    return 0


def main():
    if len(sys.argv) != 3:
        print("usage: apply_pte_map_log.py <mmu_walk_map.c> <virt_mem_allocator_gm107.c>",
              file=sys.stderr)
        return 2
    rc = patch(sys.argv[1], "CMP_PTE_VA", MAP_OLD, MAP_NEW)
    rc |= patch(sys.argv[2], "CMP_PTE_MAP", FILL_OLD, FILL_NEW)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
