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
80GB target. Capacity recognition alone does not establish workload stability;
see [Experimental 80GB](docs/EXPERIMENTAL_80GB.md).

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
| Experimental 80GB geometry on 10GB cards | Opt-in; stability not established |
| PCIe Gen 2 speeds | Working ✓ |
| JTAG (Host2Jtag register access) | Working ✓ |
| WPR/PMA reserved-memory protection (`wpr-safe-r3`) | Working; old unsafe module rejected |
| Persistence across reboot (patched modules) | Working ✓ |

---

## Uninstall

To uninstall cmpunlocker, run the following command:

```bash
sudo ./remove.sh --yes
```

Then perform a cold reboot (full power off, then boot).

## Support & Community

Having issues? Need help? Join our [Discord community](https://discord.gg/CdHSakKSFv) to discuss with other users and get support.
