# cmpunlocker

Unlock tool for the NVIDIA CMP 170HX (GA100) mining card. Restores full SM compute throughput and unlocked HBM2e memory geometry that are restricted in firmware/OTP configuration.


**[Join our Discord community](https://discord.gg/CdHSakKSFv)** for support and discussions.

---
## Proof of Concept

Below are memory and performance results after applying the unlock:

### Memory Unlock Results

<img alt="memory unlock" src="https://github.com/user-attachments/assets/ae062bd8-e3a7-4e73-b9a4-fbcde53f3c7b" width="100%" style="max-width: 900px;" />

### Performance Benchmarks ([OpenCL-Benchmark](https://github.com/ProjectPhysX/OpenCL-Benchmark))

<img alt="performance benchmarks" src="https://github.com/user-attachments/assets/2501506d-420f-4014-9574-b1bd0290eb60" width="100%" style="max-width: 900px;" />

---

## Requirements

- Linux (x86-64)
- Root access
- NVIDIA CMP 170HX
- **nvidia-open 610.43.0x already installed** (libs + firmware)
- Kernel headers matching the running kernel (`linux-headers-$(uname -r)` / `kernel-devel`)
- Secure Boot disabled (patched modules are unsigned)
- Network access on first install (downloads matching stock `open-gpu-kernel-modules` sources)
- Python 3 (used at build time to select the compiled memory geometry)

---

## Install

To install cmpunlocker, run the following command:

```bash
sudo ./install.sh
```

To force a certain memory profile, use the `--profile` option:

```bash
sudo ./install.sh --profile=8gb    # 8GB card → 64GB unlock
sudo ./install.sh --profile=10gb   # 10GB card → 40GB unlock
```

The stable default for a `10de:2082` 10GB card remains 40GB. An explicit
experimental profile is also available:

```bash
sudo ./install.sh --profile=10gb80 # 10GB card → experimental 80GB geometry
```

This compiles the coherent 80GB values into the real driver path:
`CFG1=0x02779000`, `LMR=0x0000028B`, and
`fb_length=0x0000001400000000`. It is never selected automatically. On a mixed
20c2+2082 system, 20c2 cards stay on 64GB while 2082 cards use the experimental
80GB target.

**Production recommendation (Aug 2026 verdict): use 40GB (`--profile=10gb`).**
The 80GB profile is fully validated (gpu_burn 300s clean, 256K-context LLM
inference working) but the 40G fold wall caps its *safely usable* pool at
~31–36GiB ([5G,36G) between the fold-impact zone and the phantom-reserve hole),
which is **less** than the clean 40GB profile — and any allocation crossing 36G
risks silent fold corruption. The 80GB profile remains as a research artifact;
see [Final verdict on the 40G wall](docs/FINAL_VERDICT_40G_WALL.md) and
[Experimental 80GB](docs/EXPERIMENTAL_80GB.md).

### WPR/PMA safety revision

This source includes the `wpr-safe-r3` fix. The former experimental late-PMA
path that converted the highest reserved FB region into allocatable memory has
been removed. GSP WPR, firmware heap, metadata, and other `bRsvdRegion`
carveouts remain reserved and are never passed to `pmaRegisterRegion()` by
cmpunlocker.

The safe allocatable amount can therefore be slightly lower than the capacity
shown by `nvidia-smi`; that difference is expected firmware/driver reservation,
not missing user memory. `build.sh` and `verify.sh` reject modules containing
the removed late-PMA marker.

The installer does not hot-reload the GPU driver by default. Perform a complete
power-off/cold boot, then run:

```bash
sudo ./verify.sh
sudo ./tools/collect-diagnostics.sh
```

For the first high-memory workload, run it through the monitor so both the
pre-failure state and the first kernel error are preserved:

```bash
sudo ./tools/run-monitored.sh --interval=1 --output=/root/cmp-logs -- \
  python3 your_workload.py
```

The monitor writes a timestamped archive plus SHA-256 checksum even when the
workload exits with an error. Do not use `CMPUNLOCKER_ALLOW_HOT_RELOAD=1` for
stability qualification; that developer override cannot prove stale GSP/WPR
state was cleared.

## What Gets Unlocked

| Feature | Status |
|---|---|
| Full SM compute throughput (SS0/SS1) | Working ✓ |
| Memory geometry (64GB on 8GB cards, 40GB on 10GB cards) | Working ✓ |
| 80GB geometry on 10GB cards | Working ✓ but **40GB recommended for production** — the 40G fold wall caps 80G's safe pool below the 40G profile's (see verdict below) |
| PCIe Gen 2 speeds | Working ✓ |
| PCIe Gen 3/4 | Not unlockable — blocked at fuse/PHY-cal layer (see verdict) |
| JTAG (Host2Jtag register access) | Working ✓ |
| WPR/PMA reserved-memory protection (`wpr-safe-r3`) | Working; old unsafe module rejected |
| Persistence across reboot (patched modules) | Working ✓ |

---

## Research campaign (August 2026)

A week-long deep investigation pushed the 10GB card to full 80GB geometry and then
hunted the "40G wall" (writes above 40G fold back with a +35GiB alias) all the way
to its root cause. Full archive: [docs/RESEARCH_INDEX.md](docs/RESEARCH_INDEX.md);
final verdict: [docs/FINAL_VERDICT_40G_WALL.md](docs/FINAL_VERDICT_40G_WALL.md).

**Milestones**

- **Aug 6–8** — 80GB geometry unlock stabilized (81920 MiB in `nvidia-smi`), phantom-reserve
  memory-safety scheme, full compute throughput restored (FP32 0.39 → 12.26 TFLOPS,
  BF16 166 TFLOPS class).
- **Aug 8–9** — The "32G/40G wall" discovered and independently re-confirmed; wall signature
  fully decoded: +35GiB fold with a 256-byte-granular tail, 8G (HBM-die) periodicity
  ([docs/WALL_ALIAS_DECODE.md](docs/WALL_ALIAS_DECODE.md)). Hole narrowed 8G→5G.
- **Aug 10** — SEC2 post-authentication DMA injection platform built: runtime GSP-RM patching
  in WPR, LEVEL2 (HS) register writes, VBIOS-table patch hooks — all without touching any
  signature ([docs/PLAN_SEC2_DMA_POSTPATCH.md](docs/PLAN_SEC2_DMA_POSTPATCH.md)).
- **Aug 10–11** — Systematic elimination of every software-reachable layer: all 9 writable FBPA
  registers (single + combined writes, persistence-verified), 3 regkey reroutes, host-side
  VBIOS table patching, VBIOS flashing risk analysis, and the WPR2 staging-window (R1) probe.
  VBIOS table semantics cracked: the real CMP-vs-A100 delta is 19 per-partition dwords
  (columns 7/8) selected by fused device-id inside the signed/encrypted FWSEC devinit ucode
  ([docs/re/A100_40G_80G_COLUMN_DIFF.md](docs/re/A100_40G_80G_COLUMN_DIFF.md),
  [docs/re/LATE_OVERRIDE_0294.md](docs/re/LATE_OVERRIDE_0294.md)).
- **Aug 11 — Final verdict:** the 40G wall (and PCIe Gen3/4) is a fuse-selected, cryptographically
  authenticated SKU decision; short of NVIDIA's signing key or fuse authority it is not
  software-unlockable. Shipped production configuration: 80GB geometry + 5G phantom reserve,
  validated end-to-end (gpu_burn 300s, 68GB occupied, 0 errors, zero Xid; llama 256K-context
  inference working with KV cache placed below the fold).

**Tooling left behind:** BAR0 MMIO read/write probes, wall/fold scanners and the alias decoder
(`tools/`), the SEC2 probe generator framework (`driver/apply_sec2_dma_probe.py`), and 22
reverse-engineering notes ([docs/re/](docs/re/)). Firmware binaries and disassembly are
intentionally not published.

---

## Uninstall

To uninstall cmpunlocker, run the following command:

```bash
sudo ./remove.sh --yes
```

Then perform a cold reboot (full power off, then boot).

## Support & Community

Having issues? Need help? Join our [Discord community](https://discord.gg/CdHSakKSFv) to discuss with other users and get support.
