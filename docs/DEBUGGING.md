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
   - The output of `sudo dmesg | grep SEC2_DEBUG`
   - Latest install log (if applicable)
