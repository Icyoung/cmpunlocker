#!/usr/bin/env python3
"""Narrow the CMP_PMA_ALLOC log to >=32G PAs only (phantom-zone traffic)."""
import pathlib
import sys

p = pathlib.Path(sys.argv[1])
t = p.read_text()
old = "if ((NvU64)allocationCount * pageSize >= 0x10000ULL)"
new = ("if ((NvU64)allocationCount * pageSize >= 0x10000ULL &&\n"
       "            pPages[0] >= 0x800000000ULL)   /* >=32G only: phantom-zone traffic */")
if "phantom-zone traffic" in t:
    print("already narrowed")
    sys.exit(0)
if t.count(old) != 1:
    print(f"anchor count={t.count(old)}", file=sys.stderr)
    sys.exit(1)
p.write_text(t.replace(old, new, 1))
print("narrowed")
