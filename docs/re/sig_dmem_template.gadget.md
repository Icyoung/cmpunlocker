# Refill gadget map — `sig_dmem_template.bin` (63488 bytes)

## Slot table

| DMEM off | Kind | Role | Value |
|---:|---|---|---:|
| `0x1100` | hdr | header / mode dword | `0x00000007` |
| `0x5b40` | marker | c0deca7e sentinel | `0xc0deca7e` |
| `0xf754` | writeValue | MMIO value (parameter) | `0xffffffff` |
| `0xf758` | marker | c0deca7e | `0xc0deca7e` |
| `0xf75c` | insn | falcon dword slot | `0x00000cbd` |
| `0xf76c` | writeAddr | MMIO address (parameter) | `0x009a0148` |
| `0xf774` | insn | falcon dword slot | `0x00001fbd` |
| `0xf780` | data | often zero | `0x00000000` |
| `0xf788` | insn | falcon dword slot | `0x000010aa` |
| `0xf78c` | insn | falcon dword slot | `0x0000815a` |
| `0xf790` | insn | falcon dword slot | `0x00008e18` |
| `0xf794` | marker | c0deca7e | `0xc0deca7e` |
| `0xf798` | insn | falcon dword slot | `0x0000815a` |
| `0xf79c` | data | often zero | `0x00000000` |
| `0xf7a0` | marker | c0deca7e | `0xc0deca7e` |
| `0xf7a4` | insn | falcon dword slot | `0x00001fbd` |
| `0xf7b0` | insn | falcon dword slot | `0x0000ffbc` |
| `0xf7b8` | insn | falcon dword slot | `0x0000582d` |
| `0xf7c4` | marker | c0deca7e | `0xc0deca7e` |
| `0xf7c8` | insn | falcon dword slot | `0x00000cbd` |
| `0xf7d8` | data | small const (3) | `0x00000003` |
| `0xf7e0` | insn | falcon dword slot | `0x00001fbd` |
| `0xf7f4` | insn | falcon dword slot | `0x00000ccb` |
| `0xf7f8` | insn | falcon dword slot (tail) | `0x00007f2f` |

## Tail cluster (0xf730..0xf800)

```
0000f730: a7 04 00 00 a7 04 00 00 a7 04 00 00 a7 04 00 00
0000f740: a7 04 00 00 a7 04 00 00 a7 04 00 00 a7 04 00 00
0000f750: a7 04 00 00 ff ff ff ff 7e ca de c0 bd 0c 00 00
0000f760: a7 04 00 00 a7 04 00 00 a7 04 00 00 48 01 9a 00
0000f770: a7 04 00 00 bd 1f 00 00 a7 04 00 00 a7 04 00 00
0000f780: 00 00 00 00 a7 04 00 00 aa 10 00 00 5a 81 00 00
0000f790: 18 8e 00 00 7e ca de c0 5a 81 00 00 00 00 00 00
0000f7a0: 7e ca de c0 bd 1f 00 00 a7 04 00 00 a7 04 00 00
0000f7b0: bc ff 00 00 a7 04 00 00 2d 58 00 00 a7 04 00 00
0000f7c0: a7 04 00 00 7e ca de c0 bd 0c 00 00 a7 04 00 00
0000f7d0: a7 04 00 00 a7 04 00 00 03 00 00 00 a7 04 00 00
0000f7e0: bd 1f 00 00 a7 04 00 00 a7 04 00 00 a7 04 00 00
0000f7f0: a7 04 00 00 cb 0c 00 00 2f 7f 00 00 a7 04 00 00
```

## Interpretation notes

- Template is **DMEM data**, not a linear IMEM image. App code (encrypted)
  interprets dword slots; `0xf754`/`0xf76c` are the only host-parameterized
  fields today (MMIO write gadget).
- `0xc0deca7e` markers delimit gadget chunks; `0x000004a7` fill is NOP sled.
- Booter OS writes `DMEM[0x6140]=0xd2408` before `lcall 0x100`. That constant
  is **not** an offset into this 0xf800 buffer — likely a packed IMEM/entry VA
  consumed together with `sysmemAddrOfSignature` from WPR meta.

## Verify-bypass experiment ideas

1. `RMCmpDmemSlotOff` / `RMCmpDmemSlotVal` — poke extra template dwords
   (see `apply_booter_verify_bypass.py`).
2. Clone MMIO gadget to a second tail (e.g. `0xe754`/`0xe76c`) targeting
   secure IMEM port setup instead of PRI MMIO — needs app interpreter RE.
3. Do **not** change OS theater `mbox=0x31` — breaks PLM loop.

