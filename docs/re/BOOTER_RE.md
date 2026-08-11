# Booter RE — TU102 OS plaintext control flow

> Source: `booter_tu102_image_prod.bin` OS region `0x0..0xff` (envydis fuc5).
> App region `0x100..0x84ff` is encrypted in bindata; runtime IMEM dump post-halt is zeroed.

## Layout (59136B image)

| Offset | Size | Role |
|--------|------|------|
| `0x000` | 256 | OS code (plaintext Falcon) |
| `0x100` | 0x8400 | App code (encrypted) — GSP-RM load + verify (`lcall 0x100`) |
| `0x8500` | 0x6200 | OS data (DMEM image) |
| `0x8700` | 16 | Booter HS signature slot (`s_patchBooterUcodeSignature`) |

## Normal boot OS flow (disasm)

```
0x00: init mailboxes, lcall 0x2f
0x2f: gate on DMEM[0x200..0x20c] — non-zero -> theater @0x76
0x76: lcall 0xd6; mov r15,0x31; iowrs mbox0  (PLM theater fail)
      xdst setup ...
0xcc: lcall 0x100   ← encrypted app: load + verify GSP-RM
0xd0: exit          ← driver reads mailbox0; 0xb = verify fail
```

## Mbox error codes (observed)

| Code | Meaning |
|------|---------|
| `0x31` | Intentional PLM theater fail (expected during refill) |
| `0xb` | GSP-RM signature / verify fail (patch A triggers this) |
| `0x5` | Gadget-as-signature format reject (post-PLM bypass) |
| `0x0` | Success |

## Patch strategies

| Approach | Keeps `lcall 0x100` | Result |
|----------|---------------------|--------|
| `RMCmpBooterSkipApp=1` | **No** (exit @0xcc) | Booter 0x0 but GSP-RM never runs → `GspMsgQueue` hang |
| `RMCmpBooterVerifyBypass` | Yes | Gadget template; mbox `0x5` / `0xffff` |
| **`RMCmpBooterForceMbox0=1`** | **Yes** | OS stub @0xf4 (app often halts before stub); **driver forgive 0xb** works |

### Force-mbox0 patch bytes

```
0x7b:  31 -> 00           theater mbox success
0xd0:  f8 02 -> 7e f4 00 00   exit -> lcall stub
0xf4:  0f 00 49 00 10 f7 9f 00 f8 02   mov r15,0; mbox iowrs; exit
```

## Host driver check

`kernel_gsp_booter_tu102.c:s_executeBooterUcode_TU102` fails if `mailbox0 != 0` after
`kgspExecuteHsFalcon_HAL`, regardless of NV_STATUS.

## Test matrix (CMP 170HX, FLR)

```bash
# patch A + force mbox (NOT skip-app):
NVreg_RegistryDwords="...;RMCmpGspFwPatchA=1;RMCmpBooterForceMbox0=1"
```

Expected: `CMP_GSP_PATCH` + `CMP_BOOTER_FORCE_MBOX0` + `CMP_BOOTER_MBOX_FORGIVE` + `BooterLoad status=0x0`.

**2026-08-09 test**: forgive path reaches `status=0x0` with patch A; `nvidia-smi` still hangs at
`GspMsgQueueSendCommand` — verify fail likely aborts before GSP-RM is fully live, or patch A breaks runtime.
