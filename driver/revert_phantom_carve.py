#!/usr/bin/env python3
"""Revert the region-split carve from kernel_gsp.c (superseded by PMA pin)."""
import importlib.util
import pathlib
import sys

spec = importlib.util.spec_from_file_location(
    "apc", "/home/icy/cmpunlocker/driver/apply_phantom_carve.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

p = pathlib.Path(sys.argv[1])
txt = p.read_text()
if "CMP_CARVE" not in txt:
    print("carve not present; nothing to do")
    sys.exit(0)
combo = m.ANCHOR + m.BLOCK
if combo in txt:
    p.write_text(txt.replace(combo, m.ANCHOR, 1))
    print("carve reverted")
elif m.BLOCK in txt:
    p.write_text(txt.replace(m.BLOCK, "", 1))
    print("carve block stripped")
else:
    print("ERROR: CMP_CARVE present but block not matched", file=sys.stderr)
    sys.exit(1)
