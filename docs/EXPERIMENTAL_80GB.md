# Experimental 10GB → 80GB Profile

## Status

The normal and supported targets remain:

- `10de:20c2`: 8GB → 64GB
- `10de:2082`: 10GB → 40GB

The `10gb80` profile is an explicit research option for `10de:2082`. It is not
a claim that 80GB is stable, and it is never selected by automatic detection.

## Install

```bash
sudo ./install.sh --profile=10gb80
sudo shutdown -h now
```

Perform a full power-off/cold boot. On a mixed system, 20c2 cards retain the
stable 64GB geometry and 2082 cards receive the experimental 80GB geometry.

## Compiled geometry

The profile rewrites the constants in the patched `kernel_gsp.c` that are
actually compiled into `nvidia.ko`:

| PCI device | CFG1 | LMR | GSP framebuffer length |
|---|---:|---:|---:|
| `20c2` | `0x02779000` | `0x0000020B` | `0x0000001000000000` |
| `2082`, stable | `0x02669000` | `0x0000028A` | `0x0000000A00000000` |
| `2082`, experimental | `0x02779000` | `0x0000028B` | `0x0000001400000000` |

`driver/apply_profile.py` performs and verifies this rewrite. Merely editing
`common/constants.yaml` does not alter the compiled driver.

## WPR/PMA safety

This revision removes the unsafe late-PMA extension entirely. It does not trim a
hard-coded top address. Instead, all regions marked reserved by GSP/RM remain
reserved under every geometry and firmware placement.

The current safety state is represented by the installed metadata and absence of
the removed late-PMA path:

```text
safety_revision=wpr-safe-r3
```

There must be no `SEC2_DEBUG_LATE_PMA` or `late PMA extension status` line.
The amount CUDA can allocate is expected to be below the raw 81920 MiB capacity
because WPR and other driver reservations are intentionally excluded.

PMA descriptor coverage is not used as an allocation decision. Do not infer
that a physical range is allocatable merely because it appears in a normal
descriptor or capacity report.

## Verification

```bash
sudo ./verify.sh
sudo dmesg | grep -E 'SEC2_DEBUG|CMP_MEM_'
sudo ./tools/collect-diagnostics.sh
sudo ./tools/run-monitored.sh --interval=1 --output=/root/cmp-logs -- \
  python3 your_workload.py
```

For an experimental 2082 card, retained logs should include a readback similar
to:

```text
CFG1=0x02779000 LMR=0x0000028b (devId=0x2082)
```

`verify.sh` confirms enumeration and the reported capacity target. It does not
prove retention, alias-free operation, repeated CUDA-context stability, or
long-duration workload correctness.

Before using the card for inference or other correctness-sensitive work, test at
least:

1. Dense unique-tag write/readback across the intended allocation range.
2. A deliberate comparison across the 40GiB boundary.
3. Repeated allocate/write/read/free/context-destroy cycles.
4. A long-duration workload while preserving the complete kernel log.

## Recovery

Keep the stable profile available. To return every 2082 card to 40GB:

```bash
sudo ./install.sh --profile=10gb
sudo shutdown -h now
```

If the GPU is lost or driver reload fails, use a full power-off rather than
assuming a warm reboot cleared all GSP/HBM state.

## Implementation boundaries

This revision reuses the 610.43.0x geometry, BAR0/PRAMIN, CE-scrub,
persistent-state, PCIe, and runtime device-ID patches. It explicitly does **not**
reuse the old late-PMA extension. The replacement `memory-layout-safety.patch`
keeps the CE virtual-mode workaround and adds no allocator inspection.

The current safety patch adds no allocator inspection or runtime diagnostic code.

Default 40GB and 64GB geometry constants remain unchanged. The safety fix applies
to both stable and experimental profiles.
