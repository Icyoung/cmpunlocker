# GR Unlock for CMP 90HX / 70HX

Enable the Graphics (GR) engine — 3D rasterization, display output,
OpenGL / Vulkan / CUDA graphics APIs — on NVIDIA CMP 90HX (`10de:220d`)
and CMP 70HX (`10de:248a`) by patching NVIDIA's closed-source driver
610.43.02 kernel blob.

## Scope

- **Applies to**: CMP 90HX, CMP 70HX (GA102 silicon; same die family
  as RTX 3080 Ti / RTX 3090)
- **Does not apply to**: CMP 170HX (GA100 silicon; no display engine
  in hardware, cannot be software-unlocked)
- **Driver version supported**: NVIDIA 610.43.02 only
  (`NVIDIA-Linux-x86_64-610.43.02.run`)

The main cmpunlocker project (open-driver 170HX unlock) and this tool
are independent driver installs.  They cannot coexist on the same host:
- 170HX: use the open-driver cmpunlocker main path.
- 90HX / 70HX GR unlock: use this tool with the closed 610.43.02
  driver.

## How it works

The stock closed driver refuses to bring up the Graphics engine on
CMP-branded SKUs.  The GR-related property in `pGpu` (offset
`0x4360`, bit `0x1000`) is read/writable through internal accessor
functions but is left cleared for CMP.

This tool patches three sites in `kernel/nvidia/nv-kernel.o_binary`:

| Site | Location | What changes |
|---|---|---|
| 1 | `_nv029797rm+0x4` (7 bytes) | Function prologue redirected to an appended code cave that forces GR-enable bit 12 on before falling through. |
| 2 | `_nv032289rm+0x8d` (6 bytes) | Conditional `je` → unconditional `jmp` so a downstream branch always follows the GR-enabled path. |
| 3 | `_nv031940rm+0x4` (5 bytes) | Entire body replaced with `xor eax,eax; inc eax; ret` — this predicate always returns 1 (GR present). |

A 124-byte code cave is appended to `.text` implementing the read /
OR / write / verify / return-jump sequence.

The patch is functionally identical to dm's *GreenDamTan* reference
build.  Byte content of the three patch sites and the 124-byte code
cave is bit-for-bit equal; we deliberately do not add the five
`nv610_gr_*` symbols to `.symtab` (they are debug decoration, not
required for the runtime behavior), so the output file's MD5 differs
from the reference.

## Usage

```bash
# 1. Download the stock 610.43.02 driver from NVIDIA:
#    https://us.download.nvidia.com/XFree86/Linux-x86_64/610.43.02/NVIDIA-Linux-x86_64-610.43.02.run

# 2. Patch it and stage a patched installer directory:
sudo ./install_gr_unlock.sh /path/to/NVIDIA-Linux-x86_64-610.43.02.run

# 3. Install per the tool's instructions.

# 4. Reboot cleanly.
```

In addition to the three RM byte patches above, `install_gr_unlock.sh`
also flips two source-level defaults in the extracted driver tree so
the patched CPU-side path is the one that actually runs at init:

- `kernel/nvidia/nv-reg.h` — `EnableGpuFirmware` default is changed
  from `NV_REG_ENABLE_GPU_FIRMWARE_DEFAULT_VALUE` (0x12, GSP on for
  GA102) to `NV_REG_ENABLE_GPU_FIRMWARE_MODE_DISABLED` (0).  With
  GSP on, GR-enable is decided by signed GSP-RM ucode on the card
  and the .o_binary patch is a no-op.
- `kernel/nvidia-drm/nvidia-drm-os-interface.c` —
  `nv_drm_modeset_module_param` default is changed from `true` to
  `false`, so nvidia-drm's atomic modeset does not take over the
  init ordering the code cave hooks into.

Both changes are guarded on exact-string matches against the 610.43.02
sources; if NVIDIA ships different wording the installer aborts rather
than silently skipping.

## After install: known caveats

1. **`NVreg_EnableGpuFirmware=0` and `nvidia_drm.modeset=N` are now the
   compile-time defaults.**  Verify after the driver loads:
   ```bash
   cat /sys/module/nvidia/parameters/NVreg_EnableGpuFirmware   # expect 0
   cat /sys/module/nvidia_drm/parameters/modeset               # expect N
   ```
   Either default can still be overridden at load time by a
   kernel-cmdline (`nvidia.NVreg_EnableGpuFirmware=1`,
   `nvidia-drm.modeset=1`) or `/etc/modprobe.d/*` entry.  If a distro
   ships one of those, remove it and rebuild the initramfs; otherwise
   the GR unlock is a no-op.

2. **Do not mix with the main cmpunlocker driver.**
   The main cmpunlocker path uses `nvidia-open` (open-gpu-kernel-modules)
   which does not contain the GR gate we patch here — the closed driver
   is a different codebase.  Uninstall one before installing the other.

3. **Reboot, don't rmmod/modprobe.**
   Like the main cmpunlocker path, this driver's boot-time state matters.

## Files

- `patch_gr_unlock.py` — Python patcher for `nv-kernel.o_binary`.
  Standalone, no dependencies outside the stdlib.  Run with
  `python3 patch_gr_unlock.py <stock.o_binary> <output.o_binary>`.
- `install_gr_unlock.sh` — one-shot wrapper: extracts the given
  `.run`, invokes the patcher on the extracted blob, repacks (or
  stages a directory to install from).

## Credits

The reverse-engineered patch this tool re-implements is *GreenDamTan*
by dm, distributed as a patched `.run`.  This tool packages the same
byte-level change as a source-in-repo patcher, so the origin of every
patched byte is auditable and the toolchain is self-contained.
