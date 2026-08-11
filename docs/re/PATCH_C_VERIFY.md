# Patch C/D verification + C1/C2 ring relocation (tu10x / CMP 170HX)

> 2026-08-11. Static verification against `gsp_analysis/re2/disasm.txt` (full linear disasm of
> `gsp_rm_tu10x.elf`; vaddr = file_off + 0x4000000; resident FB offset == ELF file offset).
> Verdict up front: **Patch C is a no-op (NO-GO), Patch D is fatal (NO-GO).**
> The C1/C2 logger plan stands, with rings relocated to GSP-writable pages (§5).

## Q1 — what is at file 0x102c32c, and what is s2 there

The site (va 0x502c32c) sits inside **dmaMapMemory** (func start va 0x502c1d4, PATCH_B_RE.md
landmark; sole caller: dmaAllocMap at 0x502ce94). Context:

```
0x502c2d8: lw   s2, 0x44(s1)        ; s1 = a4 = map-params struct; s2 = *(i32*)(params+0x44)
0x502c2dc: lui  a5, 1               ; 0x1000     (4K)
0x502c2e0: beq  s2, a5, ...
0x502c2ec: slli s2, s2, 0x20        ; <-- SAME zero-extend idiom, pair #1 (straddles a vtable
0x502c300: srli s2, s2, 0x20        ;     call whose return is compared: beq a0, s2 @0x502c314)
0x502c318: lw   s2, 0x44(s1)        ; reload after the call
0x502c31c: lui  a5, 0x200           ; 0x200000   (2M)
0x502c320: beq  s2, a5, ...
0x502c324: lui  a5, 0x20000         ; 0x20000000 (512M)
0x502c328: bne  s2, a5, error
0x502c32c: slli s2, s2, 0x20        ; <-- Patch C site, pair #2
0x502c330: srli s2, s2, 0x20
```

- **s2 = the pageSize attribute**, not a dmaOffset. Proof: it is loaded with `lw` (32-bit, from
  params+0x44 — a 64-bit dmaOffset could never be loaded this way), and it is validated against
  exactly {4K, 2M, 512M} (0x1000 / 0x200000 / 0x20000000). The slli/srli-32 pair is the
  compiler's canonical u32→u64 re-widening after a *signed* `lw`.
- The actual 64-bit offset fields of the params struct are at **+0x20 and +0x28** — both loaded
  with full `ld` (0x502c47c `ld a5, 0x28(s1)`, 0x502c4b0 `ld s1, 0x20(s1)`) and never truncated.
- Forward uses of s2: page-size validation branches; `mul a6, a6, s2` (pageCount × pageSize →
  byte length) at 0x502c468; alignment mask `neg a3, s2; and a3, a3, a5` at 0x502c480; and the
  pageSize stack argument to dmaUpdateVASpace (`sd s2, 0x50(sp)` at 0x502c4f4).
  None of these is an address.

## Q2 — reconciling "72G works today" with the claim

The claim's premise fails structurally: s2 never carries an address, so there is nothing to
reconcile. The high 32 bits of s2 are zero *by construction* (32-bit `lw` of a page-size enum
whose max legal value is 512M). >4G mappings work because dmaOffset/VA travel through the
64-bit params fields (+0x20/+0x28) untouched. Patch C replaces `zero-extend-already-32-bit(s2)`
with `mv s2, s2` — semantically identical for every value s2 can hold. **It is a no-op in the
same family as Patch A.** (Their replacement bytes `13 09 09 00` do decode to `mv s2, s2` —
encoding is correct, effect is nil.)

## Q3 — does the math even match the observed signature?

No, independently of Q1/Q2:

- `slli/srli 32` computes **addr mod 4G** (clears bits 63..32).
- The empirical signature is **addr − 32G** (clears bit 35 only, bits 34..32 preserved):
  the victim band [3.4G, 16G) maps to source band [35.4G, 48G) with a constant −2^35 delta.
  mod-4G would give *four different* deltas across that source band (−32G/−36G/−40G/−44G,
  changing at every 4G boundary); the measured delta is constant −32G. This was already the
  decisive argument in RE_FINDINGS.md §3 against any 32-bit truncation theory.

So even if s2 *were* an address, this site could not produce the observed pattern.

## Q4 — verdicts

- **Patch C (0x102c32c): NO-GO.** No-op. Do not burn a cycle.
  (If anyone insists on empirical proof: predicted result = bit-identical behavior,
  `vec_scan2 48` unchanged.)
- **Patch D (NOP the dmaUpdateVASpace call at file 0x102c530): NO-GO.** That is dmaMapMemory's
  *only* VAS-update call; NOPing it makes every client map a success-with-no-page-tables.
  Expected result: instant MMU faults on first touch / dead GPU. It is not a "narrower Patch A";
  it amputates the main map path.
- **Would C1/C2 have caught the claimed mechanism?** Yes — the claimed truncation sits between
  the two hooks. C2 (dmaAllocMap entry) records the incoming params pointer (offset/dmaOffset
  at +0x20/+0x28 readable in the ring via a1/a2/a3), C1 (dmaUpdateVASpace entry) records the
  post-processing a5/a6 (vAddr/vAddrLimit). Any 32-bit truncation in between would show as
  C1 vAddr high bits zeroed while C2 params are full-width. The logger plan already covers this
  hypothesis class — one more reason to spend the cycle on C1/C2 instead of Patch C.

## Q5 — C1/C2 ring relocation (old ring page was not GSP-writable)

Root cause of the probe=8 hang: ring was at va 0x43c9800 (page 0x43c9000). Static-store census
of the whole image (lui[+addi]+memop scan) shows that page receives **zero** runtime stores —
its only would-be writer is the libos logger struct at 0x43c9d08, which is never written when
logging is disabled. Page 0x43c9000 is therefore RO (or write-faulting) to the GSP MMU.

New rule: **put rings only in pages with proven static stores.** Census results (stores execute
at boot/runtime without faulting, so the page is mapped RW):

| page va | file | static stores | clean zero windows (zero in file, no static access) |
|---|---|---|---|
| 0x4389000 | 0x389000 | 74 | 0x4389891 (719B), 0x4389cc1 (327B), ... |
| 0x438a000 | 0x38a000 | 14 | 0x438a4d1 (1199B), 0x438aac1 (1127B), 0x438a198 (720B) |
| 0x438b000 | 0x38b000 | 10 | 0x438b7a9 (1023B), 0x438bba9 (1023B), 0x438b159 (735B) |
| 0x43be000 | 0x3be000 | 6  | 0x43be1c1 (2183B), 0x43bea49 (1367B) |

**New ring locations:**
- C1 (dmaUpdateVASpace logger): counter va **0x438a500**, ring va 0x438a508..0x438a708
  (file 0x38a500; inside the 1199B clean window in page 0x438a000).
- C2 (dmaAllocMap logger): counter va **0x438b7c0**, ring va 0x438b7c8..0x438b9c8
  (file 0x38b7c0; inside the 1023B clean window in page 0x438b000).

**Updated bytes** (hooks unchanged; stubs regenerated by `re2/gen_probe.py`, byte-level
re-verified: bases and return targets decode exactly)
— **⚠️ SUPERSEDED by §6**: these stubs sit in the end-of-text cave va 0x5be7c68, whose page is
not present in the runtime mapping (probe=8 round 2). Use the §6 bytes instead.

- HOOK1 @ file 0x1027b54: `13 01 01 d4 23 38 81 2a` → `17 03 c0 00 67 00 43 11`
- STUB1 @ file 0x1be7c68 (88B):
  `b7a2380403b302509303130023b07250137373001313630033035300233413502338a350233cb3502330c3522334d3522338e352233cf35223300355233413558333010023387354130101d42338812a170344ff670043ea`
- HOOK2 @ file 0x102ccdc: `13 01 01 f2 23 38 81 0c` → `17 b3 bb 00 67 00 43 fe`
- STUB2 @ file 0x1be7cc0 (88B):
  `b7b2380403b3027c9303130023b0727c1373730013136300330353002334137c2338a37c233cb37c2330c37e2334d37e2338e37e233cf37e23300381233413818333010023387380130101f22338810c175344ff670043fd`

**Early-abort check (saves cycles):** dmaUpdateVASpace and dmaAllocMap both fire during normal
driver load. Right after modprobe + patch delivery, read file offsets 0x38a500 / 0x38b7c0 via
the WPR channel: if both counters are already >0, the pages are writable and the hooks work —
proceed to the 48G repro. If zero, abort before touching the GPU workload.

**Backup exfil channel (if even RW-proven pages fault):** the libos logger's buffer base pointer
is at va 0x43c9d18 (file 0x3c9d18, zero-init, filled at runtime when logging is enabled). Read
it via WPR after boot; if nonzero it points at the circular log buffer (FB or sysmem), which is
writable *by construction* (the logger stores records through it at 0x5be7c08). A stub variant
can `ld` that pointer and write the ring at buffer+capacity−1KB; host reads the buffer address
back. Requires logging to be enabled (regkey) — second choice, not first.

## 6. probe=8 round 2 postmortem + final stub home (2026-08-11)

### What the ping-pong `0x400a35c ↔ 0x5c27c68` means

- va 0x400a35c is the **`mret` at the end of the libos trap epilogue** (PH0, file 0xa35c;
  preceding code restores registers from the tp-relative trap frame and returns to mepc
  *without advancing past the faulting instruction* → any unrecovered fetch fault loops
  forever: exactly the observed ping-pong/hang).
- The faulting PC 0x5c27c68 = stub1 ELF va (0x5be7c68) **+ 0x40000 exactly**. The hook jump is
  `auipc`+`jalr` (PC-relative): for it to land at 0x5c27c68, the hook itself must have executed
  at ELF va + 0x40000. So **the runtime maps the main text (PH25) at ELF va + 0x40000**
  (data segments stay at ELF va — absolute `lui` data references in the running firmware prove
  that). Patch A never noticed because in-place byte edits are bias-agnostic.
- The fetch at 0x5c27c68 faulted (→ ping-pong), so the cave's page (last page of PH25, file
  0x1be7000..0x1be8000) **is not present/executable in the runtime mapping at all** — the
  loader does not map the final text page(s). Tail cave unusable; no jump encoding can fix that.
- Important corollary: PC-relative hook→stub jumps need **no bias correction** — the +0x40000
  cancels in the delta as long as hook and stub are in the same segment. The round-2 failure
  was purely "target page not mapped", not a mis-encoded jump.
- Trap-state exfiltration already exists: the Xid 1 "GSP task exception" dmesg line (mepc/
  mcause/mbadaddr, task id) is how the ping-pong PCs were observed — no extra channel needed
  for crash state. (The logger buffer pointer at 0x43c9d18 remains the backup data channel.)

### Why PH26 is not an option

PH26 (file 0x1d000..0x200000, va 0x401d000..0x4200000): 949,924 linearly-decoded "instructions"
with **zero `ret`** — it is rodata (strings/tables/jump tables; the regkey strings live there),
almost certainly not mapped X. Its zero runs are data gaps, not code caves. All stub homes must
be in PH25 (the 16 MB text, va 0x4bf7000..0x5be8000), which is densely packed — its only zero
cave is the unmapped tail page.

### Final stub home: a provably-dead function in the dma CU

**va 0x5026c34, file 0x1026c34, 412 B** — one page below dmaUpdateVASpace (same mapping
regime as the proven-executed hook). Evidence of deadness (all checked over the full disasm):

1. zero direct callers (`jal`/`jalr`), zero tail-call targets, zero address materializations
   (auipc+addi / lui+addi), zero qword references anywhere in the file (no vtable, no header
   export table entry), and **zero external branches into its span** (all 15 inbound branch
   targets are internal);
2. proper prologue/epilogue, 1 ret; the preceding instruction is a call to the noreturn
   stack-chk-fail helper 0x5b1fbd4 → no fall-through into it;
3. content is a null-checking teardown helper in the dma CU with error-cookie paths —
   a devel/debug-only export that production never calls.

Stub layout: C1 stub at 0x5026c34 (88 B), C2 stub at 0x5026c8c (88 B), both inside the 412 B
dead span (ends 0x5026dd0). Ring addresses unchanged (va 0x438a500 / 0x438b7c0, the RW-proven
data pages from §5).

### Final bytes (regenerated by `re2/gen_probe.py`, round-trip verified)

- HOOK1 (dmaUpdateVASpace) @ file **0x1027b54**:
  orig `13 01 01 d4 23 38 81 2a` → **`17 f3 ff ff 67 00 03 0e`** (`auipc t1,-1; jalr zero,t1,0xe0`)
- STUB1 @ file **0x1026c34** (88 B):
  `b7a2380403b302509303130023b07250137373001313630033035300233413502338a350233cb3502330c3522334d3522338e352233cf35223300355233413558333010023387354130101d42338812a17130000670083ed`
- HOOK2 (dmaAllocMap) @ file **0x102ccdc**:
  orig `13 01 01 f2 23 38 81 0c` → **`17 a3 ff ff 67 00 03 fb`** (`auipc t1,-6; jalr zero,t1,-0x50`)
- STUB2 @ file **0x1026c8c** (88 B):
  `b7b2380403b3027c9303130023b0727c1373730013136300330353002334137c2338a37c233cb37c2330c37e2334d37e2338e37e233cf37e23300381233413818333010023387380130101f22338810c1763000067008300`

Verification: byte-level decode of both hooks and both stub return jumps resolves exactly to
STUB1/STUB2 and HOOK+8 respectively (independent decoder check, not just the generator).
All code references are PC-relative (bias-safe); only ring accesses use absolute data vas.
Early-abort check from §5 still applies: after modprobe, counters at file 0x38a500/0x38b7c0
must be >0 before running the repro.

## 7. The VA→PA problem + v41 exfil triangulation (2026-08-11)

### 7.1 The GSP has no sv39 MMU — it uses libos-programmed translation WINDOWS

No `satp` write exists anywhere in the libos segment (PH0, freshly disassembled to
`re2/disasm_libos.txt`). Instead, libos programs a small set of **address-translation windows**
via custom CSRs: `0x7c8` = window index, `0x7c9` = enable|flags, `0x7ca` = base, `0x7cb` = size,
`0x7cc` = attributes. Setup site: va 0x4005000 (libos init), plus a helper at 0x40057f4
(`set_window(idx=a0, flags=a1, base=a2, size=a3, attr=a4)`) called by the app loader
(0x4005834..) with values computed from **runtime globals** (ld 0x400d420, ld 0x400d638, ...).
Statically visible windows: region 3 = libos itself [0x4000000, 0x13000); region 7 = a
`0xdead`-tagged disabled slot; region 8 = **catch-all [0, 4 GiB)**. The RM app's windows are
installed at app-load from the phdrs + WPR-meta — i.e., the exact translation table is runtime
state, so "physical = gspFwOffset + (va − 0x4000000)" is NOT a universal rule. That is the root
cause of the v40 exfil failure.

### 7.2 The empirically-consistent mapping model

| region | va at runtime | physical | evidence |
|---|---|---|---|
| libos (PH0) | link va (bias 0) | grid | trap-epilogue PC 0x400a35c = file 0xa35c + 0x4000000 exactly |
| RM text (PH25) | **link va + 0x40000** | grid (host text patches execute) | v37/v38 fault PC 0x5c27c68 = cave va + 0x40000; text window size = filesz truncated to 64 KiB (0xff0000 < 0xff1000) → mapped range ends exactly at runtime 0x5c27000, which is why the tail cave faults |
| PH26 rodata (0x401d000..) | link va | grid | fwversion probe=7: all code-referenced "610.43.02" copies are in PH26 (xrefs at 0x4c1437c/0x4c554b0/0x51f9e70; the PH27 copies have **no** code xrefs) |
| PH27 data (0x41f7000.., incl. globals 0x4389xxx and ring 0x438a500) | link va | **NOT on grid** (target base is a runtime value) | v40: magic never on grid at 0x38a500 / 0x34a500 / 0x3ca500 |

### 7.3 v40 caveat — check the stub encoding before anything else

The v40 stub as described (`lui t0,0x438a5; sd magic,0(t0)`) stores to **0x438a5000**, not
0x438a500 — `lui` drops the low 12 bits; the +0x500 must come from the `sd` immediate
(`lui t0,0x438a; sd magic,0x500(t0)`). If that is literally what ran, the store hit the
catch-all window and the magic may already sit somewhere in FB or sysmem. **Zero-cost action:
scan FB (phantom-peek channel) and the GSP sysmem allocations for the v40 magic value before
spending another boot.** My generated bytes never had this bug (gen_probe output decodes to
0x438a500), but verify what was actually delivered.

### 7.4 v41 triangulation stub (one boot, three channels)

Hook stays at dmaUpdateVASpace entry (file 0x1027b54). The stub writes three distinct magics:

- **magicA** `0xffffffffc0ffee41` → va **0x438a508** (PH27 ring; control — same target as v40)
- **magicB** `0xffffffffc0ffee42` → through the **libos logger buffer pointer** (route 2, below)
- **magicC** `0xffffffffc0ffee43` → va **0x4033360** (PH26 zero gap, same segment as the
  on-grid-proven version strings; store-permission probe — kept LAST so a RO fault can't
  suppress A/B; a hang means "PH26 store-faults", with A/B already written during boot)

Generated by `re2/gen_magic41.py`, decode-verified. Bytes:

- HOOK1 @ file 0x1027b54: `13 01 01 d4 23 38 81 2a` → `17 f3 ff ff 67 00 03 0e` (unchanged)
- STUB1 @ file 0x1026c34 (92 B):
  `b7a2380403b302501303130023b06250b7f3ffc09b8313e423b4725023b8125023bcf25023b0025323b41253b7a23c0403b382d1630403009b83130023307304b73203049b83130023b07236130101d42338812a17130000670043ed`
- HOOK2 @ file 0x102ccdc: `13 01 01 f2 23 38 81 0c` → `17 a3 ff ff 67 00 83 fb`
- STUB2 (dmaAllocMap logger, arg record + magic `..ee49`) @ file 0x1026c94 (60 B):
  `b7b2380403b3027c1303130023b0627cb7f3ffc09b8393e423b4727c23b8127c23bcb27c23b0c27e23b4d27e130101f22338810c176300006700c301`

Result matrix:
- magicA on grid @ file 0x38a508 → PH27 *is* on-grid; v40 was the encoding bug; readback solved.
- magicA absent, magicB found (FB/sysmem scan) → PH27 off-grid; use the logger channel (or the
  empirically discovered physical) for all future readback.
- magicC on grid @ file 0x33360 → PH26 is store-writable; future probes can exfil via PH26
  directly on-grid (simplest possible channel).
- hang at boot → magicC store faulted (PH26 RO); A/B still written; treat PH26 as read-only.
- nothing anywhere → hooks not firing in this build; re-check hook delivery (v39 anomaly).

### 7.5 Route 2 (logger channel) — verified struct layout

The assert/cookie logger (va 0x5be7b50) keeps its state at **va 0x43c9d08** (file 0x3c9d08,
zero-init in the file, populated at runtime only when logging is active):
`+0x00` = write index, `+0x08` = capacity, `+0x10` = **buffer base pointer**. Records are
8 bytes at `base + index*8`, index wraps at capacity starting from 1; an `ecall` (t0=0x12)
notifies after each batch. The v41 stub writes magicB at `base+0x40` (slot 8) guarded by
`beqz` — at worst one log record is overwritten; if logging is disabled the store is skipped
and nothing faults. Host-side discovery: scan for the unique magic in (a) FB via the
phantom-peek channel, (b) the sysmem pages the host driver allocated for GSP (RPC queue /
boot-args / log buffer — the host knows those physical addresses because it allocated them).
If magicB never appears and the pointer was null, logging is off; the only log-adjacent regkeys
present in this build are `RmGspTraceCrashLoggingTracepointMaskLo/Hi` (file 0xb91f0/0xb9218).

### 7.6 Route 3 (MMIO scratch) — rejected

One paragraph: the RM-on-GSP never does an absolute-address MMIO store that a stub could reuse
— all its register/FB accesses go through runtime descriptor pointers, and every low absolute
address falls into the catch-all window whose target base is itself runtime state. A scratch
write to a host-readable register (e.g., a GSP mailbox) would need that base known; without it
the write lands at an unpredictable physical address. Not competitive with 7.4/7.5.
