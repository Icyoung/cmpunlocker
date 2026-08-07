#!/usr/bin/env python3
"""Apply the selected CMP 170HX memory geometry to patched kernel_gsp.c.

The unlock patch always keeps the stable 8 GB card geometry (20c2 -> 64 GiB).
This helper selects only the 10 GB card (2082) target:

- 10gb / mixed:  40 GiB (stable default)
- 10gb80 / mixed80: 80 GiB (experimental, explicit opt-in)

It rewrites the actual C constants that are compiled into nvidia.ko. Metadata files
such as common/constants.yaml are deliberately not treated as build inputs.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class Geometry:
    cfg1: str
    lmr: str
    fb_bytes: str
    label: str
    experimental: bool


STABLE_2082 = Geometry(
    "0x02669000",
    "0x0000028A",
    "0x0000000A00000000",
    "20c2=64GB / 2082=40GB",
    False,
)
EXPERIMENTAL_2082_80GB = Geometry(
    "0x02779000",
    "0x0000028B",
    "0x0000001400000000",
    "20c2=64GB / 2082=80GB",
    True,
)
EXPERIMENTAL_2082_64GB = Geometry(
    "0x02779000",
    "0x0000020B",
    "0x0000001000000000",
    "20c2=64GB / 2082=64GB",
    True,
)

PROFILES: dict[str, Geometry] = {
    "8gb": STABLE_2082,
    "10gb": STABLE_2082,
    "mixed": STABLE_2082,
    "10gb80": EXPERIMENTAL_2082_80GB,
    "mixed80": EXPERIMENTAL_2082_80GB,
    "10gb64": EXPERIMENTAL_2082_64GB,
}

# The expressions are intentionally scoped to the exact dual-device blocks
# added by sec2-postbl-plm-ss-cfg.patch. Each must match exactly once.
GEOMETRY_BLOCK_RE = re.compile(
    r"(?P<prefix>"
    r"if\s*\(devId\s*==\s*SEC2_POSTBL_TIMING_CMP_170HX_8GB_PCI_DEVICE_ID\)\s*"
    r"\{\s*"
    r"cfg1Value\s*=\s*0x02779000U;\s*"
    r"lmrValue\s*=\s*0x0000020BU;\s*"
    r"\}\s*"
    r"else\s*"
    r"\{\s*"
    r"cfg1Value\s*=\s*)"
    r"0x[0-9A-Fa-f]+U;"
    r"(?P<middle>\s*lmrValue\s*=\s*)"
    r"0x[0-9A-Fa-f]+U;"
    r"(?P<suffix>\s*\})",
    re.MULTILINE,
)

FB_BYTES_RE = re.compile(
    r"(?P<prefix>"
    r"NvU64\s+targetFbBytes\s*=\s*"
    r"\(devId\s*==\s*SEC2_POSTBL_TIMING_CMP_170HX_8GB_PCI_DEVICE_ID\)\s*"
    r"\?\s*0x0000001000000000ULL\s*"
    r":\s*)"
    r"0x[0-9A-Fa-f]+ULL"
    r"(?P<suffix>\s*;)",
    re.MULTILINE,
)


def replace_exactly_once(
    pattern: re.Pattern[str], text: str, replacement: str, name: str
) -> str:
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        raise RuntimeError(f"{name} rewrite expected exactly one match, found {len(matches)}")
    return pattern.sub(replacement, text, count=1)


def apply_profile(source: pathlib.Path, profile: str) -> Geometry:
    try:
        geometry = PROFILES[profile]
    except KeyError as exc:
        raise RuntimeError(
            f"unsupported profile {profile!r}; choose one of: {', '.join(PROFILES)}"
        ) from exc

    text = source.read_text(encoding="utf-8")

    text = replace_exactly_once(
        GEOMETRY_BLOCK_RE,
        text,
        rf"\g<prefix>{geometry.cfg1}U;\g<middle>{geometry.lmr}U;\g<suffix>",
        "10 GB CFG1/LMR",
    )
    text = replace_exactly_once(
        FB_BYTES_RE,
        text,
        rf"\g<prefix>{geometry.fb_bytes}ULL\g<suffix>",
        "10 GB framebuffer length",
    )

    # Fail closed if the fixed 8 GB geometry or selected 10 GB geometry is not
    # present exactly as expected after rewriting.
    required = {
        "8 GB CFG1": "cfg1Value = 0x02779000U;",
        "8 GB LMR": "lmrValue  = 0x0000020BU;",
        "8 GB framebuffer": "? 0x0000001000000000ULL",
        "10 GB CFG1": f"cfg1Value = {geometry.cfg1}U;",
        "10 GB LMR": f"lmrValue  = {geometry.lmr}U;",
        "10 GB framebuffer": f": {geometry.fb_bytes}ULL;",
    }
    missing = [name for name, marker in required.items() if marker not in text]
    if missing:
        raise RuntimeError(f"profile verification failed; missing: {', '.join(missing)}")

    source.write_text(text, encoding="utf-8")
    return geometry


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=pathlib.Path, help="patched kernel_gsp.c")
    parser.add_argument("--profile", required=True, choices=tuple(PROFILES))
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if not args.source.is_file():
        print(f"profile source does not exist: {args.source}", file=sys.stderr)
        return 2

    try:
        geometry = apply_profile(args.source, args.profile)
    except (OSError, RuntimeError) as exc:
        print(f"profile application failed: {exc}", file=sys.stderr)
        return 1

    mode = "EXPERIMENTAL" if geometry.experimental else "stable"
    print(
        f"profile={args.profile} mode={mode} geometry={geometry.label} "
        f"2082_CFG1={geometry.cfg1} 2082_LMR={geometry.lmr} "
        f"2082_FB_BYTES={geometry.fb_bytes}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
