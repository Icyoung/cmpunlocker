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

## Verification

```bash
sudo ./verify.sh
sudo dmesg | grep SEC2_DEBUG
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

This port deliberately reuses the existing 610.43.0x unlock path, including the
late-PMA extension, BAR0/PRAMIN clamp, CE scrub workarounds, persistent software
state, and runtime device-ID selection. It changes only the 2082 target geometry
when the experimental profile is explicitly selected. Default 40GB and 64GB
builds remain byte-for-byte equivalent at the geometry-selection layer.
