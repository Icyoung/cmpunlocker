#!/usr/bin/env python3
"""Scan a GSP-RM buffer for patch-A stock bytes (jalr → dmaUpdateVASpace).

Stock: e7 80 40 4f  (RISC-V jalr)
Patch: 13 05 00 00  (addi a0, x0, 0)

Usage:
  python3 scan_patch_a.py <file> [expected_fwimage_off]

If expected_fwimage_off is given (default 0x1b54664), marks whether that offset matches.
"""
from __future__ import annotations

import pathlib
import sys

STOCK = bytes([0xE7, 0x80, 0x40, 0x4F])
DEFAULT_OFF = 0x1B54664


def scan(data: bytes, label: str, expected_off: int | None) -> None:
    print(f"=== {label} size=0x{len(data):x} ===")
    if expected_off is not None and expected_off + 4 <= len(data):
        got = data[expected_off : expected_off + 4]
        mark = "MATCH" if got == STOCK else "MISMATCH"
        print(
            f"  @+0x{expected_off:x}: {got.hex()}  ({mark}, expect {STOCK.hex()})"
        )
    hits = []
    start = 0
    while True:
        i = data.find(STOCK, start)
        if i < 0:
            break
        hits.append(i)
        start = i + 1
    print(f"  stock signature hits: {len(hits)}")
    for off in hits[:20]:
        print(f"    0x{off:08x}")
    if len(hits) > 20:
        print(f"    ... +{len(hits) - 20} more")
    if len(data) >= 4 and data[:4] == b"\x7fELF":
        print("  ELF magic at +0")


def main() -> int:
    if len(sys.argv) < 2:
        print(f"usage: {sys.argv[0]} <file> [expected_off]", file=sys.stderr)
        return 2
    path = pathlib.Path(sys.argv[1])
    expected = int(sys.argv[2], 0) if len(sys.argv) > 2 else DEFAULT_OFF
    data = path.read_bytes()
    scan(data, str(path), expected)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
