# Installation

Here are the steps to install cmpunlocker on your system.

---

## Requirements

- NVIDIA CMP 170HX (8GB or 10GB)
- Linux operating system (Ubuntu, Debian, Fedora, etc.)
- Kernel headers matching the running kernel (linux-headers-$(uname -r) / kernel-devel)
- Python 3
- **nvidia-open 610.43.0x already installed** (libs + firmware)
- Root access to the system (sudo privileges)
- Secure Boot disabled
- Network access on first install (downloads matching stock open-gpu-kernel-modules sources)

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

The 10GB card defaults to the stable 40GB geometry. To build the explicit
experimental 80GB profile:

```bash
sudo ./install.sh --profile=10gb80
```

This profile is applied only to `10de:2082`; any `10de:20c2` card in the same
machine remains on the stable 64GB geometry. The installer prints an
experimental warning and records the selected profile under
`/lib/modules/$(uname -r)/updates/cmpunlocker/`.

Then perform a cold reboot (full power off, then boot). Before stress testing,
verify that the new module is active and capture a baseline bundle:

```bash
sudo ./verify.sh
sudo ./tools/collect-diagnostics.sh
```

`verify.sh` rejects an installed module that still contains the removed
late-PMA extension. The build installs modules but does not hot-reload them by
default. Do not rely on a warm module reload after replacing an unsafe build;
use a complete power-off/cold boot. `CMPUNLOCKER_ALLOW_HOT_RELOAD=1` exists only
as a developer override and is not valid for stress qualification.

For the first workload after the cold boot:

```bash
sudo ./tools/run-monitored.sh --interval=1 --output=/root/cmp-logs -- \
  python3 your_workload.py
```

## Uninstall

To uninstall cmpunlocker, run the following command:

```bash
sudo ./remove.sh --yes
```

Then perform a cold reboot (full power off, then boot).
