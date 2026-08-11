#!/usr/bin/env python3
"""Forgive Booter mailbox0 verify-fail when testing patched GSP-RM.

Regkey: RMCmpBooterForceMbox0=1 (shared with OS RAM stub in apply_booter_os_force_mbox.py)

When mailbox0==0xb after kgspExecuteHsFalcon_HAL, log and continue as success.
Use with RMCmpGspFwPatchA=1 after OS stub fails to clear mbox (app halts on verify).
"""
from __future__ import annotations

import pathlib
import sys

MARK = "CMP_BOOTER_MBOX_FORGIVE"

ANCHOR = (
    "    if (mailbox0 != 0)\n"
    "    {\n"
    "        NV_PRINTF(LEVEL_ERROR, \"Booter failed with non-zero error code: 0x%x\\n\", mailbox0);\n"
    "        return NV_ERR_GENERIC;\n"
    "    }\n"
)

PATCH = (
    "    if (mailbox0 != 0)\n"
    "    {\n"
    "        NvU32 forgiveMbox = 0;\n"
    "        NvU32 gspDevId = pGpu->idInfo.PCIDeviceID >> 16;\n"
    "\n"
    "        (void)osReadRegistryDword(pGpu, \"RMCmpBooterForceMbox0\", &forgiveMbox);\n"
    "        if (gspDevId == 0x2082 && forgiveMbox != 0 && mailbox0 == 0xb)\n"
    "        {\n"
    "            NV_PRINTF(LEVEL_ERROR,\n"
    "                      \"CMP_BOOTER_MBOX_FORGIVE: ignoring verify-fail mbox=0x%x\\n\",\n"
    "                      mailbox0);\n"
    "            mailbox0 = 0;\n"
    "        }\n"
    "        else\n"
    "        {\n"
    "            NV_PRINTF(LEVEL_ERROR, \"Booter failed with non-zero error code: 0x%x\\n\", mailbox0);\n"
    "            return NV_ERR_GENERIC;\n"
    "        }\n"
    "    }\n"
)


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <kernel_gsp_booter_tu102.c>", file=sys.stderr)
        return 2
    path = pathlib.Path(sys.argv[1])
    text = path.read_text()
    if MARK in text:
        print(f"{path}: already patched")
        return 0
    if ANCHOR not in text:
        print(f"{path}: anchor not found", file=sys.stderr)
        return 1
    path.write_text(text.replace(ANCHOR, PATCH, 1))
    print(f"{path}: inserted mbox 0xb forgive (RMCmpBooterForceMbox0=1)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
