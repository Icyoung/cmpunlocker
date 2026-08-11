# Patch A RE verification (tu10x / CMP 170HX)

> 2026-08-09. Source: `/lib/firmware/nvidia/610.43.02/gsp_tu10x.bin` fwimage @ container+0x40.

## Patch site

| Field | Value |
|-------|-------|
| fwimage offset | `0x1b54664` |
| Stock bytes | `e7 80 40 4f` → `jalr ra, ra, 0x4f4` |
| Patch bytes | `13 05 00 00` → `addi a0, zero, 0` (skip indirect call) |
| Preceding | `auipc ra, 0xff4d3` @ `0x1b54660` |

## Resolved call target

```
auipc @ 0x1b54660  →  ra = 0x1027660
jalr  @ 0x1b54664  →  target = 0x1027b54
```

**`0x1027b54` = `dmaUpdateVASpace_GF100` landmark** (prior tu10x RE). Delta **0**.

## Surrounding control flow (chunkloop)

```
0x1b54664: jalr  → dmaUpdateVASpace     ← patch A NOPs this
0x1b54668: add   s2, s2, s4            ; advance chunk index
0x1b5466c: sext.w a5, a0               ; call return value
0x1b54670: bltu  s2, s6, -0x9c         ; loop back
0x1b54674: bnez  a5, error_path        ; dmaUpdate failure
```

After patch: `a0=0`, error branch not taken, loop continues without updating VASpace.

## Conclusion

- **Offset is correct** — not a relocation mistake; same semantic as ga10x patch A (NOP chunkloop → dmaUpdateVASpace).
- **Runtime hang with forgive** is NOT explained by wrong address:
  1. **Verify path**: `lcall 0x100` app may abort before starting GSP-RM when sig fails; host forgive only clears mbox — firmware never runs.
  2. **Patch semantics**: global NOP of `dmaUpdateVASpace` in this loop may break GSP init (same loop runs during boot), not only the 32G seg2 bug path.

## IMEM dump RE (2026-08-09, negative)

Host SEC2 IMEM reads of encrypted app @ `0x100` return **all zeros** (secure IMEM
not visible post-halt or during pre-halt poll). DMEM post-halt is `0xdead5ec2`
scrub. Static bindata `csigcmp` hits in app region are encrypted noise; only OS
plaintext @ `0x76` is real (`lcall 0xd6` theater path).

## Next steps

1. **Post-verify CE patch** — `RMCmpGspFwPatchPostBoot=1` copies patch A via
   `memmgrMemCopy` after stock BooterLoad (BAR0 PRAMIN was negative).
2. **In-app csigcmp bypass** — blocked until decrypted app IMEM is obtainable.
3. **Narrower patch** — predicate NOP on seg2 / multi-chunk only.

## Test regkeys (current driver)

```bash
# Forgive only — stock GSP, harmless:
RMCmpBooterForceMbox0=1

# Patch A + forgive — Booter 0x0, nvidia-smi hangs:
RMCmpGspFwPatchA=1;RMCmpBooterForceMbox0=1

# Do NOT use with skip-app (skips lcall 0x100 entirely).
```
