#!/usr/bin/env python3
"""
patch_gr_unlock.py — enable the Graphics (GR) engine on CMP 90HX / 70HX by
byte-patching the closed-source nv-kernel.o_binary from NVIDIA driver
610.43.02.

CMP 90HX (10de:220d) and CMP 70HX (10de:248a) are GA102-based silicon —
the same die as RTX 3080 Ti / RTX 3090.  The GR engine (rasterization,
3D pipeline, display) is present in hardware but the stock driver refuses
to bring it up because of the CMP SKU marker.  This patch flips three
gates in the CPU-side RM (kernel/nvidia/nv-kernel.o_binary) so that the
GR engine initializes normally.

What this tool does NOT do:
- It does not touch the GA100-based CMP 170HX (that card has no display
  silicon; the block is architectural, not software).
- It does not touch the open-gpu-kernel-modules driver — that codebase
  has a different structure and the closed CPU-side property is not
  exposed there.  If you use the open driver (cmpunlocker main path),
  keep using it for 170HX and use this tool on a separate 610.43.02
  closed driver install intended for 90HX / 70HX display use.
- It does not add symbols to .symtab / .strtab (the reference patch
  from dm's GreenDamTan release does; those are debug decorations and
  do not affect runtime behavior — we skip them for simplicity).

Method (functional-equivalent to dm's GreenDamTan reference build):
1. In .text, patch three sites in-place:
     * _nv029797rm+0x4     7 bytes   jump to appended code cave
     * _nv032289rm+0x8d    6 bytes   je -> unconditional jmp
     * _nv031940rm+0x4     5 bytes   xor eax,eax; inc eax; ret
2. Append a 124-byte code cave to .text.  The cave reads a GR-enable
   property (call _nv052448rm), ORs bit 12 (0x1000) into the value,
   writes it back (call _nv052453rm), reads it back for verification,
   then jumps back to _nv029797rm+0xb to resume normal execution.
3. Adjust ELF section-header offsets and e_shoff so that the section
   table still refers to the correct data.

The reference (dm's blob) rebuilt the ELF with ld and rearranged .data
vs .rodata plus added a .note.GNU-stack section header — cosmetic
changes we do not reproduce, so output MD5 will not match the reference
exactly.  Byte-for-byte comparison at the three patch sites and inside
the code cave will match.

Verification of a produced blob:
  * Byte content of the three patch sites (see PATCH_SITES) matches
  * Last 124 bytes of .text match the code cave (see CODE_CAVE)
  * All non-.text bytes in the shifted regions are identical to the
    stock blob's corresponding bytes
  * ELF is still parseable and sections still resolve to matching data
"""

import argparse
import hashlib
import os
import struct
import sys
from pathlib import Path

STOCK_BLOB_MD5 = "203b7f999395d22e041f6ed50e0d5646"

PATCH_SITES = [
    # (name, .text file offset, stock bytes, patched bytes)
    (
        "_nv029797rm+0x4 (jump to code cave)",
        0x581234,
        bytes.fromhex("41554154 4989fc"),
        bytes.fromhex("e9279184 009090"),
    ),
    (
        "_nv032289rm+0x8d (je -> jmp)",
        0x5AB57D,
        bytes.fromhex("0f84fd02 0000"),
        bytes.fromhex("e9fe0200 0090"),
    ),
    (
        "_nv031940rm+0x4 (return 1)",
        0x5F2134,
        bytes.fromhex("4157 4531c0"),
        bytes.fromhex("31c0 ffc0c3"),
    ),
]

# 124-byte code cave appended to .text.  Do not edit; this exact
# sequence contains hard-coded PC-relative call/jmp displacements
# to _nv052448rm, _nv052453rm and back to _nv029797rm+0xb.  The
# displacements are relative to the code cave's placement at
# .text offset 0xdca354 (i.e. immediately after the last byte of
# stock .text).
CODE_CAVE = bytes.fromhex(
    "662e0f1f8400000000006690f30f1efa"
    "415541544989fc4883ec08498dbc2460"
    "43000031f631d2b9000200004531c0e8"
    "98c078ff4189c54181cd00100000498d"
    "bc246043000031f631d2b90002000045"
    "89e84531c9e862b978ff498dbc246043"
    "000031f631d2b9000200004531c0e859"
    "c078ff4883c408e96b6e7bff"
)
assert len(CODE_CAVE) == 124

# The stock .text section header info we rely on (values from ELF).
STOCK_TEXT_OFFSET = 0x40       # .text sh_offset
STOCK_TEXT_SIZE = 0xDCA354     # .text sh_size (14459732)


def md5file(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_elf_header(buf):
    if buf[:4] != b"\x7fELF":
        raise ValueError("not an ELF file")
    if buf[4] != 2:
        raise ValueError("not ELF64")
    if buf[5] != 1:
        raise ValueError("not little-endian")
    (e_shoff,) = struct.unpack_from("<Q", buf, 0x28)
    e_shentsize, e_shnum = struct.unpack_from("<HH", buf, 0x3A)
    e_shstrndx = struct.unpack_from("<H", buf, 0x3E)[0]
    return {
        "e_shoff": e_shoff,
        "e_shentsize": e_shentsize,
        "e_shnum": e_shnum,
        "e_shstrndx": e_shstrndx,
    }


def parse_section_headers(buf, hdr):
    sects = []
    base = hdr["e_shoff"]
    for i in range(hdr["e_shnum"]):
        off = base + i * hdr["e_shentsize"]
        (
            sh_name,
            sh_type,
            sh_flags,
            sh_addr,
            sh_offset,
            sh_size,
            sh_link,
            sh_info,
            sh_addralign,
            sh_entsize,
        ) = struct.unpack_from("<IIQQQQIIQQ", buf, off)
        sects.append(
            {
                "index": i,
                "sh_name": sh_name,
                "sh_type": sh_type,
                "sh_flags": sh_flags,
                "sh_addr": sh_addr,
                "sh_offset": sh_offset,
                "sh_size": sh_size,
                "sh_link": sh_link,
                "sh_info": sh_info,
                "sh_addralign": sh_addralign,
                "sh_entsize": sh_entsize,
                "hdr_file_offset": off,
            }
        )
    # Resolve names
    shstrtab_off = sects[hdr["e_shstrndx"]]["sh_offset"]
    for s in sects:
        end = buf.index(b"\x00", shstrtab_off + s["sh_name"])
        s["name"] = buf[shstrtab_off + s["sh_name"] : end].decode("ascii")
    return sects


def apply_gr_unlock(stock_blob: bytes) -> bytes:
    """Return a bytes object with the GR-unlock patch applied.

    The stock blob must be the exact NVIDIA 610.43.02 closed-driver
    nv-kernel.o_binary (MD5 203b7f999395d22e041f6ed50e0d5646).
    """
    buf = bytearray(stock_blob)
    hdr = parse_elf_header(buf)
    sects = parse_section_headers(buf, hdr)

    text = next((s for s in sects if s["name"] == ".text"), None)
    if text is None:
        raise RuntimeError("no .text section found")
    if text["sh_offset"] != STOCK_TEXT_OFFSET:
        raise RuntimeError(
            f".text sh_offset unexpected: 0x{text['sh_offset']:x} "
            f"(expected 0x{STOCK_TEXT_OFFSET:x})"
        )
    if text["sh_size"] != STOCK_TEXT_SIZE:
        raise RuntimeError(
            f".text sh_size unexpected: 0x{text['sh_size']:x} "
            f"(expected 0x{STOCK_TEXT_SIZE:x})"
        )

    # 1. Verify then apply the three in-place patches.
    for name, off, stock_bytes, patched_bytes in PATCH_SITES:
        assert len(stock_bytes) == len(patched_bytes), name
        file_off = text["sh_offset"] + off
        actual = bytes(buf[file_off : file_off + len(stock_bytes)])
        if actual != stock_bytes:
            raise RuntimeError(
                f"{name}: stock bytes at .text+0x{off:x} do not match "
                f"expected — got {actual.hex()}, want {stock_bytes.hex()}"
            )
        buf[file_off : file_off + len(patched_bytes)] = patched_bytes

    # 2. Insert code cave at the current end of .text.  Everything after
    #    the .text section (data, symtab, strtab, relas, section-header
    #    table) shifts down by len(CODE_CAVE) bytes.
    insert_at = text["sh_offset"] + text["sh_size"]
    shift = len(CODE_CAVE)
    new_buf = bytearray()
    new_buf += buf[:insert_at]
    new_buf += CODE_CAVE
    new_buf += buf[insert_at:]

    # 3. Grow .text sh_size.  Elf64_Shdr layout: sh_name(4)+sh_type(4)+
    #    sh_flags(8)+sh_addr(8)+sh_offset(8)+sh_size(8)+...  so sh_size
    #    lives at +32, sh_offset at +24.  The .text section header did
    #    not move (it sits inside the section-header table, which moved
    #    as a block later); its new file position is hdr_file_offset +
    #    shift because the whole shdr table shifted along with the rest.
    new_text_size = text["sh_size"] + shift
    new_text_hdr_off = text["hdr_file_offset"] + shift
    struct.pack_into("<Q", new_buf, new_text_hdr_off + 32, new_text_size)

    # 4. Bump sh_offset for every section physically after the insertion
    #    point.  The section-header table itself is somewhere at the
    #    end of the file; its position is stored in e_shoff and must
    #    also be bumped.
    new_shoff = hdr["e_shoff"] + shift
    for s in sects:
        if s["sh_offset"] > text["sh_offset"]:
            # section starts after .text — its file bytes moved down
            new_off = s["sh_offset"] + shift
            # The section-header table entries themselves live at
            # hdr_file_offset, which is inside the file region that
            # got moved.  Update them at their NEW positions.
            new_hdr_off = s["hdr_file_offset"] + shift
            struct.pack_into("<Q", new_buf, new_hdr_off + 24, new_off)

    # Update e_shoff in ELF header (unchanged file position — the
    # ELF header itself did not move).
    struct.pack_into("<Q", new_buf, 0x28, new_shoff)

    return bytes(new_buf)


def verify_patched(patched_blob: bytes) -> None:
    """Sanity-check a produced patched blob."""
    hdr = parse_elf_header(patched_blob)
    sects = parse_section_headers(patched_blob, hdr)
    text = next((s for s in sects if s["name"] == ".text"), None)
    if text is None:
        raise RuntimeError("output has no .text")
    if text["sh_size"] != STOCK_TEXT_SIZE + len(CODE_CAVE):
        raise RuntimeError(
            f"output .text size {text['sh_size']} != expected "
            f"{STOCK_TEXT_SIZE + len(CODE_CAVE)}"
        )
    # Verify each patch site has the patched bytes
    for name, off, _stock, patched in PATCH_SITES:
        file_off = text["sh_offset"] + off
        actual = patched_blob[file_off : file_off + len(patched)]
        if actual != patched:
            raise RuntimeError(f"verify: {name} bytes mismatch")
    # Verify code cave at end of .text
    cave_off = text["sh_offset"] + STOCK_TEXT_SIZE
    if patched_blob[cave_off : cave_off + len(CODE_CAVE)] != CODE_CAVE:
        raise RuntimeError("verify: code cave bytes mismatch")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=(
            "Patch NVIDIA 610.43.02 closed-driver nv-kernel.o_binary "
            "to enable the Graphics engine on CMP 90HX / 70HX."
        )
    )
    ap.add_argument(
        "input",
        type=Path,
        help="Path to stock nv-kernel.o_binary (MD5 must match 610.43.02).",
    )
    ap.add_argument(
        "output",
        type=Path,
        help="Path to write the patched nv-kernel.o_binary.",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="Skip the input MD5 check (dangerous — use only if you "
        "have verified equivalence with a different-hashed but "
        "structurally identical 610.43.02 blob).",
    )
    args = ap.parse_args(argv)

    if not args.input.is_file():
        print(f"error: input file not found: {args.input}", file=sys.stderr)
        return 2

    got_md5 = md5file(args.input)
    if not args.force and got_md5 != STOCK_BLOB_MD5:
        print(
            f"error: input MD5 {got_md5} != expected {STOCK_BLOB_MD5}\n"
            f"       Expected the closed-driver nv-kernel.o_binary from "
            f"NVIDIA-Linux-x86_64-610.43.02.run.\n"
            f"       Re-run with --force to skip if you know what you "
            f"are doing.",
            file=sys.stderr,
        )
        return 2

    stock = args.input.read_bytes()
    print(
        f"[info] input:  {args.input}  ({len(stock)} bytes, MD5 {got_md5})",
        file=sys.stderr,
    )
    patched = apply_gr_unlock(stock)
    verify_patched(patched)
    args.output.write_bytes(patched)
    out_md5 = hashlib.md5(patched).hexdigest()
    print(
        f"[ok]   output: {args.output}  ({len(patched)} bytes, "
        f"MD5 {out_md5})",
        file=sys.stderr,
    )
    print(
        "[note] Output MD5 will NOT match dm's GreenDamTan reference "
        "(that build re-linked with ld and rearranged sections).  "
        "Functional behavior is identical: the three patch sites and "
        "the code cave contain identical bytes.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
