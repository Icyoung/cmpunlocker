# SEC2 refill DMEM template (PLM exploit → verify bypass path)

> 2026-08-09. Companion to `BOOTER_RE.md`.

## What the signature memdesc really is

`pSignatureMemdesc` is **not** a 16-byte AES signature at runtime on CMP cards with
`sec2-postbl-plm-ss-cfg.patch`. It is a **0xf800-byte DMEM template** copied into
SEC2 DMEM via WPR meta (`sysmemAddrOfSignature`).

| Constant | Value |
|---|---:|
| `SEC2_POSTBL_TIMING_SIGNATURE_SIZE` | `0xf800` |
| `SEC2_POSTBL_TIMING_FILL_DWORD` | `0x000004a7` (NOP sled between gadgets) |
| Optional file | `/lib/firmware/nvidia/ga100/gsp/dmem.bin` (missing on p3-server; built-in fill used) |

`kgspSec2PostblTimingRefillPayload(addr, value)` overwrites two parameter slots
then re-runs Booter; Booter returns **`0x31`** (theater) while the gadget performs
one PLM MMIO write.

## Proven MMIO gadget slots

From `_kgspSec2PostblTimingFillPayload`:

| DMEM off | Role | Example |
|---:|---|---|
| `0xf754` | **writeValue** | `0xffffffff` (open PLM) |
| `0xf76c` | **writeAddr** | `0x00823804` (FEAT), `0x009a0148` (FBPA), … |
| `0xf758`, `0xf75c`, … | Falcon insn / sentinel mix | `0xc0deca7e` markers |

Rebuild locally:

```bash
cd gsp_analysis
python3 build_refill_dmem.py -o sig_dmem_template.bin
```

## Booter OS ↔ DMEM pointer

Theater stub `lcall 0xd6` (**OS IMEM 0xd6**):

```text
mov  r15, 0xd2408
mov  r9,  0x6140
st   D[r9], r15        ; DMEM[0x6140] = 0xd2408
ret
```

- Static booter image has **zeros** at DMEM `0x6140` (image offset `0x8500+0x6140`).
- Value `0xd2408` is **not** an offset into the 0xf800 template (too large).
- App code @ `lcall 0x100` consumes `DMEM[0x6140]` together with WPR-meta
  signature pointer — exact decode still open.

**Hypothesis:** `0xd2408` is a **virtual address** (IMEM or sysmem) of the entry
trampoline into the refill gadget chain, not a byte offset into `sig_dmem`.

## Why disasm of the fill buffer looks broken

Treating the whole 0xf800 blob as sequential IMEM fails: the buffer is **data +
scattered insn dwords** interpreted by app logic. Only the tail cluster around
`0xf754` disassembles coherently when isolated.

## Verify bypass via refill (planned)

Same primitive as PLM open, new goal:

1. Capture **real** `dmem.bin` from hardware once (`RMCmpSigDmemDump=1`, see below).
2. RE the gadget interpreter in encrypted app (or trace which DMEM offsets it reads).
3. Add a **second gadget** (or extend tail) that:
   - runs after `xdst` decrypt of app IMEM, and
   - patches secure IMEM (NOP `csigcmp` branch) **or**
   - forces verify success path in DMEM metadata.
4. Trigger via new `kgspSec2PostblTimingRefillPayload` variant during **GSP-RM load**
   boot (after patch A), not during PLM loop.

**Do not** NOP OS `mbox=0x31` — PLM loop depends on it.

## Safe next hardware step (when server is stable)

One-shot dump hook (no Booter retry loop):

```bash
sudo modprobe -r nvidia_drm nvidia_modeset nvidia_uvm nvidia
echo 1 | sudo tee /sys/bus/pci/devices/0000:3d:00.0/reset
sudo modprobe nvidia NVreg_RegistryDwords="RmForceEnableGen2=1;RMPcieLinkSpeed=0x1;RMDisableScrubOnFree=1;RMCmpSigDmemDump=1"
sudo modprobe nvidia-modeset
# dmesg | grep CMP_SIG_DMEM_DUMP
```

Live capture matches synthetic template byte-for-byte (`sig_dmem_live.bin` ≡
`build_refill_dmem.py` output except parameterized `0xf754`/`0xf76c`).

Gadget map: `sig_dmem_template.gadget.md` (from `disasm_refill_gadget.py`).

## DMEM slot experiment (`apply_booter_verify_bypass.py`)

**Post-PLM only** — runs after `RebuildStockSignature`, before `kgspPopulateWprMeta`.
Does not touch PLM refill loops (avoids `0x31` storms from hdr tweaks).

| Regkey | Example | Meaning |
|---|---|---|
| `RMCmpBooterVerifyBypass` | `1` | Master enable: replace stock sig with 0xf800 gadget |
| `RMCmpDmemSlotOff` | `63480` | Extra dword offset (`0xf7f8` tail insn) |
| `RMCmpDmemSlotVal` | `1191` | u32 value (`0x4a7` NOP sled) |
| `RMCmpDmemGadget2` | `1` | Clone tail gadget to `0xe754` |
| `RMCmpDmemGadget2Addr` | `…` | Second MMIO write address (`0xe76c`) |
| `RMCmpDmemGadget2Val` | `…` | Second MMIO write value (`0xe754`) |

Log lines: `CMP_POSTPLM_VERIFY_BYPASS`, `CMP_DMEM_SLOT_EXP`, `CMP_GADGET2`

**Protocol (one knob per FLR boot):**

```bash
# Baseline FLR — BooterLoad status=0x0
sudo modprobe -r nvidia_drm nvidia_modeset nvidia_uvm nvidia
echo 1 | sudo tee /sys/bus/pci/devices/0000:3d:00.0/reset

# Step 1: patch A only (expect 0xb)
sudo modprobe nvidia NVreg_RegistryDwords="RmForceEnableGen2=1;RMPcieLinkSpeed=0x1;RMDisableScrubOnFree=1;RMCmpGspFwPatchA=1"
# FLR restore, then:

# Step 1b: patch A + post-PLM gadget + tail slot
sudo modprobe nvidia NVreg_RegistryDwords="...;RMCmpGspFwPatchA=1;RMCmpBooterVerifyBypass=1;RMCmpDmemSlotOff=63480;RMCmpDmemSlotVal=1191"

# Step 2: patch A + gadget2 clone (set addr/val to IMEM poke target when known)
# ...;RMCmpBooterVerifyBypass=1;RMCmpDmemGadget2=1;RMCmpDmemGadget2Addr=0;RMCmpDmemGadget2Val=0
```

**Candidate offsets (post-PLM path only):**

| Off | Stock | Notes |
|---:|---:|---|
| `0x1100` | `0x7` | hdr — test only on post-PLM path |
| `0xf7f8` | `0x7f2f` | tail insn |
| `0xe754`/`0xe76c` | clone | second MMIO gadget (gadget2) |

Success: `RMCmpGspFwPatchA=1` + bypass → `normal BooterLoad status=0x0`.

## Related files

- `driver/patches/sec2-postbl-plm-ss-cfg.patch` — refill implementation
- `gsp_analysis/booter_os.disasm.txt` — OS `0xd6` / `0x76` / `lcall 0x100`
- `gsp_analysis/build_refill_dmem.py` — synthetic template builder
