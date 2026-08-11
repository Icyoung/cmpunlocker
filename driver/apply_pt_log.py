#!/usr/bin/env python3
"""Instrumentation: always log GMMU page-table page allocations (PA + VA range).

2026-08-08: mappings beyond ~35G total per process corrupt an early VA band
(writes black-hole, reads return stale DRAM).  Suspect: page-table pages get
double-allocated / overlapped with user pages or GSP-side structures.  The
stock driver already has a LEVEL_INFO printf with exactly the data we need
(PA + VA range of every page-table level allocation); bump it to LEVEL_ERROR
so it lands in dmesg, and tag it CMP_PT_ALLOC for grepping.

Target: src/nvidia/src/kernel/gpu/mmu/gmmu_walk.c, _gmmuWalkCBLevelAlloc.
Idempotent via "CMP_PT_ALLOC" marker.
"""
import pathlib
import sys

OLD1 = (
    "        NV_PRINTF(LEVEL_INFO,\n"
    '                  "[GPU%u]: [%s] PA 0x%llX (0x%X bytes) for VA 0x%llX-0x%llX\\n",'
)
NEW1 = (
    "        /* cmpunlocker CMP_PT_ALLOC */\n"
    "        NV_PRINTF(LEVEL_ERROR,\n"
    '                  "CMP_PT_ALLOC: [GPU%u] [%s] PA 0x%llX (0x%X bytes) VA 0x%llX-0x%llX\\n",'
)
OLD2 = (
    "        NV_PRINTF(LEVEL_INFO,\n"
    '                  "[GPU%u]:  [Packed: %c] PA 0x%llX (0x%X bytes) for VA 0x%llX-0x%llX\\n",'
)
NEW2 = (
    "        /* cmpunlocker CMP_PT_ALLOC */\n"
    "        NV_PRINTF(LEVEL_ERROR,\n"
    '                  "CMP_PT_ALLOC: [GPU%u] [Packed:%c] PA 0x%llX (0x%X bytes) VA 0x%llX-0x%llX\\n",'
)


def main():
    if len(sys.argv) != 2:
        print("usage: apply_pt_log.py <path/to/gmmu_walk.c>", file=sys.stderr)
        return 2
    src = pathlib.Path(sys.argv[1])
    txt = src.read_text(encoding="utf-8")
    if "CMP_PT_ALLOC" in txt:
        print("already applied; skipping")
        return 0
    if txt.count(OLD1) != 1:
        print(f"OLD1 anchor count={txt.count(OLD1)}", file=sys.stderr)
        return 1
    if txt.count(OLD2) != 1:
        print(f"OLD2 anchor count={txt.count(OLD2)}", file=sys.stderr)
        return 1
    txt = txt.replace(OLD1, NEW1, 1)
    txt = txt.replace(OLD2, NEW2, 1)
    src.write_text(txt, encoding="utf-8")
    print(f"inserted; new size: {len(txt)} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
