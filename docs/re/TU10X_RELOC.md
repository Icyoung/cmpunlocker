# tu10x GSP-RM landmark relocation (2026-08-09)

> Method: structural fingerprinting (stack frame / loop opcode) on
> `gsp_analysis/gsp_rm_tu10x.elf` RM text (ELF+`0xbf7000`, size `0xff1000`).
> Container offset in `gsp_tu10x.bin` = ELF offset + `0x40`.

| Symbol | ga10x VA | tu10x ELF | tu10x container | Confidence |
|---|---:|---:|---:|---|
| `dmaUpdateVASpace_GF100` | `0x012ebc5c` | `0x01027b54` | `0x01027b94` | **high** — `addi sp,-0x2c0` prologue + identical save sequence |
| `chunkloop` | `0x01acba1c` | `0x01b5441c` | `0x01b5445c` | **high** — `addi sp,-0x170` + `bltu s2,s6,-0x9c` loop tail |
| patch A (`jalr`→`dmaUpdateVASpace`) | `0x01acbbe2` | `0x01b54664` | `0x01b546a4` | **medium** — loop body match; NOP `jalr` (4 B) → `addi a0,zero,0` |

## Patch A bytes (tu10x)

At image offset `0x01b54664` (container `0x01b546a4` in `gsp_tu10x.bin`):

| | Bytes |
|---|---|
| stock | `e7 80 40 4f` (`jalr ra, ra, 0x4f4`) |
| patch A | `13 05 00 00` (`addi a0, zero, 0` → NV_OK) |

**Blocked on disk**: Booter `0xb`. Host RAM hook: `driver/apply_gsp_radix3_patch.py` + `RMCmpGspFwPatchA=1`.

## Still open

- `dmaPageArrayGetPhysAddr`, `dmaMapMemory`, `dmaAllocMap`, `_Direct` — no unique anchor yet
- Rogue site likely RPC handler → `dmaAllocMap` NVOC layer (see `RE_FINDINGS.md` §4.2)

## Tools

- `tu10x_relocate.py` — masked pattern (weak on this build); prefer capstone scans documented here.
