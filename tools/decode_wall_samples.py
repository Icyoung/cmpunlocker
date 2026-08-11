#!/usr/bin/env python3
"""Decode wall_reconfirm sample lines: invert pattern() on `actual` values
to find the source logical offset of the polluting write, and print the
alias delta per sample.

pattern(x) = (x * K mod 2^64) ^ (x >> 3), K = 0x9E3779B97F4A7C15.
The map is not injective; we enumerate all preimages and keep plausible
ones (< 128 GiB). High-mode values are pat(x)|HIGH_MARK with bit63 of
pat(x) unknown, so both bit63 variants are tried.

Usage: decode_wall_samples.py <logfile> [<logfile>...]
"""
import re
import sys

K = 0x9E3779B97F4A7C15
M = 1 << 64
MARK = 1 << 63
GB = 1024 ** 3
PLAUSIBLE = 128 * GB


def pat(x: int) -> int:
    return ((x * K) % M) ^ (x >> 3)


def invert_all(y: int):
    """All x with pat(x) == y, via LSB-first constraint propagation."""
    sols = []
    for lo in range(8):  # guess bits 0..2
        x = lo
        for i in range(61):  # x_{i+3} = y_i ^ (x*K)_i
            zi = ((x * K) >> i) & 1
            x |= (((y >> i) & 1) ^ zi) << (i + 3)
        if pat(x) == y:
            sols.append(x)
    return sols


def solve(actual: int):
    """Return list of (kind, src) preimages for a sample `actual` value."""
    out = []
    if actual & MARK:
        for cand in (actual & ~MARK, actual | MARK):
            for x in invert_all(cand):
                if (pat(x) | MARK) == actual:
                    out.append(("H", x))
    else:
        for x in invert_all(actual):
            out.append(("L", x))
    return out


SAMPLE_RE = re.compile(
    r"sample\[\d+\]\s+(\S+)\s+addr=0x([0-9a-f]+)\s+expected=0x([0-9a-f]+)"
    r"\s+actual=0x([0-9a-f]+)")


def main():
    for path in sys.argv[1:]:
        print(f"=== {path} ===")
        with open(path) as f:
            for line in f:
                m = SAMPLE_RE.search(line)
                if not m:
                    continue
                tag, addr_h, _exp, act_h = m.groups()
                addr, actual = int(addr_h, 16), int(act_h, 16)
                sols = [(k, s) for k, s in solve(actual) if s < PLAUSIBLE]
                if not sols:
                    print(f"{tag} addr={addr:#012x} actual={actual:#018x}"
                          f" -> NO plausible preimage")
                    continue
                for kind, src in sols:
                    d = src - addr
                    print(f"{tag} addr={addr:#012x} ({addr / GB:8.4f}G)"
                          f" {kind} src={src:#012x} ({src / GB:8.4f}G)"
                          f" delta={d:#014x} ({d / GB:+.5f}G)"
                          f" xor={addr ^ src:#014x}")
        print()


if __name__ == "__main__":
    main()
