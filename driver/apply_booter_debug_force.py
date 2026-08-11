#!/usr/bin/env python3
"""Force GSP Booter DBG image when fuse debug is disabled.

Regkey: NVreg_RegistryDwords="RMCmpBooterDbg=1"

Patches kgspIsDebugModeEnabled_{GA100,TU102} so BINDATA_LABEL_IMAGE_DBG is used.
"""
from __future__ import annotations

import pathlib
import sys

ANCHOR = (
    "    data = GPU_REG_RD32(pGpu, NV_FUSE_OPT_SECURE_GSP_DEBUG_DIS);\n"
    "\n"
    "    return FLD_TEST_DRF(_FUSE, _OPT_SECURE_GSP_DEBUG_DIS, _DATA, _NO, data);\n"
)

PATCH = (
    "    NvU32 forceBooterDbg = 0;\n"
    "\n"
    "    (void)osReadRegistryDword(pGpu, \"RMCmpBooterDbg\", &forceBooterDbg);\n"
    "    if (forceBooterDbg != 0)\n"
    "    {\n"
    "        NV_PRINTF(LEVEL_ERROR, \"CMP_BOOTER_DBG: forcing DBG booter image\\n\");\n"
    "        return NV_TRUE;\n"
    "    }\n"
    "\n"
    "    data = GPU_REG_RD32(pGpu, NV_FUSE_OPT_SECURE_GSP_DEBUG_DIS);\n"
    "\n"
    "    return FLD_TEST_DRF(_FUSE, _OPT_SECURE_GSP_DEBUG_DIS, _DATA, _NO, data);\n"
)


def patch_file(path: pathlib.Path) -> bool:
    text = path.read_text()
    if "CMP_BOOTER_DBG:" in text:
        print(f"{path}: already patched")
        return True
    if ANCHOR not in text:
        print(f"{path}: anchor not found", file=sys.stderr)
        return False
    path.write_text(text.replace(ANCHOR, PATCH, 1))
    print(f"{path}: inserted RMCmpBooterDbg force-DBG hook")
    return True


def main() -> int:
    if len(sys.argv) < 2:
        print(f"usage: {sys.argv[0]} <kernel_gsp_*.c> [...]", file=sys.stderr)
        return 2
    ok = True
    for arg in sys.argv[1:]:
        ok = patch_file(pathlib.Path(arg)) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
