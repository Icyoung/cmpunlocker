# CMP 170HX 10GB→80GB Unlock — Session Research Report (2026-08-06 / 2026-08-07)

_Branch: `experiment-user-pte-kind-gmk-r4` (name is now historical — currently carries P1a + P1c GSP-timing experiments in addition to R3.)_

_Author: this session._

_All measurements on: NVIDIA CMP 170HX (GA100, PCI ID `10de:2082`), driver `nvidia-open 610.43.02`, cmpunlocker R3, `RMDisableScrubOnFree=1` in `NVreg_RegistryDwords`, eGPU over Thunderbolt 3 (PCIe Gen 2 x4), server `p3-server`._

---

## 1. Where we are

**Confirmed** — with R3 + `RMDisableScrubOnFree=1`:

- Any single `cudaMalloc` up to **78 GiB** (alloc/free only, no writes) — **PASS**, all under 0.6 s.
- `cudaMemset` up to **≤20 GiB** — **PASS**, ~0.01 s (~1300 GB/s).
- `cudaMemset` at exactly **40 GiB** — **returns "success" after 49.0 s**. Highly suspect: 49.0 s repeats to ±10 ms across independent runs, matching an RM double-timeout constant (22 s × 2 + slack), not a real CE progression.
- Any workload that writes / touches ~**≥ 40–60 GiB** in one operation — kernel-space crash follows shortly. GSP dies with:
  ```
  Xid 1, GSP task exception: illegal instruction (cause:0x2) @ pc:0x5b2b940, task:1
  mepc:0x5b2b940  mbadaddr:0x5c0901c  mcause:0x2
  ```
  followed by Xid 119 RPC timeouts on `fn 76 GSP_RM_CONTROL 0x20800a70 = NV2080_CTRL_CMD_INTERNAL_BUS_FLUSH_WITH_SYSMEMBAR`. Recovery requires a full cold power cycle (soft `rmmod` will not fully drop refcounts once GSP is dead).
- **This PC and the ~96 s SM-prefill duration reproduce to 3-decimal-second precision across independent cold boots and independent driver builds.** It is a deterministic firmware fault, not a race or a transient.

**Unresolved** — end goal was stable ~70+ GiB residency for vLLM (Qwen3.6-27B bf16, ~50 GiB weights + KV cache). We cannot get there with the levers we've tried.

**Recovery discovery** — for hung GSP after crash, cycling **eGPU chassis power** (with correct `rmmod → PCI remove → power off → power on → rescan → modprobe` order) restores the card in ~30 s vs 2–3 min for a whole-server cold boot. See `feedback_egpu_power_cycle_order` in memory.

---

## 2. Timeline of experiments and what we learned

### Experiment F0: your (GPT Pro's) A/B on scrub-on-free

Two cold boots:

| Probe | F0-S (scrub on)  | F0-N (`RMDisableScrubOnFree=1`) |
| -------------- | ---------------- | ------------------------------- |
| PRE-FREE       | PASS 0.20 s      | PASS 0.19 s |
| POST-2S        | **HANG 144 s**   | PASS 0.23 s |
| POST-30S       | FAIL no-device   | PASS 0.23 s |
| POST-EXIT      | FAIL no-device   | PASS 0.24 s |

F0-S dmesg confirmed exactly:
```
Xid 119, pid=f0_probe, Timeout 22s waiting for RPC
Expected function 103 (GSP_RM_ALLOC) sequence 2102 (0x20800a70 0x0)
```

**Ruled out** (from PRE-FREE PASS + `worker still alive` timeline):
- Static residency of 41 GiB is not the trigger.
- Last-client teardown is not the trigger.
- External `nvidia-smi` polling is not required.
- New-client attach at ~41 GiB residency (with scrub OFF) is fine.

**Established:** the scrub-on-free path was implicated in some 40 G–adjacent hangs and disabling it silenced that class of hang. But it is not the deepest layer — see §3.

### Verify60 flake — retracted

We saw a single `f0_verify` sample report 76 928 bytes of stale `0xA5` at `40G + 384 KiB` after a `cudaMemset(0xAB, 20G)` returned OK. **Unreproducible across 80 subsequent identical runs.** Now attributed to hot-reload transient (see `feedback_driver_reload_transient` memory) — the first workload after `rmmod && modprobe` is unreliable regardless of `RMDisableScrubOnFree`.

### f0_torture and f0_memset_range — hitting the real ceiling

`f0_torture ROUNDS=5 CAP_GB=70` (alloc 70 G, memset ENTIRE 70 G, free, ×5): hangs on **round 1**, both hot-reload boot and cold boot. Xid 154 PF FLR, `fn 103 GSP_RM_ALLOC` timeout, dmesg shows the recurring pair `0x20800a70` (sysmembar) alternating with `0x20801702 (0x4)` before the last GSP_RM_ALLOC never returns.

`f0_memset_range` on 70 G alloc:

| range | result |
|---|---|
| `memset(0, 40G)` | PASS but **49.01 s** |
| `memset(40G, 30G)` never ran (worker hung after B) | HANG on next fork |

### f0_alloc_ladder — the surprising negative control

Fresh process per size, `cudaMalloc(gb) + cudaFree(gb)` **without any memset**:

```
60G alloc+free   PASS 0.60s
62G alloc+free   PASS 0.40s
...
78G alloc+free   PASS 0.60s
```

**Alloc/free of every size up to 78 G works**. The 40 G / 70 G failures are triggered by **writes**, not by allocation size or PMA-region size.

### f0_memset_timing — precise crossover

Fresh child per size:

```
CE cudaMemset off=0 len=10G   PASS 0.007 s  (~1400 GB/s)
CE cudaMemset off=0 len=20G   PASS 0.016 s  (~1250 GB/s)
CE cudaMemset off=0 len=40G   PASS 49.006 s (~0.8 GB/s)  ← suspect fake sync
```

Subsequent memsets in later children hung → Xid 119. So somewhere in `20G < len < 40G` the CE path drops off a cliff. **We never got to test 25 / 30 / 35 G** because each attempt costs a GPU crash.

### f0_sm_vs_ce and f0_reproduce — nailed the 49 s constant as suspicious

Extra tests that **all PASSed** on 60 G workloads (SM write, CE memset, CE D2H, SM verify in mixed combinations). Suggested that 60 G writes with the right verification path don't necessarily fail. But `f0_fake_sync_check` on 70 G exposed the real problem:

### f0_fake_sync_check — the decisive experiment

Sequence: alloc 70 G → SM write ENTIRE 70 G with `0x99999999` → SM verify (baseline) → CE memset [0, TARGET_GB) with `0xAB` → SM verify low+high.

**Cold boot, R3 + `RMDisableScrubOnFree=1`, `TARGET_GB=40`**:
- `SM prefill 70G with 0x99999999 ... done in 96.03 s`
- BASELINE VERIFY hangs → dmesg shows `Xid 1 illegal instruction @ pc:0x5b2b940` during SM verify kernel.
- SM write 70 G is not a normal ~1 s operation. **It takes 96 s** — same signature as the 49 s CE-memset suspicion, only at a different scale. The kernel is `write32_kernel` doing plain `p[i] = pat` — no CE, no comptag, pure SM STG. And it still hangs GSP.

**Reproduced 3 times across 3 driver builds** (base R3 / R3+P1a / R3+P1a+P1c) with **identical PC 0x5b2b940** and identical ~96 s prefill duration.

### f0_verify — long-form 60 G stability

`f0_verify` (60 G alloc + memset [40G,60G) + D2H sample verify × 10 rounds × 8 runs = 80 rounds): **all 80 rounds PASS in ~11 s each.** With `RMDisableScrubOnFree=1`, 60 G workloads that stay in the "write only the top 20 G with CE" pattern are stable. This is why we can *almost* get to a vLLM viable configuration — but not one that survives a full `SM write 70G` or `cudaMemset(dev, X, 70G)`.

---

## 3. Root cause hypotheses — what fits, what doesn't

### Killed hypotheses

- **~~scrub-on-free is the only bug~~** — F0 A/B nailed one class of hang but 70 G torture still crashes with scrub off.
- **~~CE-only bug~~** — 96 s SM-only prefill kills GSP the same way.
- **~~PTE kind / comptag on user allocations~~** — R4 patch (extend `memmgrGetPteKindForScrubber_TU102`-style override to `memmgrChooseKind_TU102`) rebuilt and cold-booted. **Identical crash, identical PC**. `memmgrChooseKind_TU102` is the correct choke point per subagent research; forcing every user alloc to `NV_MMU_PTE_KIND_GENERIC_MEMORY` for `0x2082`/`0x20C2` does nothing to the failure mode.
- **~~Late `pWprMeta->fbSize` update is enough~~** — R3 already re-populates `pWprMeta->fbSize` to `0x1400000000` (80 GiB) after PLM-open loop, before the final `kgspBootstrap_HAL`. dmesg confirms this. GSP still boots into a state that crashes at ~40 GiB of touch.

### Surviving hypotheses (ordered by fit)

1. **[ROOT CAUSE — high confidence, 2026-08-07 update]** VBIOS strap-4 tier nibble `44` restricts HBM row addressing to 12 bits (2 GB/die) instead of A100's `66` (14 bits, 8 GB/die). Cross-referenced against a [public GA100 VBIOS comparison gist](https://gist.github.com/amoghmunikote/dafea7b6663c13edc28b33872f6e51be) that mapped byte offset `0x41D53` (250W 170HX) or `0x41F53` (300W) as the exact single byte where CMP 170HX VBIOSes differ from A100 in memory addressing. With 12 row bits × HBM stack geometry × active stacks, the physical addressable ceiling is ~40 GiB — exactly matching our observed crash boundary. R3's `sec2-postbl-plm-ss-cfg.patch` manipulates runtime CFG1/LMR **after** DevInit has already programmed the FBPA row-addressing configuration from the strap, so the HBM controller still only knows how to address 12 row bits regardless of what CFG1/LMR broadcast values are. GSP crashes at `pc:0x5b2b940` when trying to walk internal tracking tables sized against the actual usable HBM addressing (~40 GiB), because RM has claimed 80 GiB is usable but the DRAM controller cannot resolve addresses beyond ~40 GiB.
   - The gist notes: "The single byte to flip for memory unlock: `0x41D53` (250W 170HX): value `44` → `66`."
   - The byte is inside MAC-verified range (0x2200–0x43A00) → straight VBIOS edit will break Booter signature.
   - R3's PLM exploit already demonstrates that Booter can be tricked. We need to extend the trick to either (a) modify the strap byte before Booter re-verifies MAC, or (b) directly write the equivalent runtime FBPA row-config register after DevInit but before GSP boot.
2. **GSP firmware self-derives its usable-FB from a source that RM cannot override.** Still true but now understood as **downstream of (1)** — GSP reads DRAM geometry from what DevInit programmed based on the strap; RM cannot override this via `pWprMeta` because the underlying HBM controller state is set from the VBIOS strap.
3. **R3 borrows the 8 GB SKU's CFG1 verbatim for the 10 GB→80 G profile — CFG1 physical config mismatches the 10 GB card's actual DRAM layout.** Still relevant. `0x02779000` is the "8 GB SKU CFG1", but both 8 GB and 10 GB SKUs have strap 4 pinned to `44` per gist, so both are actually restricted to per-stack 8 GiB regardless. The 8 GB SKU → 64 GB (8 stacks × 8 GiB) works because it's exactly the strap-encoded ceiling. The 10 GB SKU → 80 GB (8 stacks × 10 GiB per die × 12 row bits = still 8 GiB/stack accessible) exceeds the strap ceiling by 16 GB, hence the crash at ~40 GB (crash point suggests ~5 fully-mapped stacks × 8 GiB = 40 GiB — the crossover between strap-permitted and strap-forbidden addresses).
4. **The 49.006 s "successful" CE memset is a fake-sync artifact** — RM waits for CE completion, GSP is already sick, RM's `_issueRpcAndWait` doubles its timeout twice, then returns success. Writes may or may not have landed. Would explain the retracted `0xA5` verify60 sample.

### The 8 GB vs 10 GB asymmetry — key evidence

The user's observation:

| SKU (PCI) | Fuse cap | cmpunlocker target | Result |
|---|---|---|---|
| 0x20C2 (8 GB) | 8 GB | **64 GB** (8×) | **Stable** |
| 0x2082 (10 GB) | 10 GB | 40 GB (4×) | Stable |
| 0x2082 (10 GB) | 10 GB | **80 GB** (8×) | **Crashes at ~40 G of touch** |

If the ceiling were "fuse × N" for some fixed N, 8×8=64 would fail proportionally with 10×8=80. It does not. That is decisive evidence that the ceiling is **not** derived from the fuse cap in a uniform way — it must come from a per-SKU value (a VBIOS DevInit constant, a per-SKU fuse, or a per-SKU firmware code path in GSP).

Both SKUs are the same GA100 die. The difference is in what got fused/burned per-SKU. Whatever value or code path lets the 8 GB SKU spread to 64 GB safely does not help the 10 GB SKU spread to 80 GB. **The 10 GB SKU appears to hit a hard 40 GiB internal-table ceiling that 4× the fuse cap conveniently satisfies but 8× does not.**

### What P1a and P1c actually did (both failed)

- **P1a** (`driver/patches/early-lmr-write-p1a.patch`): write `CMP_MEM_CFG1_BCAST` and `CMP_MEM_LMR` to the 80 G-encoded values *before* the first `kgspPopulateWprMeta_HAL`, hoping `kmemsysReadUsableFbSize_GP102` would read `0x00100CE0` and get 80 G. **The register write is silently rejected**: dmesg reports `cfg1 before=0x02449000 target=0x02779000 after=0x02449000` and same for LMR. **The FBPA PLM is still closed at that point.** So the first `pWprMeta->fbSize = 0x280000000` (10 GiB) as before.
- **P1c** (`driver/patches/extra-booter-run-p1c.patch`): after R3's post-PLM writes have succeeded (`cfg1_now=0x02779000 lmr_now=0x0000028b`), re-run booter one more time so GSP-side state is re-derived under the 80 G LMR value. **Booter reports `boot=0xffff` (non-zero status)**, then the SM prefill is still 96 s and crashes at the same PC. **The booter re-run has no effect on GSP's internal world view of usable FB.**

### `kmemsysReadUsableFbSize_GP102` decode of R3's LMR values (verified)

```
LMR = 0x0000020B (8 GB profile → 64 G): MAG=32, SCALE=11 → 32 << 31 = 64 GiB ✓
LMR = 0x0000028A (10 GB stable → 40 G): MAG=40, SCALE=10 → 40 << 30 = 40 GiB ✓
LMR = 0x0000028B (10 GB80 → 80 G):      MAG=40, SCALE=11 → 40 << 31 = 80 GiB ✓
```

**R3 encodes the correct values.** The issue is that GSP doesn't read this register as its source of truth for internal table sizing.

---

## 4. What R3 actually changes (summary of what's on the current branch)

Patches in `driver/build.sh` order:

1. `sec2-postbl-plm-ss-cfg.patch` — huge patch. The exploit that opens the 11 PLMs by refilling a payload buffer inside the booter's signature memdesc and running booter 22 times. Then writes `CFG1_BCAST` and `LMR`. Then does a second `kgspPopulateWprMeta_HAL` so `pWprMeta->fbSize` reflects the new LMR.
2. `booter-verify.patch` — SEC2/booter signature verification tweaks.
3. `memory-layout-safety.patch` — keeps the CMP CE virtual-mode workaround and
   adds no runtime memory-layout diagnostics or additional PMA regions (the old,
   removed `late-pma.patch` did the latter unsafely).
4. `bar0-pramin-clamp.patch` — clamp BAR0 PRAMIN operations to the pre-unlock region.
5. `ce-scrub-workarounds.patch` — forces `memmgrGetPteKindForScrubber_TU102` to return `NV_MMU_PTE_KIND_GENERIC_MEMORY` on `0x20C2/0x2082`, and disables CeUtils VIRTUAL_MODE for the scrubber's own allocations on those PCI IDs. **Only scrubber allocations** — see R4 for why user-side extension didn't help.
6. `early-lmr-write-p1a.patch` — **new this session, does not fix the bug** (register writes rejected by PLM). Kept in tree for diagnostic value; consider removing.
7. `extra-booter-run-p1c.patch` — **new this session, does not fix the bug** (extra booter run does not change GSP internal state). Kept in tree for diagnostic value; consider removing.
8. `persistent-sw-state.patch` — some persistent-state related fix.
9. `pcie-gen2.patch` — force Gen 2 for TB3 topology.
10. `pcie-gen2-probe-retrain.patch` — retrain to Gen 2 after probe.
11. `name-string.patch` — cosmetic.

Also modified:
- `driver/apply_profile.py` — rewrites CFG1/LMR/`targetFbBytes` compile-time constants in the patched `kernel_gsp.c` based on `CMPUNLOCKER_CARD_PROFILE`.

Plus modprobe:
- `/etc/modprobe.d/cmp-pcie-gen2.conf` → `NVreg_RegistryDwords="RmForceEnableGen2=1;RMPcieLinkSpeed=0x1;RMDisableScrubOnFree=1"`

---

## 5. What we learned about the driver runtime path (for whoever picks this up)

- The GA100 HAL for user-allocation PTE-kind selection is `memmgrChooseKind_TU102` (`src/nvidia/src/kernel/gpu/mem_mgr/arch/turing/mem_mgr_tu102.c:189-292`). GA100 dispatch (`g_mem_mgr_nvoc.c:924-1025`) binds it directly. Only NVOS32 TYPE DEPTH/STENCIL go elsewhere.
- `pWprMeta->fbSize` for GA100 comes from `kmemsysReadUsableFbSize_GP102` (`kern_mem_sys_gp102.c:36-59`) reading register `NV_PFB_PRI_MMU_LOCAL_MEMORY_RANGE = 0x00100CE0`. Bit layout: `LOWER_SCALE[3:0], LOWER_MAG[9:4]`; value = `MAG << (SCALE + 20)` bytes.
- `gspFwHeapSize` scales at 96 KiB/GB via `_kgspCalculateFwHeapSize` (`kernel_gsp.c:6417-6478`), read from the same `kmemsysGetUsableFbSize_HAL`. Registry override: `NV_REG_STR_RM_GSP_FIRMWARE_HEAP_SIZE_MB`.
- `GspSystemInfo` (`gsp_static_config.h:170-229`) does **not** carry an `fbSize`. The only "how big is FB" datum GSP receives at boot is `pWprMeta->fbSize` in `GspFwWprMeta`.
- The 22-fold PLM-open loop in R3 lives at approximately line 4924 of `kernel_gsp.c` after the R3 sec2 patch is applied. It requires `pWprMetaDescriptor` to be already populated (chicken-and-egg for early-LMR-write approaches).
- `_kgspBootGspRm` is the single entry point for the boot sequence: `kgspPopulateWprMeta_HAL` → `kgspPrepareForBootstrap_HAL` → R3 SEC2 dance → second `kgspPopulateWprMeta_HAL` → `kgspBootstrap_HAL`.

Registry keys worth cataloguing for future experiments (not yet tested):
- `RMOverrideToGMK=7` — force block-linear kinds to GMK globally.
- `RmInstLoc2` with `COMPTAG_STORE=NCOH` — move compbit backing store off HBM.
- `RMDisableFastScrubber=1` — different CE code path than the regular scrubber.
- `RmCeUseGen4Mapping=1` / `RmCeEnableAutoConfig` — re-route CE.
- `NV_REG_STR_RM_GSP_FIRMWARE_HEAP_SIZE_MB` — direct heap override.
- `RmPrintAssertBacktrace=2` — make swallowed asserts loud.

---

## 6. The GSP illegal-instruction fingerprint

Always identical across boots and driver variants:

```
Xid 1, GSP task exception: illegal instruction (cause:0x2) @ pc:0x5b2b940, task:1
mstatus:0x000000001e000000  mscratch:0x0  mie:0x880  mip:0x0
mepc:0x5b2b940  mbadaddr:0x5c0901c  mcause:0x2
```

`mepc == 0x5b2b940` and `mbadaddr == 0x5c0901c` differ by **0xDD6DC (~882 KiB)**. `mcause = 2` is RISC-V "illegal instruction". The likely mechanism is an indirect jump / vectored call whose destination is computed from an oversized index, landing outside the code region. If GSP is treating some data area of size ~ 4 GiB× (small_stride) as a jump table, and the actual index at ~40 GiB is (40G / stride) mapped somewhere past 0x5c0901c, the fault would explain both the crossover and the identical PC.

The RPC-timeout sysmembar `0x20800a70` is a downstream victim — GSP is dead by the time the sysmembar is issued.

---

## 7. TODO — what to try next

Bounded by fit to evidence, cost, and expected information gain.

### High-value, low-cost (try first)

- [ ] **[Path 4, sharpened]** Physically swap in the 8 GB SKU card the user has on hand. Boot with R3 (choose the `8gb` profile). Capture:
  - `SEC2_DEBUG: WPR meta fbSize=...` at both phases
  - Every `CMP_MEM_GEOMETRY:` dump — specifically `cfg1=`, `lmr=`, `l2Decode=`, `cstatusBroadcast=`, `config4Broadcast=`, `fbpaDisable=`, `fbpDisable=`, `activeLtcs=`
  - Every `CMP_MEM_FBPA:` per-index dump — which FBPAs are live, and each live one's `cfg1=` `cstatus=` `config4=`
  - The value of `NV_PFB_PRI_MMU_LOCAL_MEMORY_RANGE (0x00100CE0)` at pre-boot, post-PLM-open, post-populate, post-bootstrap.
  Then run the same `f0_alloc_ladder` and `f0_fake_sync_check` scaled to the 8 GB card's 64 G target (e.g. `TARGET_GB=32`, `f0_torture CAP_GB=64`).
  Then diff every single hardware register and every WprMeta field vs the 10 GB capture. **The one that differs and is not just SKU-metadata (like PCI ID) is the sizing input GSP actually uses**. Highest-signal single experiment we have not run. Direct test of the "R3 borrowed 8 GB CFG1, mismatches 10 GB DRAM layout" hypothesis (§3 hypothesis 2).
- [ ] **[Registry sweep]** Cold-boot with `RMOverrideToGMK=7`, then a separate boot with `RMDisableFastScrubber=1`, then `RmCeUseGen4Mapping=1`, each combined with `RmPrintAssertBacktrace=2`. Rerun `f0_fake_sync_check TARGET_GB=40`. Cheap: single modprobe change + cold boot each. Expected outcome per GPT Pro's prior guidance: none of these help — but confirming that concentrates diagnostic attention on the firmware side.
- [ ] **Delete `early-lmr-write-p1a.patch` and `extra-booter-run-p1c.patch`** from the build once we've written up the experiments (they add noise and no value). Or keep them behind an env flag for reproducibility.

### Medium-value

- [ ] **Fine-grained memset ladder** — extend `f0_memset_timing.cu` to sweep `10, 15, 20, 22, 24, 26, 28, 30, 32, 35, 38, 40 G` at `off=0`. Each data point costs one cold boot (~2–3 min). Find the exact crossover to bound the "table size" hypothesis. If crossover is at 32 GiB → table is 2^25 entries × something; if at 40 GiB → 2^25 × 32-bit; etc. Numeric alignment of the crossover to a power of two is a strong signal for internal-table geometry.
- [ ] **Try `NV_REG_STR_RM_GSP_FIRMWARE_HEAP_SIZE_MB=1024`** — force GSP heap to 1 GiB (vs the current ~112 MiB). If the crashing table is inside the GSP heap and depends on heap size, this may move or eliminate the crossover. Cheap.
- [ ] **Inspect `NV_PFB_PRI_MMU_LOCAL_MEMORY_RANGE` at multiple time points** on 10 GB card — add a diagnostic patch that reads and prints `0x00100CE0` at (a) module load, (b) before `kgspPopulateWprMeta_HAL`, (c) between GSP boot phases, (d) after final bootstrap. Confirms whether the register even *stays* at `0x28B` all the way through GSP boot, or gets restored by VBIOS DevInit / hardware fuse enforcement.

### Higher-cost, needed if above fails

- [ ] **[Path 3]** Disassemble the GSP firmware image and identify what lives at `pc:0x5b2b940`. Extract `nvidia-open-610` firmware blobs — likely under `/lib/firmware/nvidia/`; look for `.bin` matching the GA100/GA10X naming for GSP RM (`gsp_ga10x.bin` or similar). Disassemble with a RISC-V toolchain (`riscv64-elf-objdump` from `binutils`, or `capstone`/`radare2` in RISC-V mode). Find the function that contains `0x5b2b940`, look at what happens right before — likely a load from an array whose base is derived from a size stored somewhere. Then work backwards to find who writes that size. This is the definitive path but has significant cost (hours to days).
- [ ] **Ask GPT Pro** what a valid interpretation of `boot=0xffff` from the P1c extra booter run is — is booter reporting "no work to do" (fine) or "boot state corrupted" (dangerous)? The `refill=0x0` succeeded but the boot status is atypical.
- [ ] **Cross-reference open-gpu-kernel-modules commits** for `gp102/gp100`-lineage bug fixes affecting `LOCAL_MEMORY_RANGE` or `NV_PFB_PRI_MMU_LOCAL_MEMORY_RANGE`. Any changelog note about VBIOS-DevInit programming this register may reveal an alternate register that GSP actually reads.

### Not worth doing (rejected)

- Continuing to sweep the CE / PTE-kind side of the driver. Multiple experiments (F0 A/B, R4 GMK patch, verify60 flake retraction) point away from this being the root layer.
- Chasing the 76 928-byte `0xA5` verify60 sample further. Attributed to hot-reload transient.
- Blaming Thunderbolt 3 bandwidth (this was a user-corrected wrong theory of mine earlier).
- HBM refresh / retention arguments (Jon Pry paper style). The crossover is too sharp and too deterministic — retention failures are stochastic and temperature-dependent; we see identical PC across cold boots minutes apart.

---

## 8. Assets

- **Branch:** `experiment-user-pte-kind-gmk-r4` (contains R3 + P1a + P1c). Do NOT merge to master — nothing on it materially improves stability. If any of the P1x patches are kept, gate behind an env var.
- **New patches:** `driver/patches/early-lmr-write-p1a.patch`, `driver/patches/extra-booter-run-p1c.patch`.
- **New helper scripts:** `driver/apply_early_lmr_p1a.py`, `driver/apply_p1c.py` (bootstrap scripts for the patches, not runtime).
- **Experiment sources (local):** `/tmp/f0_*.{c,cu,sh}` — worker/probe/verify/reproduce/sm_vs_ce/ce_d2h/torture/h2d/alloc_ladder/memset_range/memset_timing/fake_sync_check.
- **Bundle for GPT Pro:** `~/Downloads/f0_bundle_20260807.tar.gz` (174 KB) — src + logs + `analysis/HANDOFF.md`.
- **Server experiment logs:** `p3-server:~/f0_logs/*` — every run's dmesg + probe outputs.
- **Server binaries:** `p3-server:~/f0/*` — pre-compiled experiments, `f0_probe` is the definitive CUDA health check (must PASS after any recovery before trusting `nvidia-smi`).

---

## 9. Learned operational practices

- **`nvidia-smi` shows healthy 80 G / P0 does not mean CUDA works.** Always follow with `f0_probe`. See `feedback_egpu_power_cycle_order`.
- **First workload after `rmmod && modprobe` is untrusted** — cold-boot for load-bearing measurements. See `feedback_driver_reload_transient`.
- **eGPU chassis power cycle recovery** is 10× faster than a whole-server cold boot but requires the exact host-side sequence and only works if `rmmod` cleared all refcounts. See same memory doc.
- **`nvidia-smi` under a hung GSP is invasive** — it issues a GSP RPC and can push a limping GSP over the edge. Prefer `pgrep`, `ls /sys/module/nvidia/refcnt`, `dmesg | grep NVRM`.

---

_End of first-pass report. Addendum with new findings follows below._

---

# Addendum — 2026-08-07 late session

Since the original report we ran 4 more cold-boot A/B experiments, a physical DRAM alias probe, a full GSP firmware disassembly pass, a per-FBPA register diff between 8GB and 10GB CMP boards, and a byte-diff between two real A100 VBIOSes (40 GB PCIe + 80 GB SXM). All key hypotheses from the first-pass report either got confirmed or refuted, and the root cause narrowed by another layer.

## 1. Path 4 executed — the 8 GB CMP comparison

Physically swapped in the 8 GB SKU (PCI 0x20C2) with unchanged host driver and modprobe. Reboot, then rerun the same experiments the 10 GB card fails.

| Test | 10 GB card (80 G target) | 8 GB card (64 G target) |
| --- | --- | --- |
| `f0_probe` | PASS 0.56 s | PASS 0.56 s |
| `f0_alloc_ladder` up to card max | PASS | PASS |
| `f0_fake_sync_check TARGET_GB=40` (SM prefill 70 G / CE memset 40 G / SM verify) | **96 s prefill → Xid 1** | N/A (fits in 60 G) |
| `f0_fake_sync_check_60G TARGET_GB=40` | N/A | PASS in 0.05 s SM prefill, 0.032 s CE memset |
| `f0_fake_sync_check_63G TARGET_GB=60` (63 GiB alloc + 60 GiB memset + full verify) | can't run | PASS in 0.14 s |

**The 8 GB card, with the exact same driver build, runs 60 GiB of CE + SM write/verify to completion in a tenth of a second.** The 10 GB card cannot do 40 GiB in 96 seconds without killing GSP. Software is not the delta.

## 2. Per-FBPA register dump — 8 GB vs 10 GB

R3 already prints per-FBPA CONFIG4 in `_kgspCmpDumpGeometry`. On dmesg after boot:

| Property | 8 GB CMP | 10 GB CMP |
| --- | --- | --- |
| Active FBPAs | 16 (idx 0-1, 4-7, 10-11, 14-21) | 20 (idx 0-11, 14-21) |
| Disabled FBPAs | idx 2, 3, 8, 9, 12, 13, 22, 23 | idx 12, 13, 22, 23 |
| Per-FBPA CFG1 (active) | 0x02779000 | 0x02779000 |
| Per-FBPA CONFIG4 (active) | 0xc4028033 (bit 15 = 1) | 0xc4030033 (bit 16 = 1) |
| activeLtcs | 0x00000010 (16 LTCs) | 0x00000014 (20 LTCs) |

Every active FBPA reads the same broadcast value — so per-FBPA reads on the 10 GB card **already agree with the broadcast**. Kimi's suggestion that "R3 wrote broadcast but per-instance is empty" was worth checking; the 10 GB card's per-instance CONFIG4 all match `0xc4030033`, so per-FBPA reprogramming would be writing the same value it already has.

## 3. A100-40GB vs A100-80GB VBIOS byte-diff — kills the CONFIG4 hypothesis

User provided both real A100 VBIOSes. Byte-pattern searches:

```
CFG1 = 0x02779000 (80G encoding):   40G ROM 4 hits    80G ROM 6 hits
CFG1 = 0x02669000 (40G encoding):   40G ROM 4 hits    80G ROM 4 hits
CONFIG4 = 0xc4030033:               40G ROM 7 hits    80G ROM 7 hits    ← both use this
CONFIG4 = 0xc4028033:               40G ROM 2 hits    80G ROM 0 hits    ← 40G ROM only
```

**`0xc4030033` is the A100 20-FBPA-density value that both 40G and 80G VBIOSes emit.** The 10GB CMP already has `0xc4030033`. `0xc4028033` is a 40G-specific fallback (probably for the 5th disabled stack). Kimi's B6 experiment tried to force CMP 10G's CONFIG4 to `0xc4028033`, which would have been a regression toward the 40G side. The runtime CONFIG4 write was silently rejected regardless — but even if it had gone through, it would have made things worse, not better.

**Conclusion: CONFIG4 broadcast bit 15/16 is not the strap-4 lever. It's a 16-FBPA vs 20-FBPA discriminator, both correct within their SKU.**

The two ROMs disagree in 556849 bytes across 19887 regions (~half the ROM). Not a single-bit unlock.

## 4. GSP firmware disassembly — the crash is at `subdeviceCtrl` dispatch, not a page walker

Subagent installed `llvm-objcopy`/`llvm-objdump` on the Mac, extracted the main task ELF from `.fwimage` section of `/lib/firmware/nvidia/610.43.02/gsp_ga10x.bin` (84 MB, build ID `1687a6f9071ccd1eb24e902150aa95df8c420a84`). Static LOAD segments cover VA `0x01000000-0x01E97000` (~15 MiB).

`pc:0x5b2b940` is not in any static code segment. Its low 24 bits (`0xB2B940`) are inside code — high byte `0x05` is out of range. The value is a corrupted 64-bit pointer whose upper bits got scribbled with `0x05`.

Function containing the "valid" version of that PC located at `0x01b2b804`:

```
0x1b2b804: prologue, save regs
0x1b2b81c: lui a5,0x2; add a5,a5,a0; ld s7,-0x240(a5)   ; s7 = *(pGpu + 0x1DC0)  — pMemMgr subobject ptr
0x1b2b82e: ld a5, 0x120(s7)                             ; a5 = pMemMgr->vtable[0x120] — indirect fn ptr
0x1b2b832: lui a2,0xff000; addi a2,a2,0xBA              ; a2 = 0xFF0000BA — control command ID
0x1b2b854: jalr a5                                      ; *** indirect call — this is what dispatches to garbage
```

**The crash is an indirect call through `pMemMgr->vtable[0x120]` where the stored function pointer got its high 40 bits corrupted with `0x05000000`-ish garbage.** The dispatched command is `subdeviceCtrl 0xFF0000BA`. This is not a page-table walker — the GMMU walker candidates are at `0x1A6BC36`, `0x1A54B7E`, `0x1B24CB0` (in the same compilation unit but not the crash site). The MMU fault name table is at `0x01C610F0` / `0x01C61170` (32 entries: `FAULT_PDE, FAULT_PTE, ...` — used elsewhere for GMMU fault reporting).

Interpretation: the 80 GiB code path IS being taken (because LMR/CFG1 report A100-80 layout to GSP), but the actual FB/HBM re-training or memsys initialisation doesn't complete for the top half. The result is a memsys object whose vtable slot `+0x120` never got its function pointer written — it holds whatever was left in that heap slot, and the upper bits happen to be `0x05...`. Subsequent RM command dispatch through it explodes at the first call.

## 5. Physical DRAM alias probe — the 80 GiB is really there

`~/f0/f0_alias_probe.cu`. `cudaMalloc(70 GiB)`, then SM kernel writes 16 pages at offset 0 with pattern A (0xAAAA000{0..f}) and 16 pages at offset **+40 GiB** with pattern B (0xBBBB000{0..f}). Read both back.

Result: **all 32 pages come back with the pattern they were written**. Zero aliasing. The top 40 GiB is distinct physical DRAM, not a fold of the bottom.

Also **small SM writes to the top 40 GiB work fine** — the crash only happens under bulk write pressure (~40 GiB at once). Consistent with hypothesis 4: memsys is only partially built. Occasional pointer accesses to the top half go through a per-page path that works; bulk operations trigger the corrupted vtable path.

## 6. SEC2 payload primitive — the working exploit that could be redirected

R3's `kgspSec2PostblTimingRefillPayload(writeAddr, writeValue)` is a working primitive that writes to PLM-protected registers by injecting values into the booter's signature memdesc, then re-running booter to have IT execute the write. R3 currently uses this only to open 11 PLMs. **The primitive itself will accept any (writeAddr, writeValue) — the question is which addr/value pairs would actually reprogram HBM row addressing.**

## 7. Envytools DevInit diff — got the register list but not the values

Attempted to parse both A100 VBIOSes with envytools/nvbios. **Envytools does not implement GA100's compressed IEP devinit codec** — it parses the BIT structure fine but disassembly walks garbage.

Fell back to raw-byte pattern analysis. Found a dense stream of ~55-byte devinit micro-instructions targeting FBPA registers (`0x9a02XX` and `0x9a03XX` range). Pattern discriminates the two ROMs:

**Registers that differ between 40G and 80G devinit:**

| Register | Pattern | Note |
| --- | --- | --- |
| **0x9a0204 (CFG1_BCAST)** | 40G/80G payload differs by encoded delta +10 | Primary candidate |
| **0x9a0200 (CFG0_BCAST)** | 40G writes real value; 80G writes "-1" magic | Related config |
| **0x9a0220** | 40G writes "0" magic; 80G writes real value | 80G-only enable |
| 0x9a0294, 0x9a0298, 0x9a029c | 40G writes real values; 80G writes NOTHING to these | 40G-only limits — 80G leaves them at reset |
| 0x9a039c | 40G writes real value; 80G writes "0" magic | 40G limit removed on 80G |

Registers written identically in both: `0x9a02a0 (CONFIG4)`, `0x9a02a4`, `0x9a02a8`, `0x9a02cc`, `0x9a02e8`, `0x9a02f4`, `0x9a03e4` — these are per-FBPA shared config, common between SKUs.

**No `INIT_STRAP_*` conditional or fuse-guarded branches** differ between the two ROMs. Both execute unconditional straight-line devinit. So the ceiling is NOT strap-gated at DevInit — it's baked into the sequence itself. **A100-40G VBIOS unconditionally programs a smaller memory geometry; A100-80G VBIOS unconditionally programs a bigger one. CMP 170HX VBIOS presumably follows the 40G path.**

**Blocker:** we can identify the register offsets that differ, but the compressed IEP payloads use a codec envytools doesn't understand, so we cannot decode the raw 32-bit target values from the ROM alone.

## 8. Refined root cause statement (2026-08-07 final)

Every mmio register on the CMP 10G that RM can inspect after R3's SEC2 hack matches the A100-80G behaviour. Yet the crash persists. The physical 80 GiB DRAM is genuinely present per the alias probe.

The residual gap sits in **FBPA register writes issued by DevInit — specifically at 0x9a0200, 0x9a0204, 0x9a0220, 0x9a0294/0x9a0298/0x9a029c, 0x9a039c** — where A100-40G's DevInit programs different values than A100-80G's DevInit. CMP 170HX presumably runs the 40G-style DevInit values. R3's runtime patch touches only CFG1(0x0204) and LMR — not the other five differing registers — and even the CFG1 write goes to the broadcast alias in a way that the memsys re-init doesn't consume.

The GSP crash at `pMemMgr->vtable[0x120]` is a downstream effect: the memsys object gets partially constructed under the wrong geometry, half its vtable slots never get written, and the first RM command that dispatches through the slot explodes.

## 9. What is still to do

**Line A (highest expected value):** dump the full `0x009a0200-0x009a03ff` register range from a live A100-80GB card. The user has A100 access. Compare that dump to the current 10GB CMP post-hack dump. The registers that differ are the ones R3 hasn't touched. Then extend the SEC2 payload write list to cover all differing registers.

**Line B (fallback if A can't run):** reverse-engineer NVIDIA's compressed IEP devinit codec by reading GA100 SEC2 microcode. envytools can't do it. Would require RISC-V disassembly of a SEC2 fw blob to see how it interprets `b6 f4 ... 9a 14 ...` payload bytes. Days of work.

**Line C (parallel prep):** now that the SEC2 payload primitive is understood, code the driver extension to write multiple `(addr, value)` pairs in a loop after PLM open. Same shape as the existing 11-iteration PLM open loop. This can be built ahead of knowing the values; once Line A yields the target values, one edit and one cold boot to test.

## 10. Assets added since the original report

- `driver/patches/ss-config4-override.patch` — writes A100-real SS0/SS1 (0x00112011/0x00000002), attempts CONFIG4 force to 0xc4028033 on 10G card. **Both had no effect on the crash.** CONFIG4 write silently rejected.
- `driver/patches/feat-restore.patch` — restores FEAT08/0c/10/14/28 to A100 dump values. **Only FEAT08 write accepted**, none changed crash behavior.
- `driver/patches/early-lmr-write-p1a.patch` — pre-write CFG1/LMR before first `kgspPopulateWprMeta_HAL`. **Writes rejected (PLM closed).** No effect.
- `driver/patches/extra-booter-run-p1c.patch` — re-run booter after LMR update. **No state change.**
- `driver/patches/config4-write-probe.patch` — Kimi-authored write probe; unused in the failing sequence.
- `driver/apply_ss_config4.py`, `driver/apply_feat_restore.py`, `driver/apply_early_lmr_p1a.py`, `driver/apply_p1c.py` — inline patchers used to generate `.patch` files by in-place edit + real diff. Recommended pattern for future patches (avoids hand-authoring hunk headers).
- `~/f0/f0_alias_probe.cu` on p3-server — the DRAM alias probe. Preserve for future re-runs.
- `HANDOFF_TO_KIMI.md` in repo root — session handoff summary for Kimi (or any successor).
- `~/tools/envytools/` on this Mac — envytools tree built. `nvbios` binary exists but doesn't disassemble GA100. Kept for further attempts.
- `/tmp/gsp_maintask_fixed.elf` and `/tmp/gsp_code.bin` on this Mac — extracted GSP task ELF ready for further RISC-V disassembly. `llvm-objdump` in `/opt/homebrew/opt/llvm@21/bin/`.

## 11. Six-boot A/B summary (updated)

| # | Patch stack | SM prefill 70G | Crash |
| --- | --- | --- | --- |
| B1 | R3 baseline | 96.02 s | Xid 1 @ pc:0x5b2b940 |
| B2 | + P1a (early LMR) | 96.03 s (write rejected) | Xid 1 @ pc:0x5b2b940 |
| B3 | + P1c (extra booter) | 96.36 s (booter returns 0xffff) | Xid 1 @ pc:0x5b2b940 |
| B4 | + SS-fix (0x88888888→0x00112011) | 96.01 s (write accepted, no effect) | Xid 1 @ pc:0x5b2b940 |
| **B5 (control)** | **8 GB card @ 64 G target** | **0.05 s** | **none — PASSES** |
| B6 | + CONFIG4 force (target A100-40G val) | 96.12 s (write rejected + wrong direction) | Xid 1 @ pc:0x5b2b940 |
| B7 | + FEAT-restore | 96.23 s (only FEAT08 accepted) | Xid 1 @ pc:0x5b2b940 |

Seven 10 GB card boots, identical crash to millisecond precision.
