# EXFIL_RE — post-seal exfil from GSP-RM (2026-08-11)

> Target: `gsp_analysis/gsp_rm_tu10x.elf`; tooling `gsp_analysis/re2/`.
> **Verdict up front: Route 1 (patch the lock writer) is dead — no writer exists in any
> plaintext firmware image. Route 2/3 (register-window mailbox exfil) is concrete and
> generated below (v45 stub, bytes verified).**

## 1. Route 1 — the SEC2 window lock (0x840240: 0x3000→0x7021, 0x840250: 0xf→0)

Exhaustive negative evidence (full disasm of RM text PH25 + rodata PH26/PH27 + libos PH0 +
data-level scans of the whole ELF + the three booter images):

- No `0x7021`, `0x4021`, `0x840240`, `0x840250` dword anywhere in the ELF; the only
  `lui 0x7021` in code is a multiply coefficient (0x7021039), as v50 already noted.
- No code builds 0x7021/0x4021 (all `lui 0x7 + addi 0x21`-class sequences scanned).
- All 25 SEC2-base (`lui 0x840`) sites traced: the SEC2 access pattern is
  `ld base, field(struct); add base, 0x840000; lw/sw off(base)` — descriptor-driven as
  suspected — but the offsets used are only 0x100/0x12c/0x168/0x170/0x330/0x3c0/0x3c4/0x3e4/
  0x408/0x700/0x708/0x714/0x738/0x748/0x750 (reset, DMA engine control, mailboxes).
  **None touches 0x240/0x250.** No `lui 0x840; addi +0x240/0x250` address formation either
  (helper-call form excluded too).
- libos (PH0, `re2/disasm_libos.txt`): no 0x840 reference, no 0x240/0x250 store, no 0x7021.
- Booter images (`booter_load_ga100_prod.bin`, `booter_tu102_image_{prod,dbg}.bin`): no
  matching constants in the plaintext parts; the app portion is encrypted and cannot be
  excluded — but the lock appears *after* the final BooterLoad (arm snapshot still 0x3000),
  so the SEC2 booter is timing-excluded anyway.

**Conclusion: the write is not in any image we hold.** It is either hardware-set at the
RISC-V secure transition, or done by the encrypted SEC2 ucode that GSP-RM launches post-boot
(see §4 — GSP does drive SEC2 after boot). There is no byte offset to NOP. Residual
possibility: a stub-side *revert* write (§3.4) — cheap to try once, but if the lock bits are
sticky-until-reset it is silently dropped.

## 2. How GSP-RM addresses registers / sysmem (the mechanism that matters)

- No sv39: **no `satp` anywhere**. libos programs custom translation **windows** via CSRs
  0x7c8=idx / 0x7c9=enable|flags / 0x7ca=base / 0x7cb=size / 0x7cc=attr (setup at va
  0x4005000; helper `set_window` at 0x40057f4; catch-all window 8 covers [0, 4 GiB)).
- All MMIO goes through a **register-window base pointer** in the global at
  **va 0x438af28** (read as `lui t0,0x438b; ld t0,-0xd8(t0)`). Proof: the doorbell/notify
  function at va 0x5b1e040 does `t5 = ld [0x438af28]` then `lw/sw` at t5+0x110340/0x110344/
  0x110348/0x110350/0x110354 (poll loops on status bits 21/22/23, command write with
  `|0x10000`, interrupt ack bits 0x80/0x100). The assert path also writes t5+0x111300.
  So the window provably covers bus [0x110000, ≥0x111400), and base ↔ bus 0 (the doorbell
  registers land correctly only if the window maps base+0x110340 to the GSP pri block).
- FB/sysmem bulk data movement (RPC payloads) is done by the DMA engines, not by `sd`
  from the RISC-V; but MMIO mailbox writes from the RISC-V are exactly how the doorbell
  works, so a plain `sw` to a host-visible register is a proven channel.

## 3. v45 — mailbox exfil stub (chosen route)

### 3.1 Target registers

**NV_PGSP mailbox regs at bus 0x110440 / 0x110444** (falcon-v4-style layout, same offset
class as the SEC2 mailbox the host-side Booter code reads). Whole-disasm scan: **zero
firmware accesses to 0x110440/0x110444** — unused post-boot, safe to repurpose. They sit
inside the proven-writable window range (writes at 0x110344/0x110348/0x110350/0x111300 are
all done by the firmware through the same global). Host reads them via BAR0
(`/tmp/mmio_read`), no SEC2, no WPR, no seal involvement.
⚠️ The 0x110440/0x110444 offsets are by falcon-v4 analogy, not from a header — the v45 run
doubles as the verification: if BAR0 reads show live-changing values, the offsets are right.
Fallback if they don't: bus **0x111300** (the assert-scratch register the firmware itself
writes — proven writable; only overwritten with 0 when an assert fires).

### 3.2 Stub (hook = dmaUpdateVASpace entry; lives in the proven dead function)

Per call: `mbox0 ← a5.lo`, `mbox1 ← (a5>>32)&0xffff | (ra&0xffff)<<16`
(bit 35 of vAddr = bit 3 of mbox1 low half; ra low16 distinguishes callers:
dmaMapMemory = 0xc534, chunkloop = 0x4668, etc.). Also keeps incrementing the on-grid
ring counter at 0x438a500 (pre-seal cross-check).

- HOOK1 @ file **0x1027b54**: orig `13 01 01 d4 23 38 81 2a` → **`17 f3 ff ff 67 00 03 0e`** (unchanged from v41)
- STUB @ file **0x1026c34** (76 B, decode-verified, `re2/gen_exfil45.py`):
  `b7b2380483b282f237031100b382620023a0f24493d307029393030193d303011b9e0001b3e3c30123a27244b7a2380403b302501303130023b06250130101d42338812a17130000670043ee`

Instruction sequence (for the mini-assembler agent):
```
lui   t0, 0x438b          ; 
ld    t0, -0xd8(t0)       ; t0 = [0x438af28] = register window base
lui   t1, 0x110
add   t0, t0, t1          ; + 0x110000
sw    a5, 0x440(t0)       ; mailbox0 = vAddr.lo
srli  t2, a5, 0x20
slli  t2, t2, 16
srli  t2, t2, 16          ; vAddr.hi16
slliw t3, ra, 16          ; ra.lo16 << 16
or    t2, t2, t3
sw    t2, 0x444(t0)       ; mailbox1
lui   t0, 0x438a          ; (0x438a000)
ld    t1, 0x500(t0)       ; on-grid counter++
addi  t1, t1, 1
sd    t1, 0x500(t0)
addi  sp, sp, -0x2c0      ; overwritten prologue
sd    s0, 0x2b0(sp)
auipc t1, 1               ; return to hook+8 (va 0x5027b5c)
jalr  zero, t1, -0x11c
```
Clobbers only t0–t3 (caller-saved, dead at callee entry). Runs on every dmaUpdateVASpace
call — the host can watch the mailboxes change live during the repro; twin detection =
two records, same ra lane, a5.hi differing by 0x8 (2^35).

### 3.3 Same treatment for hook 2 (dmaAllocMap, file 0x102ccdc)

Optional second mailbox pair is not available (only 2 regs); leave stub2 as the v41 on-grid
ring (file 0x1026c94 bytes from PATCH_C_VERIFY.md §7.4) or drop it — the C1 stream is the
one that names the rogue caller.

### 3.4 Optional one-shot lock-revert probe (route 1 residual)

Append (before the prologue restore) — writes arm-time values back through the same window:
```
lui  t1, 0x840            ; SEC2 base offset
add  t1, t0_base...       ; (needs the regbase again — reload from [0x438af28])
sw   0x3000 → +0x240,  sw  0xf → +0x250
```
Window coverage of bus 0x840240 is unproven (SEC2 code uses a struct-field base, likely the
same window value, but not proven identical), and the lock may be sticky. If the write is
dropped/faults, cost = one cycle. Only after v45 has proven the mailbox channel.

## 4. The v46 CTXDMA=6 residue — who drives SEC2 post-boot?

GSP-RM itself. The firmware contains a SEC2 DMA control cluster: va 0x580f4b8 (writes
0x840738/0x840748), 0x581189c (sw 0x840714, lw 0x840708), 0x5811b20 (sw 0x840750),
0x580dac4/0x581e818 (reset toggle + status poll at 0x8403c0). So the GSP launches and uses
SEC2 (scrub/secure-copy class workloads) after boot with its own secure context — the
CTXDMA=6 residue is that usage. Implication for §3.4: the host-window lock and GSP's own
SEC2 DMA coexist by design, so *if* a revert write works it should not disturb GSP's SEC2
usage (different context); but the same fact means NVIDIA intended the host window sealed
while GSP keeps using the engine — expect the lock to be sticky.
