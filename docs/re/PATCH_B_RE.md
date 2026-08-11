# Patch B RE — why Patch A was a no-op + next probe/fix candidates (tu10x / CMP 170HX)

> ⚠️ 2026-08-11 errata: the C1/C2 ring at va 0x43c9800 is on a page the GSP cannot write
> (probe=8 hung on the ring `sd`). Relocated rings + updated stub bytes are in
> `PATCH_C_VERIFY.md` §5 (ring1 va 0x438a500, ring2 va 0x438b7c0).

> 2026-08-10. Target: `gsp_analysis/gsp_rm_tu10x.elf` (RISC-V64, vaddr = file_off + 0x4000000;
> resident WPR FB offset == ELF file offset; container offset in `gsp_tu10x.bin` = ELF offset + 0x40).
> Tooling: full linear disasm of both text segments regenerated at `gsp_analysis/re2/disasm.txt`
> (capstone 5.0.7, venv). Helpers: `re2/xref.py` (callers/func_of/dump with auipc+jalr resolution),
> `re2/strxref.py` (lui+addi absolute string xrefs), `re2/gen_probe.py` (patch byte generator).

## 1. What Patch A actually NOP'd — mechanism-level autopsy

Patch A (ELF off `0x1b54664`, `jalr ra, ra, 0x4f4` → `addi a0, zero, 0`) sits in the loop body
of the function at **va 0x5b5441c** ("chunkloop", file 0x1b5441c). Full read of the function
(`re2/disasm.txt`, entry `addi sp, sp, -0x170`):

- Args: a0 = object with per-subdevice state at a0+0x2000, a1 = record-table base,
  a2 = params (`0(a2)`=base, `8(a2)`=flags, `0x10(a2)`=pageSize, `0x30(a2)`=length, `0x68(a2)`==2 gate),
  a3 = mode (0 = build, 1 = skip/teardown path).
- Loop body (0x5b545d4..0x5b54670): for each `pageSize`-sized chunk (gated `pageSize >= 2MB`
  at 0x5b54580, `lui a5, 0x200`):
  - calls a getter (0x5abfe98) that returns the chunk's **physical address** into a stack
    single-entry page array (`-0xd8(s0)`, count=1);
  - computes **vAddr = ld(0(s3)) + PA**, where s3 = a1 + (subdev+5)*0x100 and `ld(0(s3))` is a
    **per-subdevice VA delta** (so VA = PA + delta, an offset/identity-style mapping);
  - calls `dmaUpdateVASpace` (0x5027b54) with flags **0x8007ff**, a6 = vAddrLimit, and ~11 stack params;
  - `s2 += s4`, loop while `s2 < length`.

So the NOP'd call is one iteration of "**map this memdesc chunk at VA = PA + per-subdev delta
into an internal VAS**". Callers: the record-list walker pair
**0x4d0b134 (build, a3=0) / 0x4d0b330 (teardown, a3=1)** (walk a linked list of 0x100-byte
records: +0x64 flag, +0x74, +0x84==0x100 check, +0xc8 next, +0x134 refcount, +0x138/+0x13c ids,
per-subdev VA range at +0x500/+0x508), called from VAS-management code at **0x4c43348** ← 0x5a0d83c
(memsys CU). This is an **auxiliary per-subdevice mapping layer** (kernel/CE-style VA=PA+delta
mappings of memory objects), *not* the client VA page-table build path.

### Why removing it is a no-op for the >32G aliasing

The client mapping path (the one that builds the user page tables where the −32G rogue write
lands) is a **disjoint call chain** (see §2): RPC → 0x502d0dc → `dmaAllocMap` 0x502ccdc →
`dmaMapMemory` 0x502c1d4 → `dmaUpdateVASpace` 0x5027b54 → gvaspaceMap/mmuWalk. The chunkloop
never runs inside it; NOPing the chunkloop's `dmaUpdateVASpace` call cannot change any client
PTE. The observed "zero effect on aliasing AND zero functional damage" is exactly what this
predicts: Patch A only skipped building those auxiliary VA=PA+delta mappings, which our
workloads evidently never exercise (or which get rebuilt on demand elsewhere).

Also note: the ga10x-era hypothesis ("chunkloop = the rogue second build pass") predicted the
loop writes client PTEs; the tu10x disassembly shows it writes a *different* VAS with
VA=PA+delta semantics — that hypothesis was wrong, and the offset/bytes of Patch A were
"correctly placed on the wrong function".

## 2. tu10x landmark table (this session; va — file off = va − 0x4000000)

| Function | va | file off | Evidence |
|---|---|---|---|
| `dmaUpdateVASpace_GF100` | 0x5027b54 | 0x1027b54 | `addi sp,-0x2c0` prologue (matches TU10X_RELOC); contains the "non-contig 4KB pages" continuity check (2× call to page-array getter at 0x5027d94/0x5027da8 + assert-cookie logs) |
| `dmaPageArrayGetPhysAddr` | 0x501e7d4 | 0x101e7d4 | field layout exactly dma.c:1199: pData@0, count@0xc, startIndex=a1, bDuplicate@0x11, bOsFormat@0x10, PteAdjust@0x20, bLocalized@0x12, localizedMask@0x18 |
| `dmaMapMemory` | 0x502c1d4 | 0x102c1d4 | big flags-word decode (ld 0x50(a4)); **single** dmaUpdateVASpace call at 0x502c530; regkey-init sibling 0x502c028/0x502bf30 read `RmDisableMmuInvalidate`/`RMMmuMemoryMap`/`RMRestrictVARange` (lui+addi abs xrefs) |
| `dmaAllocMap` | 0x502ccdc | 0x102ccdc | builds 0x30-byte map record, calls dmaMapMemory at 0x502ce94; matches ga10x 0x12edaa4 shape |
| map wrapper (NVOC-indirect, RPC-facing) | 0x502d0dc | 0x102d0dc | no direct callers; either calls helper 0x4c9c38c or tail-calls dmaAllocMap (0x502d250) |
| chunkloop (aux VA=PA+delta mapper) | 0x5b5441c | 0x1b5441c | Patch A site inside; see §1 |
| chunkloop walkers | 0x4d0b134 / 0x4d0b330 | 0xd0b134 / 0xd0b330 | build/teardown |
| assert-cookie logger | 0x5be7b50 | 0x1be7b50 | a0=severity, a1=cookie (strings are **stripped** in this build — dangling "string" pointers are assert cookies, not text) |

`dmaUpdateVASpace` direct callers: 0x5029018 (8 callers), 0x502b2a0 (10), 0x502b88c (4),
0x502c1d4 (dmaMapMemory), 0x4d0f088 (alignment-aware map variant in the VAS-mgmt CU),
0x5b5441c (chunkloop).

## 3. Rogue-hunt status (honest)

- Whole-binary scans: no `2^35`/`2^35−1` constants, no 35-bit sign-extension idioms
  (`slli/srli 0x23`, `slli 0x1d+srli/srai 0x1d` pairs all checked — PTE packing and array
  indexing only). The −32G is a **runtime-derived** value, not an immediate.
- The shared map path (dmaUpdateVASpace and below) matches open-source RM structure and is
  single-pass; chunkloop excluded by Patch A experiment. The rogue therefore sits in the
  GSP-specific layer around the map RPC (0x502d0dc and up) **or** inside the walk/transfer
  below dmaUpdateVASpace. Static analysis alone does not pin it further — the next step must
  be a runtime observation. That's what the probes below buy in one boot each.

## 4. Candidate patches

### C1 (run first) — dmaUpdateVASpace argument logger

Records (ra, a0..a7, first stack arg) of **every** dmaUpdateVASpace call into an 8-entry ring
in the resident image; read back via the WPR channel. This directly shows whether a rogue
second map pass exists and who calls it.

- **Hook** at ELF off `0x1027b54` (va 0x5027b54), 8 bytes:
  - orig: `13 01 01 d4 23 38 81 2a` (`addi sp,sp,-0x2c0; sd s0,0x2b0(sp)`)
  - new:  `17 03 c0 00 67 00 43 11` (`auipc t1,0xbc0; jalr zero,t1,0x114` → va 0x5be7c68)
- **Stub** at ELF off `0x1be7c68` (va 0x5be7c68 — 920-byte zero cave at end of RM text,
  verified file bytes are all zero and the preceding logger function ends at 0x5be7c64), 88 bytes:
  `b7a23c0403b302809303130023b07280137373001313630033035300233413802338a380233cb3802330c3822334d3822338e382233cf38223300385233413858333010023387384130101d42338812a170344ff670043ea`
  (clobbers only t0/t1/t2 — safe at function entry; re-executes the overwritten prologue and
  jumps back to va 0x5027b5c; counter at va **0x43c9800**, ring va 0x43c9808..0x43c9a08,
  ELF off 0x3c9800+)
- Byte stream generated and round-trip verified by `re2/gen_probe.py`.

**Readback protocol**: after cold boot + patch delivery, read ELF off 0x3c9800..0x3c9c28 via
WPR and confirm all-zero (the area is a zero-initialized globals region; if not zero, pick
another window in the 0x3894b0..0x3c9d28 zero run and adjust BASE in gen_probe.py). Then run
the 48G repro (vec_scan2). Read counter + ring.

**Interpretation**:
- Legit client map calls show ra = 0x502c534 (return into dmaMapMemory). The rogue second
  pass, if it exists, appears as a call whose a5 (vAddr) is exactly another call's a5 − 32G
  (same a2/a3). Its ra names the guilty caller → targeted single-instruction fix follows.
- If every call is sane (no −32G twin) while aliasing still reproduces → the corruption is
  **below** dmaUpdateVASpace (walk level-instance / transfer target). Next probe then hooks
  the transfer layer.
- If counter stays 0 or GPU dies at boot → the data page isn't GSP-writable as assumed;
  relocate the ring (e.g., into the same page as a known-written global) and retry.

### C2 (same delivery, complements C1) — dmaAllocMap call logger

Discriminates "segment loop **above** dmaAllocMap" (two dmaAllocMap calls per map RPC, second
with shifted offset) vs "below" (single call). Same ring design.

- **Hook** at ELF off `0x102ccdc` (va 0x502ccdc), 8 bytes:
  - orig: `13 01 01 f2 23 38 81 0c` (`addi sp,sp,-0xe0; sd s0,0xd0(sp)`)
  - new:  `17 b3 bb 00 67 00 43 fe` (→ va 0x5be7cc0)
- **Stub** at ELF off `0x1be7cc0`, 88 bytes:
  `b7a23c0403b302a29303130023b072a2137373001313630033035300233413a22338a3a2233cb3a22330c3a42334d3a42338e3a4233cf3a4233003a7233413a783330100233873a6130101f22338810c175344ff670043fd`
  (counter va **0x43c9a20**, ring 0x43c9a28..0x43c9c28)

### C3 (destructive one-shot discriminator, last) — dmaPageArrayGetPhysAddr +2MB poison

Forces every page-array lookup to return PA+0x200000. If the rogue write is a real second map
pass (re-reads the page array), the rogue PTEs shift by one 2MB page too (victim content moves);
if the rogue copies already-encoded slots, it doesn't. Three 4-byte words in 0x501e7d4:

| ELF off | orig | new | effect |
|---|---|---|---|
| 0x101e830 | `63 06 06 00` (`beqz a2, 0xc`) | `13 00 00 00` (nop) | always take the merge path |
| 0x101e834 | `83 b7 87 01` (`ld a5, 0x18(a5)`) | `b7 07 20 00` (`lui a5, 0x200`) | a5 = 0x200000 |
| 0x101e838 | `33 65 f5 00` (`or a0, a0, a5`) | `33 05 f5 00` (`add a0, a0, a5`) | return PA+2MB |

System may not survive boot (poisons internal maps too) — one-shot experiment, revert after.
Prefer C1/C2 first; C3 only if C1 shows a rogue call but its ra is ambiguous.

## 5. What would confirm/refute

- Win criterion unchanged: cold-boot `vec_scan2 48` with `total_bad_units=0`.
- C1+C1′: the ring dumps are the discriminating evidence. A (−32G)-twinned call pair =
  second-pass confirmed and caller identified → next patch NOPs/fixes that call site
  (that's the real "Patch B"). No twin = bug is in the walk/transfer; we then hook
  `memmgrMemEndTransfer`-equivalent target addresses (to be located from the dmaUpdateVASpace
  MapNextEntries materializations at 0x5027fd0..0x5028254).
- Confidence ranking: C1 > C2 > C3 (C3 destructive). No blind "fix" byte-patch is justifiable
  yet; anyone claiming one is guessing.
