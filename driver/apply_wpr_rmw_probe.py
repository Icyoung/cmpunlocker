#!/usr/bin/env python3
"""Post-BooterLoad WPR RMW smoke probe — EXPERIMENT CONCLUDED (2026-08-08).

Result (GA100 / CMP 170HX, after normal BooterLoad status=0x0):

  ctrl@FB+1MiB via kbusMemAccessBar0Window: stick=1  (PRAMIN write path works)
  wpr@gspFwOffset via same path:
    - readback is NOT ELF magic (got 0x01/0x02, expect 0x7f)
    - write does NOT stick (stick=0)
    - even READ-ONLY PRAMIN access to WPR breaks subsequent GSP init
      (kgspSendInitRpcs / Max GSP-RM boot attempts exceeded)

Conclusion: host cannot patch the Booter-verified WPR-resident GSP-RM image
via BAR0 PRAMIN. Do not re-enable this inject without a new write path.

This helper is intentionally a no-op so build.sh can keep referencing it.
"""
from __future__ import annotations

import pathlib
import sys


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <kernel_gsp_tu102.c>", file=sys.stderr)
        return 2
    path = pathlib.Path(sys.argv[1])
    print(f"{path}: CMP_WPR_RMW probe disabled (experiment negative — see script docstring)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
