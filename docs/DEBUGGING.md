# Debugging

Before you go asking in the Discord for help, here is a FAQ you should take a look at:

---

## "nvidia-smi: command not found"

- The installer likely didn't run or even failed. Re-run `sudo ./install.sh` and cold reboot.

---

## nvidia-smi shows 8192 or 10240 MiB (not 65536, 40960, or experimental 81920)

- Confirm each PLM reached its logged target. `WPR_CFG` is expected to be `0xfffff0ff`; the other opened PLMs use `0xffffffff`. Run `sudo dmesg | grep SEC2_DEBUG` to inspect the readback.

- If this still persists, refer to the Discord protocol at the end of the document.

---

## Verify the WPR/PMA safety fix before any stress test

Run:

```bash
sudo ./verify.sh
```

The installed core module must not contain `SEC2_DEBUG_LATE_PMA` or
`memmgrSec2DebugLateExtendHighPmaRegion`. The installed safety metadata must
read `wpr-safe-r3`, and the current boot must have no `late PMA extension status`
or `SEC2_DEBUG_LATE_PMA: registering` line. `verify.sh` fails closed if any of
these checks indicate the old module is active.

Useful manual checks:

```bash
strings /lib/modules/$(uname -r)/updates/cmpunlocker/nvidia.ko \
  | grep -E 'SEC2_DEBUG_LATE_PMA|memmgrSec2DebugLateExtendHighPmaRegion'
cat /lib/modules/$(uname -r)/updates/cmpunlocker/safety_revision
sudo dmesg | grep -E 'CMP_MEM_|SEC2_DEBUG|Xid|UVM|GSP'
```

After installation and before a stress run, capture a baseline:

```bash
sudo ./tools/collect-diagnostics.sh
```

For the actual workload, prefer the monitored runner:

```bash
sudo ./tools/run-monitored.sh --interval=1 --output=/root/cmp-logs -- \
  python3 your_workload.py
```

It records current-boot kernel messages, periodic VRAM/temperature/power/clock
telemetry, process state, workload output, and pre/post diagnostic bundles. The
collector wraps `nvidia-smi` in timeouts, so final collection does not wait
indefinitely on an unresponsive GPU.

The safety property is that cmpunlocker no longer performs a late
`pmaRegisterRegion()` or clears reserved-region flags.

---

## Experimental 80GB reports capacity but workloads fail

- Confirm the compiled profile is `10gb80` or `mixed80`:

  ```bash
  cat /lib/modules/$(uname -r)/updates/cmpunlocker/card_profile
  ```

- Confirm coherent register readback is retained in the kernel log:

  ```bash
  sudo dmesg | grep -iE 'CFG1=0x02779000 LMR=0x0*28b.*2082'
  ```

- A successful `nvidia-smi` capacity report is not a memory-stability test. Keep
  the complete Xid/GSP log and return to the stable 40GB profile with:

  ```bash
  sudo ./install.sh --profile=10gb
  sudo shutdown -h now
  ```

---

## PCIe still at Gen1 after install

- Confirm IOMMU passthrough mode is enabled. Depending on your operating system, enabling IOMMU passthrough can vary.

- If this still persists, refer to the Discord protocol at the end of the document.

---

## Discord protocol

If you have tried the above steps and are still having issues, please follow these steps to get help in the [Discord community](https://discord.gg/CdHSakKSFv):

1. Open a ticket in the #issue-support channel.

2. Provide the following information in your ticket:
   - Your operating system and version
   - Your GPU model and driver version
   - The archive produced by `sudo ./tools/collect-diagnostics.sh`
   - Latest install log (if applicable)
