#!/usr/bin/env python3
"""Host PMA pin for the tail-steer corridor (optional, separate kill-switch).

RMCmpTailPin=1 (+ RMCmpTailSizeMB) pins [bigUserLimit-tailSize+1, bigUserLimit]
so user CUDA cannot collide with a steered GSP PT corridor.  Independent of
RMCmpTailSteer so host-free experiments can steer the GSCI map without
shrinking client capacity via a premature tail pin.

Mid phantom hole (CMP_MEM_RSV) stays until P3.

Idempotent via CMP_TAIL_PIN marker.
"""
from __future__ import annotations

import pathlib
import sys

MARKER = "CMP_TAIL_PIN"

# Insert right after the phantom-reserve pin block's closing.
ANCHOR = (
    '        else if (rsvDevId == 0x2082 && rsvEnable == 0)\n'
    '        {\n'
    '            NV_PRINTF(LEVEL_ERROR,\n'
    '                      "CMP_MEM_RSV: phantom guard DISABLED via RMCmpPhantomReserve=0\\n");\n'
    '        }\n'
    '    }\n'
)

PIN = r'''
    /*
     * cmpunlocker optional tail corridor pin.
     * NVreg RMCmpTailPin=1 (+ optional RMCmpTailSizeMB).  Default OFF —
     * enable only after confirming GSP pools moved into the tail.
     */
    {
        NvU32 tailDevId = pGpu->idInfo.PCIDeviceID >> 16;
        NvU32 tailPin = 0;
        NvU32 tailSizeMb = 4096;
        (void)osReadRegistryDword(pGpu, "RMCmpTailPin", &tailPin);
        (void)osReadRegistryDword(pGpu, "RMCmpTailSizeMB", &tailSizeMb);
        if (tailSizeMb < 512)
            tailSizeMb = 512;
        if (tailSizeMb > 16384)
            tailSizeMb = 16384;
        if (tailDevId == 0x2082 && tailPin != 0 && status == NV_OK &&
            pMemoryManager->pHeap != NULL &&
            pMemoryManager->pHeap->pPmaObject != NULL &&
            memmgrIsPmaInitialized(pMemoryManager))
        {
            /* Match live big user-region limit under 80G unlock. */
            const NvU64 tailEnd = 0x00000013f410ffffULL;
            NvU64 tailSize = ((NvU64)tailSizeMb) << 20;
            NvU64 tailBase = tailEnd - tailSize + 1;
            PMA *pTailPma = pMemoryManager->pHeap->pPmaObject;
            if (pmaIsPmaManaged(pTailPma, tailBase, tailEnd))
            {
                pmaSetBlockStateAttrib(pTailPma, tailBase, tailSize,
                                       STATE_PIN, STATE_MASK);
                NV_PRINTF(LEVEL_ERROR,
                          "CMP_TAIL_PIN: pinned [0x%llx,0x%llx] "
                          "(tailSizeMb=%u)\n",
                          tailBase, tailEnd, tailSizeMb);
            }
            else
            {
                NV_PRINTF(LEVEL_ERROR,
                          "CMP_TAIL_PIN: range not PMA-managed "
                          "[0x%llx,0x%llx]\n",
                          tailBase, tailEnd);
            }
        }
        else if (tailDevId == 0x2082)
        {
            NV_PRINTF(LEVEL_ERROR,
                      "CMP_TAIL_PIN: idle (RMCmpTailPin=%u)\n",
                      tailPin);
        }
    }
'''

# MIG zero-check: tolerate tail pin only when RMCmpTailPin is on.
# Phantom hole size must match apply_phantom_reserve.py (0x140000000 = 5G).
_PHANTOM_RSV = "0x140000000ULL"
_ZERO_TAIL = (
    '        if (freeMem + cmpPhantomRsv + cmpTailRsv != totalMem)\n'
)
ZERO_OLD_8G = (
    '        cmpPhantomRsv =\n'
    '            (((pGpu->idInfo.PCIDeviceID >> 16) == 0x2082) && cmpRsvEnable != 0)\n'
    '                ? 0x200000000ULL : 0;\n'
    '        if (freeMem + cmpPhantomRsv != totalMem)\n'
)
ZERO_OLD_5G = (
    '        cmpPhantomRsv =\n'
    '            (((pGpu->idInfo.PCIDeviceID >> 16) == 0x2082) && cmpRsvEnable != 0)\n'
    f'                ? {_PHANTOM_RSV} : 0;\n'
    '        if (freeMem + cmpPhantomRsv != totalMem)\n'
)

ZERO_NEW = (
    '        NvU32 cmpTailPin = 0;\n'
    '        NvU32 cmpTailSizeMb = 4096;\n'
    '        NvU64 cmpTailRsv = 0;\n'
    '        (void)osReadRegistryDword(pGpu, "RMCmpTailPin", &cmpTailPin);\n'
    '        (void)osReadRegistryDword(pGpu, "RMCmpTailSizeMB", &cmpTailSizeMb);\n'
    '        if (cmpTailSizeMb < 512) cmpTailSizeMb = 512;\n'
    '        if (cmpTailSizeMb > 16384) cmpTailSizeMb = 16384;\n'
    '        cmpPhantomRsv =\n'
    '            (((pGpu->idInfo.PCIDeviceID >> 16) == 0x2082) && cmpRsvEnable != 0)\n'
    f'                ? {_PHANTOM_RSV} : 0;\n'
    '        if (((pGpu->idInfo.PCIDeviceID >> 16) == 0x2082) && cmpTailPin != 0)\n'
    '            cmpTailRsv = ((NvU64)cmpTailSizeMb) << 20;\n'
    + _ZERO_TAIL
)

# Upgrade already-injected TailSteer-gated zero-check / pin if present.
ZERO_LEGACY_8G = (
    '        NvU32 cmpTailEnable = 0;\n'
    '        NvU32 cmpTailSizeMb = 4096;\n'
    '        NvU64 cmpTailRsv = 0;\n'
    '        (void)osReadRegistryDword(pGpu, "RMCmpTailSteer", &cmpTailEnable);\n'
    '        (void)osReadRegistryDword(pGpu, "RMCmpTailSizeMB", &cmpTailSizeMb);\n'
    '        if (cmpTailSizeMb < 512) cmpTailSizeMb = 512;\n'
    '        if (cmpTailSizeMb > 16384) cmpTailSizeMb = 16384;\n'
    '        cmpPhantomRsv =\n'
    '            (((pGpu->idInfo.PCIDeviceID >> 16) == 0x2082) && cmpRsvEnable != 0)\n'
    '                ? 0x200000000ULL : 0;\n'
    '        if (((pGpu->idInfo.PCIDeviceID >> 16) == 0x2082) && cmpTailEnable != 0)\n'
    '            cmpTailRsv = ((NvU64)cmpTailSizeMb) << 20;\n'
    + _ZERO_TAIL
)
ZERO_LEGACY_5G = (
    '        NvU32 cmpTailEnable = 0;\n'
    '        NvU32 cmpTailSizeMb = 4096;\n'
    '        NvU64 cmpTailRsv = 0;\n'
    '        (void)osReadRegistryDword(pGpu, "RMCmpTailSteer", &cmpTailEnable);\n'
    '        (void)osReadRegistryDword(pGpu, "RMCmpTailSizeMB", &cmpTailSizeMb);\n'
    '        if (cmpTailSizeMb < 512) cmpTailSizeMb = 512;\n'
    '        if (cmpTailSizeMb > 16384) cmpTailSizeMb = 16384;\n'
    '        cmpPhantomRsv =\n'
    '            (((pGpu->idInfo.PCIDeviceID >> 16) == 0x2082) && cmpRsvEnable != 0)\n'
    f'                ? {_PHANTOM_RSV} : 0;\n'
    '        if (((pGpu->idInfo.PCIDeviceID >> 16) == 0x2082) && cmpTailEnable != 0)\n'
    '            cmpTailRsv = ((NvU64)cmpTailSizeMb) << 20;\n'
    + _ZERO_TAIL
)

PIN_LEGACY_READ = (
    '        (void)osReadRegistryDword(pGpu, "RMCmpTailSteer", &tailEnable);\n'
)


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <mem_mgr.c>", file=sys.stderr)
        return 2
    path = pathlib.Path(sys.argv[1])
    text = path.read_text(encoding="utf-8")
    changed = False

    if MARKER not in text:
        if text.count(ANCHOR) != 1:
            print(f"pin anchor not unique ({text.count(ANCHOR)})", file=sys.stderr)
            return 1
        text = text.replace(ANCHOR, ANCHOR + PIN, 1)
        changed = True
    else:
        # Upgrade in-place if an older TailSteer-gated pin is present.
        if 'osReadRegistryDword(pGpu, "RMCmpTailSteer", &tailEnable)' in text:
            text = text.replace(
                '        NvU32 tailEnable = 0;\n'
                '        NvU32 tailSizeMb = 4096;\n'
                '        (void)osReadRegistryDword(pGpu, "RMCmpTailSteer", &tailEnable);\n',
                '        NvU32 tailPin = 0;\n'
                '        NvU32 tailSizeMb = 4096;\n'
                '        (void)osReadRegistryDword(pGpu, "RMCmpTailPin", &tailPin);\n',
                1,
            )
            text = text.replace('tailEnable != 0', 'tailPin != 0', 1)
            text = text.replace(
                '                      "CMP_TAIL_PIN: idle (RMCmpTailSteer=%u)\\n",\n'
                '                      tailEnable);\n',
                '                      "CMP_TAIL_PIN: idle (RMCmpTailPin=%u)\\n",\n'
                '                      tailPin);\n',
                1,
            )
            changed = True

    if "cmpTailRsv" not in text:
        if text.count(ZERO_OLD_5G) == 1:
            text = text.replace(ZERO_OLD_5G, ZERO_NEW, 1)
            changed = True
        elif text.count(ZERO_OLD_8G) == 1:
            text = text.replace(ZERO_OLD_8G, ZERO_NEW, 1)
            changed = True
        else:
            print("zero-check anchor missing (apply phantom_reserve first)", file=sys.stderr)
            return 1
    elif ZERO_LEGACY_5G in text:
        text = text.replace(ZERO_LEGACY_5G, ZERO_NEW, 1)
        changed = True
    elif ZERO_LEGACY_8G in text:
        text = text.replace(ZERO_LEGACY_8G, ZERO_NEW, 1)
        changed = True

    if not changed:
        print(f"{path}: already applied")
        return 0
    path.write_text(text, encoding="utf-8")
    print(f"{path}: injected/updated CMP_TAIL_PIN (RMCmpTailPin)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
