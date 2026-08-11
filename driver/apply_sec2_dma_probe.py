#!/usr/bin/env python3
"""SEC2 probe v32+v33 — write Patch A into WPR via post-reset host DMA.

Plan: docs/PLAN_SEC2_DMA_POSTPATCH.md §5/§9 (v32, 2026-08-10)

All prerequisites proven this session:
  - post-kflcnReset, host writes to SEC2 regs stick and host-driven DMATRF
    DMA works: sysmem roundtrip (A1) and FB write chain (A2) both verified
    with CPU-readback magic in v23.
  - WPR was closed there only because the 4 enable iowrs hadn't run yet;
    in the reset window the host can write them directly.
  - Patch A block content is known (fwimage offset == vaddr == FB offset
    from gspFwOffset; stock bytes verified locally).

v32 (RMCmpSec2DmaProbe=3, post-BooterLoad hook — GSP-RM already verified
and resident in WPR, not yet started):
  1. kflcnReset(SEC2); FBIF_CTL |= ALLOW_PHYS_NO_CTX; DMACTL = 0
  2. host-write the 4 enable regs (with readback log)
  3. stage the 256B patched block: CPU -> meta+0xC00 -> DMEM 0x1700
     (ctx0, TRANSCFG0 = sysmem)
  4. TRANSCFG0 = LOCAL_FB; DMA DMEM -> FB gspFwOffset+0x1b54600
  5. restore TRANSCFG0; driver proceeds to start patched GSP-RM

Boot log markers: CMP_WPR_WR: ...

v33 (RMCmpSec2DmaProbe=4, RMCmpSec2DmaSec=<0..3> default 1): same hook
and staging as v32, but the DMEM->FB store carries DMATRFCMD.SEC=sec.
A control 256B store to FB+1MiB (v23 A2-proven non-WPR scratch) runs
first to distinguish "SEC value breaks DMA entirely" from "WPR ACL
rejects". Boot log markers: CMP_SEC2_PROBE4: ...

v34 (RMCmpSec2DmaProbe=5, RMCmpSec2DmaSec=<0..3> default 0): HS-triggered
variant. The DMATRFCMD trigger write is executed by the booter
continuation in HS context (the proven PLM-open primitive:
RefillPayload(addr,val) + one BooterLoad run = one arbitrary 32-bit
MMIO write in HS context). Host pre-programs DMATRFBASE/BASE1/MOFFS/
FBOFFS/TRANSCFG after the last kflcnReset; the trigger run uses
kgspExecuteBooterLoadNoReset_TU102 (no kflcnReset, so the host-programmed
DMA regs survive). Patch block rides the signature template at offset
0x1000 -> lands at DMEM 0x1800 (DMATRFMOFFS=0x1800).
  1. RefillPayload(0x84111c, 1) + normal BooterLoad run (HS open of the
     suspected WPR gate), host readback logged
  2. kflcnReset (last), enables (minus 0x84111c), FBIF_CTL|=0x80,
     DMACTL=0, TRANSCFG0=FB, DMATRF* programmed for dst
  3. RefillPayload(0x840118, 0x620|(sec<<2)) + patch block at tmpl 0x1000
  4. kgspExecuteBooterLoadNoReset_TU102 (trigger, no reset)
  5. bounded IDLE poll, state dump, TRANSCFG0 restore
Boot log markers: CMP_SEC2_PROBE5: ...

v35 (RMCmpSec2DmaProbe=6, RMCmpSec2WprCollapseMode=<1|2> default 1):
collapse WPR2 bounds post-BooterLoad, then host DMA Patch A in, then
restore. probe==4 proved host SEC2 DMATRF works to NON-WPR FB; if
NV_PFB_PRI_MMU_WPR2_ADDR_LO/HI (0x1fa824/0x1fa828) are collapsed, the
target becomes ordinary FB. mode 1: LO=0,HI=0; mode 2: LO=0xFFFFF000,
HI=0 (lo>hi = empty). Read-verify (expect stock block) BEFORE writing,
write, read-verify again (expect patched), restore bounds.
Boot log markers: CMP_SEC2_PROBE6: ...

v36 (RMCmpSec2DmaProbe=7): discriminator for "patch delivered but no
effect" (v35). Same WPR2-collapse + host-DMA path as probe==6, but
patches the ".fwversion" string "610.43.02" -> "610.43.99" at ALL 7
fwimage offsets (0x25ea8, 0x822c0, 0xb8be0, 0x189830, 0x1aea48,
0x1d77e8, 0x1bfae2c) instead of Patch A. Per occurrence: 256B-block
RMW with string-level read-verify before (expect stock) and after
(expect .99). .99 visible in /proc/dmesg -> in-place execution proven
(Patch A semantics wrong); still .02 despite verified write -> executed
copy lives elsewhere. Boot log markers: CMP_SEC2_PROBE7: ...

v38 (RMCmpSec2DmaProbe=10): HS gadget exfil smoke test. After the 11-round
PLM open loop (still pre-final BooterLoad), one RefillPayload write targets
SEC2 mailbox1 (0x00840044) with magic 0xdeadbeef, then a theater Booter run.
Host readback of mbox0/mbox1 before/after is logged. match=1 on mbox1 proves
the proven single-MMIO-write primitive can reach a host-readable SEC2
register (necessary but not sufficient for csecret spill). Boot log markers:
CMP_SEC2_PROBE10: ...

v37/v38 (RMCmpSec2DmaProbe=8): runtime argument loggers inside GSP-RM
(gsp_analysis/PATCH_B_RE.md C1+C2, rings relocated in v38 per
PATCH_C_VERIFY.md §5 to GSP-writable pages: C1 file 0x38a500, C2 file
0x38b7c0), delivered via the probe==6 WPR2-collapse channel. Arms:
C1 hook+stub (dmaUpdateVASpace arg logger), C2 hook+stub (dmaAllocMap
logger), each site stock/zero read-verified before writing; ring
windows zero-verified. After boot, a Linux delayed-work
(cmpScheduleRingDump in os-interface.c) re-opens the channel every
30s x8 and dumps both rings as CMP_RING_C1/C2 lines. probe==9 triggers
a one-shot dump (and keeps s_cmpSec2RingDump alive vs --gc-sections).
Boot log markers: CMP_SEC2_PROBE8: ...
"""

from __future__ import annotations

import pathlib
import sys

MARK = "CMP_SEC2_DMA_PROBE"

STUB_OFF = 0x30
IMM32_OFF = 60   # runtime-patched imm32 offset inside STUB (wprPhys+0x800)

STUB = bytes([
    # --- the 4 enable writes (verbatim OS values)
    0x8F, 0x00, 0x02, 0x00,                  # mov r15, 0x200
    0x89, 0x00, 0x84, 0x04,                  # mov r9, 0x48400
    0xF7, 0x9F, 0x00,                        # iowrs I[r9], r15
    0x0F, 0x01,                              # mov r15, 1
    0xB8, 0x99, 0x00, 0x1D, 0x02,            # sub r9, r9, 0x1d00
    0xF7, 0x9F, 0x00,                        # iowrs I[r9], r15
    0x0F, 0x03,                              # mov r15, 3
    0xB8, 0x99, 0x00, 0x01, 0x02,            # sub r9, r9, 0x100
    0xF7, 0x9F, 0x00,                        # iowrs I[r9], r15
    0x0F, 0x01,                              # mov r15, 1
    0xB8, 0x99, 0x00, 0x06, 0x02,            # sub r9, r9, 0x600
    0xF7, 0x9F, 0x00,                        # iowrs I[r9], r15
    # --- xdst setup: xdbase=0, xtargets=0 (port0 = TRANSCFG0 = sysmem now)
    0x01, 0x00,                              # mov r1, 0
    0xFE, 0x17, 0x00,                        # mov $xdbase, r1
    0x01, 0x00,                              # mov r1, 0
    0xFE, 0x1B, 0x00,                        # mov $xtargets, r1
    # --- store magic to DMEM[0x1700]
    0x41, 0x00, 0x17,                        # mov r1, 0x1700
    0x42, 0x7A, 0xDA,                        # mov r2, 0xda7a
    0xA0, 0x12,                              # st  D[r1], r2
    # --- xdst: DMEM[0x1700] -> SYSMEM meta+0x800 (256B)
    0xD1, 0x00, 0x00, 0x00, 0x00,            # mov r1, imm32 (runtime patch)
    0x82, 0x00, 0x17, 0x06,                  # mov r2, (6<<16)|0x1700
    0xFA, 0x12, 0x06,                        # xdst r1, r2
    0xF8, 0x03,                              # xdwait
    # --- continue the normal boot (enables + xdst cfg + lcall 0x100 app)
    0x7E, 0x76, 0x00, 0x00,                  # lcall 0x76
    0xF8, 0x02,                              # exit (unreachable, safety)
])

GATE_LCALL_OFF = 0x27   # 7e 2f 00 00 -> 7e 30 00 00

# probe==8/13 patch bytes — round 15 (v52): bit-35 crash-oracle tripwire.
# If a dmaUpdateVASpace call's vAddr (a5) has bit 35 set (>= 0x800000000),
# the stub spins forever (GSP hang = observable answer). Else prologue
# replay + jump back (v42b-proven skeleton). 32B stub1, 16B stub2
# passthrough. No exfil, no dump train (probe==13).
P8_HOOK1_ORIG = bytes.fromhex("130101d4233881 2a".replace(" ", ""))
P8_HOOK2_ORIG = bytes.fromhex("130101f2233881 0c".replace(" ", ""))
P8_STUB1_STOCK = bytes.fromhex("130101fc2338810223349102233c110223302103233c310113040104b7941f04")
P8_STUB2_STOCK = bytes.fromhex("233004fc630206120337060093070600")

import struct as _struct

def _rv_auipc(rd, imm20): return ((imm20 & 0xfffff) << 12) | (rd << 7) | 0x17
def _rv_jalr(rd, rs1, imm): return ((imm & 0xfff) << 20) | (rs1 << 15) | (rd << 7) | 0x67
def _rv_srli(rd, rs1, sh): return ((sh & 0x3f) << 20) | (rs1 << 15) | (5 << 12) | (rd << 7) | 0x13
def _rv_andi(rd, rs1, imm): return ((imm & 0xfff) << 20) | (rs1 << 15) | (7 << 12) | (rd << 7) | 0x13
def _rv_beqz(rs1, imm):
    i = imm & 0x1fff
    return (((i >> 12) & 1) << 31) | (((i >> 5) & 0x3f) << 25) | (rs1 << 15) | \
           (((i >> 1) & 0xf) << 8) | (((i >> 11) & 1) << 7) | 0x63

def _rv_jump(pc, target):
    delta = target - pc
    imm20 = (delta + 0x800) >> 12
    lo = delta - (imm20 << 12)
    assert -2048 <= lo <= 2047, hex(lo)
    return [_rv_auipc(6, imm20), _rv_jalr(0, 6, lo)]   # t1

# srli t1,a5,35; andi t1,1; beqz +8 (skip hang); jal x0,0 (hang);
# prologue; jump back to hook+8
P8_STUB1 = b"".join(_struct.pack("<I", x) for x in [
    _rv_srli(6, 15, 35),              # t1 = a5 >> 35
    _rv_andi(6, 6, 1),                # t1 &= 1
    _rv_beqz(6, 8),                   # clear -> skip the hang insn
    0x0000006f,                       # jal zero, 0  (infinite loop)
    0xd4010113,                       # addi sp, sp, -0x2c0
    0x2a813823,                       # sd s0, 0x2b0(sp)
] + _rv_jump(0x5026c34 + 24, 0x5027b54 + 8))
assert len(P8_STUB1) == 32
assert len(P8_STUB1_STOCK) == 32

# C2 stub: passthrough at file 0x1026c60
P8_STUB2 = b"".join(_struct.pack("<I", x) for x in
                    [0xf2010113, 0x0c813823] + _rv_jump(0x5026c68, 0x502ccdc + 8))
assert len(P8_STUB2) == 16
P8_HOOK1_NEW = b"".join(_struct.pack("<I", x) for x in _rv_jump(0x5027b54, 0x5026c34))
P8_HOOK2_NEW = b"".join(_struct.pack("<I", x) for x in _rv_jump(0x502ccdc, 0x5026c60))

# probe==18 (v60): R1-window WPR2 scan+rewrite stub. Bytes generated and
# capstone-verified by gsp_analysis/re2/gen_probe60.py (see that file for the
# stub logic, mailbox protocol and slot layout). Trigger: orchestrator
# vtable-call return at link va 0x4e2e0f0 (file 0xe2e0f0), suspected
# post-FWSEC-exec / pre-DEVINIT-consume. Stub home: proven dead function at
# link va 0x5026c34 (file 0x1026c34, 412B cave). Data slots at stub tail are
# patched by the arm code with WPR2 bounds + gspFwOffset.
# NOTE: all-4-byte instructions — compressed instructions trap as illegal
# instruction in the RM app context (v60 round 1, Xid 1 @ stub entry).
# Round 2: mbox1 carries 1MB-scan progress (raw scan VA) while scanning.
R1_STUB_LEN = 404
R1_SLOT_PALO_OFF = 0x17c
R1_SLOT_SPAN_OFF = 0x184
R1_SLOT_FWBASE_OFF = 0x18c
R1_STUB = bytes.fromhex("130101fd233091002334210123383101233c410123305103b7b2380483b282f23703110033876200b702036023205704232207049703000083b9831483ba8300370eadde131e0e02135e0e02130e1e00930270007390827cf322907c6392c20f7390a97c7390ba7cb7020c00938202607390c27cb7020070938212007390927c83b20301b3823241370a0070b382420103e30200b7424c469382f2576318530a3786d4381306b641b786263993868642130f00009307f0ffb70e1000938efeffb3040a0033095a0103e40400630cc40893844400b3f2d4016394020023229704e3e424ff930270007390827c73109e7c630c0f00b702d360b3e2e201232057042322f7046f00c000b702c36023205704833401000339810083390101033a8101833a01021301010363060b001773e0ff6700033b83b509e21773e0ff6700c338b702ba60232057046ff09ffc930270007390827c73109e7cb702bb60232057046ff01ffb23a0d400130f1f00e3d207f6b38744416ff0dff513000000000000000000000000000000000000000000000000000000")
R1_STUB_STOCK = bytes.fromhex("130101fc2338810223349102233c110223302103233c310113040104b7941f0403b784882334e4fc13070000233004fc630206120337060093070600630c0710033686006308061083b507016384051003b707046300071003b7870383b68702130504fc1389000097c01a00e78000581b050500630a0502033784fc83b78488b347f700130700006396071083308103033401038334810203390102833981011301010467800000b7a5f105938505e6130510009710bc00e78080e6130609009305000013050000b79938049790bdffe780404583c709506390070a7300100037a6f1059305600513052000130686e49710bc00e780c0e21306090093050000130560059790bdffe780c04183c709506396070473001000033504fc630e0500832705091b87f7ff2328e5086316070097c01a00e78080a1b715f80513051000938585439710bc00e78080dd130560056ff09ff21305f0016ff01ff2b7b7380483b787f23717110013070730b387e70023a007006ff01ffab7b7380483b787f23717110013070730b387e70023a007006ff0dff4")
R1_HOOK_NEW = bytes.fromhex("17931f00670043b4")
R1_HOOK_ORIG = bytes.fromhex("63100b0283b509e2")

# __V61_BLOCK_BEGIN__
# probe==19 (v61): R1 final-judgement probe ladder (R1_VA_WINDOW.md).
# Per-round stub bytes from gsp_analysis/re2/gen_probe61.py (capstone
# verified); hook/cave shared with probe==18; no host-patched data slots.
# Mailbox: bus 0x110440/444 + spares via [0x438af28]+0x110000.
# CURRENT ROUND: 8
V61_STUB_LEN = 176
V61_STUB = bytes.fromhex("b7b2380483b282f237031100338762003703806123206744b7b33804938383b2130e0020338ec301b70ec005373f2d2d130fdfd283b203009392020293d2020263e4d20163d8020093838300e3e4c3ff6f008000338f0200330f0f00b702010193821210330f5f00b762020423a4e2e7b722080423a0e229b7920b0423a0e2bbb792180423a8e27fb7f21a0423a4e2a1b7721d0423a4e27b63060b001773e0ff6700034483b509e21773e0ff6700c341")
V61_STUB_STOCK = bytes.fromhex("130101fc2338810223349102233c110223302103233c310113040104b7941f0403b784882334e4fc13070000233004fc630206120337060093070600630c0710033686006308061083b507016384051003b707046300071003b7870383b68702130504fc1389000097c01a00e78000581b050500630a0502033784fc83b78488b347f700130700006396071083308103033401038334810203390102833981011301010467800000b7a5f105938505e6")
V61_HOOK_NEW = R1_HOOK_NEW
V61_HOOK_ORIG = R1_HOOK_ORIG
# __V61_BLOCK_END__


def _c_array(b: bytes) -> str:
    return ", ".join(f"0x{x:02x}" for x in b)


GSP_HELPER = r"""
#include "gpu/sec2/kernel_sec2.h"

#define CMP_SEC2_FALCON_DMACTL  0x0084010cU
#define CMP_SEC2_DMATRFBASE     0x00840110U
#define CMP_SEC2_DMATRFMOFFS    0x00840114U
#define CMP_SEC2_DMATRFCMD      0x00840118U
#define CMP_SEC2_DMATRFFBOFFS   0x0084011cU
#define CMP_SEC2_DMATRFBASE1    0x00840128U
#define CMP_SEC2_FBIF_TRANSCFG0 0x00840600U
#define CMP_SEC2_FBIF_CTL       0x00840624U

#define CMP_SEC2_META_SRC_OFF   0xC00U
#define CMP_SEC2_DMEM_A         0x1700U
#define CMP_SEC2_PATCH_BLK_OFF  0x1b54600U
#define CMP_SEC2_FB_SCRATCH     0x100000U   /* v23 A2-proven non-WPR FB target */

#define CMP_SEC2_TRANSCFG_SYSMEM 0x5U
#define CMP_SEC2_TRANSCFG_FB     0x4U

/* 256B block @ fwimage 0x1b54600 with Patch A applied (+0x64: jalr -> li a0,0) */
static const NvU8 s_cmpPatchBlock[256] = {
    0x13, 0x03, 0x10, 0x00, 0x23, 0x38, 0x71, 0x05, 0x23, 0x34, 0x01, 0x04, 0x23, 0x30, 0x01, 0x04,
    0x23, 0x3c, 0x51, 0x03, 0x23, 0x38, 0x51, 0x03, 0x23, 0x34, 0x01, 0x02, 0x23, 0x30, 0x61, 0x02,
    0x23, 0x3c, 0x61, 0x00, 0x23, 0x38, 0x01, 0x00, 0x23, 0x34, 0x81, 0x01, 0x23, 0x30, 0x91, 0x01,
    0xb3, 0x07, 0xfe, 0x00, 0x83, 0x38, 0x84, 0xef, 0x03, 0x36, 0x84, 0xf0, 0x83, 0x35, 0x84, 0xf1,
    0x03, 0x35, 0x04, 0xf1, 0x13, 0x88, 0xf7, 0xff, 0x33, 0x08, 0x48, 0x01, 0x13, 0x07, 0x00, 0x00,
    0x93, 0x86, 0x04, 0x00, 0x23, 0x34, 0xc4, 0xf3, 0x23, 0x22, 0x64, 0xf6, 0x23, 0x3c, 0xa4, 0xf5,
    0x97, 0x30, 0x4d, 0xff, 0x13, 0x05, 0x00, 0x00, 0x33, 0x09, 0x49, 0x01, 0x9b, 0x07, 0x05, 0x00,
    0xe3, 0x62, 0x69, 0xf7, 0x63, 0x90, 0x07, 0x08, 0x03, 0x37, 0x84, 0xf8, 0x83, 0xb7, 0x8d, 0x88,
    0xb3, 0x47, 0xf7, 0x00, 0x13, 0x07, 0x00, 0x00, 0x63, 0x96, 0x07, 0x1e, 0x03, 0x35, 0x04, 0xef,
    0x83, 0x30, 0x81, 0x16, 0x03, 0x34, 0x01, 0x16, 0x83, 0x34, 0x81, 0x15, 0x03, 0x39, 0x01, 0x15,
    0x83, 0x39, 0x81, 0x14, 0x03, 0x3a, 0x01, 0x14, 0x83, 0x3a, 0x81, 0x13, 0x03, 0x3b, 0x01, 0x13,
    0x83, 0x3b, 0x81, 0x12, 0x03, 0x3c, 0x01, 0x12, 0x83, 0x3c, 0x81, 0x11, 0x03, 0x3d, 0x01, 0x11,
    0x83, 0x3d, 0x81, 0x10, 0x13, 0x01, 0x01, 0x17, 0x67, 0x80, 0x00, 0x00, 0x13, 0x0a, 0x0b, 0x00,
    0x6f, 0xf0, 0xdf, 0xe7, 0xb7, 0xb7, 0x00, 0x00, 0xb3, 0x87, 0xf9, 0x00, 0x83, 0xa7, 0x07, 0xb5,
    0x23, 0x2c, 0xf4, 0xf2, 0x6f, 0xf0, 0x9f, 0xec, 0x93, 0x07, 0x60, 0x05, 0x23, 0x38, 0xf4, 0xee,
    0x6f, 0xf0, 0x9f, 0xf8, 0x93, 0x95, 0x07, 0x02, 0x37, 0xc6, 0x09, 0x06, 0x13, 0x06, 0x06, 0x05,
};

static NV_STATUS
s_cmpSec2PollIdle(OBJGPU *pGpu)
{
    NvU32 t;
    for (t = 0; t < 1000000U; t++)
    {
        if (GPU_REG_RD32(pGpu, CMP_SEC2_DMATRFCMD) & 0x2U)
            return NV_OK;
    }
    return NV_ERR_TIMEOUT;
}

static NV_STATUS
s_cmpSec2HostDma256(OBJGPU *pGpu, NvU64 extAddr, NvU32 dmemOff, NvU32 cmd)
{
    NvU32 t = 0;
    while (GPU_REG_RD32(pGpu, CMP_SEC2_DMATRFCMD) & 0x1U)
    {
        if (++t > 1000000U)
            return NV_ERR_TIMEOUT;
    }
    GPU_REG_WR32(pGpu, CMP_SEC2_DMATRFBASE,
                 (NvU32)((extAddr >> 8) & 0xFFFFFFFFU));
    GPU_REG_WR32(pGpu, CMP_SEC2_DMATRFBASE1,
                 (NvU32)((extAddr >> 40) & 0x1FFU));
    GPU_REG_WR32(pGpu, CMP_SEC2_DMATRFMOFFS, dmemOff);
    GPU_REG_WR32(pGpu, CMP_SEC2_DMATRFFBOFFS, 0);
    GPU_REG_WR32(pGpu, CMP_SEC2_DMATRFCMD, cmd);
    return s_cmpSec2PollIdle(pGpu);
}

/*
 * probe==4: same window as probe==3, but the DMEM->FB store carries
 * DMATRFCMD.SEC=RMCmpSec2DmaSec (default 1). Control transfer to a
 * known-good non-WPR FB scratch (FB+1MiB, v23 A2) runs first so a
 * WPR failure can be attributed to the WPR ACL, not to the SEC value.
 */
static void
s_cmpSec2DmaProbe4
(
    OBJGPU *pGpu,
    KernelGsp *pKernelGsp
)
{
    NvU32 sec = 1;
    KernelSec2 *pKernelSec2;
    NvU64 wprPhys;
    NvU64 dst;
    volatile NvU32 *pMeta;
    NV_STATUS rst, st1, stCtl, stWpr;
    NvU32 tcEntry, cmdAfterCtl, cmdAfterWpr;

    if ((pKernelGsp->pWprMeta == NULL) ||
        (pKernelGsp->pWprMetaDescriptor == NULL))
        return;

    pKernelSec2 = GPU_GET_KERNEL_SEC2(pGpu);
    if (pKernelSec2 == NULL)
        return;

    (void)osReadRegistryDword(pGpu, "RMCmpSec2DmaSec", &sec);
    sec &= 3U;

    wprPhys = memdescGetPhysAddr(pKernelGsp->pWprMetaDescriptor, AT_GPU, 0);
    pMeta   = (volatile NvU32 *)pKernelGsp->pWprMeta;
    dst     = pKernelGsp->pWprMeta->gspFwOffset + CMP_SEC2_PATCH_BLK_OFF;

    rst = kflcnReset_HAL(pGpu, staticCast(pKernelSec2, KernelFalcon));

    GPU_REG_WR32(pGpu, 0x00841210U, 0x200U);
    GPU_REG_WR32(pGpu, 0x00841198U, 3U);
    GPU_REG_WR32(pGpu, 0x00841180U, 1U);
    GPU_REG_WR32(pGpu, 0x0084111cU, 1U);
    NV_PRINTF(LEVEL_ERROR,
              "CMP_SEC2_PROBE4: rst=0x%x en=%08x/%08x/%08x/%08x\n",
              rst,
              GPU_REG_RD32(pGpu, 0x00841210U), GPU_REG_RD32(pGpu, 0x00841198U),
              GPU_REG_RD32(pGpu, 0x00841180U), GPU_REG_RD32(pGpu, 0x0084111cU));

    GPU_REG_WR32(pGpu, CMP_SEC2_FBIF_CTL,
                 GPU_REG_RD32(pGpu, CMP_SEC2_FBIF_CTL) | 0x80U);
    GPU_REG_WR32(pGpu, CMP_SEC2_FALCON_DMACTL, 0);
    tcEntry = GPU_REG_RD32(pGpu, CMP_SEC2_FBIF_TRANSCFG0);

    portMemCopy((void *)(pMeta + CMP_SEC2_META_SRC_OFF / 4), 256,
                s_cmpPatchBlock, 256);
    GPU_REG_WR32(pGpu, CMP_SEC2_FBIF_TRANSCFG0, CMP_SEC2_TRANSCFG_SYSMEM);
    st1 = s_cmpSec2HostDma256(pGpu, wprPhys + CMP_SEC2_META_SRC_OFF,
                              CMP_SEC2_DMEM_A, 0x600U);
    NV_PRINTF(LEVEL_ERROR, "CMP_SEC2_PROBE4: stage st=0x%x\n", st1);

    GPU_REG_WR32(pGpu, CMP_SEC2_FBIF_TRANSCFG0, CMP_SEC2_TRANSCFG_FB);
    stCtl = s_cmpSec2HostDma256(pGpu, (NvU64)CMP_SEC2_FB_SCRATCH,
                                CMP_SEC2_DMEM_A, 0x620U | (sec << 2));
    cmdAfterCtl = GPU_REG_RD32(pGpu, CMP_SEC2_DMATRFCMD);
    NV_PRINTF(LEVEL_ERROR,
              "CMP_SEC2_PROBE4: control st=0x%x cmd=%08x fb=0x%x\n",
              stCtl, cmdAfterCtl, CMP_SEC2_FB_SCRATCH);

    stWpr = s_cmpSec2HostDma256(pGpu, dst, CMP_SEC2_DMEM_A,
                                0x620U | (sec << 2));
    cmdAfterWpr = GPU_REG_RD32(pGpu, CMP_SEC2_DMATRFCMD);
    NV_PRINTF(LEVEL_ERROR,
              "CMP_SEC2_PROBE4: wpr st=0x%x cmd=%08x dst=%llx\n",
              stWpr, cmdAfterWpr, dst);

    GPU_REG_WR32(pGpu, CMP_SEC2_FBIF_TRANSCFG0, tcEntry);
    NV_PRINTF(LEVEL_ERROR,
              "CMP_SEC2_PROBE4: sec=%u control_st=0x%x wpr_st=0x%x\n",
              sec, stCtl, stWpr);
}

/* ---- probe==10: HS gadget -> SEC2 mbox1 exfil smoke test ---- */
#define CMP_SEC2_MAILBOX0          0x00840040U
#define CMP_SEC2_MAILBOX1          0x00840044U
#define CMP_SEC2_MBOX_EXFIL_MAGIC  0xdeadbeefU

static void
s_cmpSec2DmaProbe10MboxExfil
(
    OBJGPU *pGpu,
    KernelGsp *pKernelGsp
)
{
    NvU32 probe = 0;
    NvU32 gspDevId;
    NV_STATUS stRefill, stBoot;
    NvU32 mbox0Before, mbox1Before, mbox0After, mbox1After;

    gspDevId = pGpu->idInfo.PCIDeviceID >> 16;
    if (gspDevId != 0x2082)
        return;

    (void)osReadRegistryDword(pGpu, "RMCmpSec2DmaProbe", &probe);
    if (probe != 10)
        return;

    mbox0Before = GPU_REG_RD32(pGpu, CMP_SEC2_MAILBOX0);
    mbox1Before = GPU_REG_RD32(pGpu, CMP_SEC2_MAILBOX1);

    stRefill = kgspSec2PostblTimingRefillPayload(pGpu, pKernelGsp,
                    CMP_SEC2_MAILBOX1, CMP_SEC2_MBOX_EXFIL_MAGIC);
    if (stRefill != NV_OK)
    {
        NV_PRINTF(LEVEL_ERROR,
                  "CMP_SEC2_PROBE10: refill st=0x%x mbox1_before=%08x\n",
                  stRefill, mbox1Before);
        return;
    }

    stBoot = kgspExecuteBooterLoad_HAL(pGpu, pKernelGsp,
                memdescGetPhysAddr(pKernelGsp->pWprMetaDescriptor, AT_GPU, 0));

    mbox0After = GPU_REG_RD32(pGpu, CMP_SEC2_MAILBOX0);
    mbox1After = GPU_REG_RD32(pGpu, CMP_SEC2_MAILBOX1);

    NV_PRINTF(LEVEL_ERROR,
              "CMP_SEC2_PROBE10: boot=0x%x refill=0x%x "
              "mbox0 %08x->%08x mbox1 %08x->%08x want=%08x match=%u\n",
              stBoot, stRefill,
              mbox0Before, mbox0After,
              mbox1Before, mbox1After,
              CMP_SEC2_MBOX_EXFIL_MAGIC,
              (mbox1After == CMP_SEC2_MBOX_EXFIL_MAGIC) ? 1U : 0U);
}

/*
 * probe==14 (v53): can the SEC2 HS (LEVEL2) payload write FBPA config
 * registers? Three minimal-disturbance writes via the proven
 * refill+BooterLoad primitive, with immediate host readback after each
 * run. DevInit/geometry writes run later, so immediate-vs-final splits
 * "reverted later" from "never accepted".
 */
static void
s_cmpSec2DmaProbe14Fbpa
(
    OBJGPU *pGpu,
    KernelGsp *pKernelGsp
)
{
    NvU32 probe = 0;
    NvU32 gspDevId;
    NvU32 w2lo, w2hi;
    NvU32 i;

    static const struct { NvU32 addr; NvU32 val; } s_wr[3] = {
        { 0x009a0250U, 0x0bb800b1U },   /* orig 0x0bb800a1 (bit4 only) */
        { 0x009a016cU, 0x00000010U },   /* orig 0x14 */
        { 0x009a0294U, 0x3926c525U },   /* orig 0x38d4841b */
    };

    gspDevId = pGpu->idInfo.PCIDeviceID >> 16;
    if (gspDevId != 0x2082)
        return;
    (void)osReadRegistryDword(pGpu, "RMCmpSec2DmaProbe", &probe);
    if (probe == 16)
    {
        /*
         * v56: combo write — all 8 A100-p10 targets in ONE boot (0x9a0294
         * skipped: writing it at this hook kills the final BooterLoad even
         * with a same-value write). Post-boot readback happens host-side.
         */
        static const struct { NvU32 addr; NvU32 val; } s_combo[8] = {
            { 0x009a0298U, 0x881b0b11U },
            { 0x009a0254U, 0x01cab04aU },
            { 0x009a029cU, 0x2400218aU },
            { 0x009a0290U, 0x1861a048U },
            { 0x009a0224U, 0x12040d12U },
            { 0x009a0248U, 0x0a147444U },
            { 0x009a0250U, 0x0bb800c1U },
            { 0x009a03e4U, 0x00000004U },
        };
        NvU32 w2lo, w2hi;
        NvU32 i;

        if (pKernelGsp->pWprMetaDescriptor == NULL)
            return;
        w2lo = GPU_REG_RD32(pGpu, 0x001fa824U);
        w2hi = GPU_REG_RD32(pGpu, 0x001fa828U);

        for (i = 0; i < 8U; i++)
        {
            NvU32 orig, imm;
            NV_STATUS stR, stB;

            orig = GPU_REG_RD32(pGpu, s_combo[i].addr);
            GPU_REG_WR32(pGpu, 0x001fa824U, w2lo);
            GPU_REG_WR32(pGpu, 0x001fa828U, w2hi);
            stR = kgspSec2PostblTimingRefillPayload(pGpu, pKernelGsp,
                                                    s_combo[i].addr,
                                                    s_combo[i].val);
            stB = kgspExecuteBooterLoad_HAL(pGpu, pKernelGsp,
                    memdescGetPhysAddr(pKernelGsp->pWprMetaDescriptor,
                                       AT_GPU, 0));
            imm = GPU_REG_RD32(pGpu, s_combo[i].addr);
            NV_PRINTF(LEVEL_ERROR,
                      "CMP_FBPA_HS: [p16] addr=%08x orig=%08x target=%08x "
                      "refill=0x%x boot=0x%x imm_rb=%08x\n",
                      s_combo[i].addr, orig, s_combo[i].val, stR, stB, imm);
        }
        GPU_REG_WR32(pGpu, 0x001fa824U, w2lo);
        GPU_REG_WR32(pGpu, 0x001fa828U, w2hi);
        return;
    }
    if (probe == 15)
    {
        /*
         * v54: regkey-driven single HS write (one FLR boot per candidate).
         * RMCmpFbpaAddr / RMCmpFbpaVal (hex). Same primitive + immediate
         * host readback as probe==14.
         */
        NvU32 addr = 0, val = 0;
        NvU32 orig, imm;
        NV_STATUS stR, stB;

        (void)osReadRegistryDword(pGpu, "RMCmpFbpaAddr", &addr);
        (void)osReadRegistryDword(pGpu, "RMCmpFbpaVal", &val);
        if (addr == 0)
            return;
        if (pKernelGsp->pWprMetaDescriptor == NULL)
            return;

        w2lo = GPU_REG_RD32(pGpu, 0x001fa824U);
        w2hi = GPU_REG_RD32(pGpu, 0x001fa828U);

        orig = GPU_REG_RD32(pGpu, addr);
        GPU_REG_WR32(pGpu, 0x001fa824U, w2lo);
        GPU_REG_WR32(pGpu, 0x001fa828U, w2hi);
        stR = kgspSec2PostblTimingRefillPayload(pGpu, pKernelGsp, addr, val);
        stB = kgspExecuteBooterLoad_HAL(pGpu, pKernelGsp,
                memdescGetPhysAddr(pKernelGsp->pWprMetaDescriptor, AT_GPU, 0));
        imm = GPU_REG_RD32(pGpu, addr);
        GPU_REG_WR32(pGpu, 0x001fa824U, w2lo);
        GPU_REG_WR32(pGpu, 0x001fa828U, w2hi);
        NV_PRINTF(LEVEL_ERROR,
                  "CMP_FBPA_HS: [p15] addr=%08x orig=%08x target=%08x "
                  "refill=0x%x boot=0x%x imm_rb=%08x\n",
                  addr, orig, val, stR, stB, imm);
        return;
    }
    if (probe != 14)
        return;
    if (pKernelGsp->pWprMetaDescriptor == NULL)
        return;

    w2lo = GPU_REG_RD32(pGpu, 0x001fa824U);
    w2hi = GPU_REG_RD32(pGpu, 0x001fa828U);

    for (i = 0; i < 3U; i++)
    {
        NvU32 orig = GPU_REG_RD32(pGpu, s_wr[i].addr);
        NV_STATUS stR, stB;
        NvU32 imm;

        GPU_REG_WR32(pGpu, 0x001fa824U, w2lo);
        GPU_REG_WR32(pGpu, 0x001fa828U, w2hi);
        stR = kgspSec2PostblTimingRefillPayload(pGpu, pKernelGsp,
                                                s_wr[i].addr, s_wr[i].val);
        stB = kgspExecuteBooterLoad_HAL(pGpu, pKernelGsp,
                memdescGetPhysAddr(pKernelGsp->pWprMetaDescriptor, AT_GPU, 0));
        imm = GPU_REG_RD32(pGpu, s_wr[i].addr);
        NV_PRINTF(LEVEL_ERROR,
                  "CMP_FBPA_HS: [%u] addr=%08x orig=%08x target=%08x "
                  "refill=0x%x boot=0x%x imm_rb=%08x\n",
                  i, s_wr[i].addr, orig, s_wr[i].val, stR, stB, imm);
    }

    GPU_REG_WR32(pGpu, 0x001fa824U, w2lo);
    GPU_REG_WR32(pGpu, 0x001fa828U, w2hi);
    NV_PRINTF(LEVEL_ERROR, "CMP_FBPA_HS: done (wpr2 rb %08x/%08x)\n",
              GPU_REG_RD32(pGpu, 0x001fa824U),
              GPU_REG_RD32(pGpu, 0x001fa828U));
}

/* ---- probe==5: HS-triggered DMA write of Patch A into WPR ---- */
NV_STATUS kgspExecuteBooterLoadNoReset_TU102(OBJGPU *pGpu,
                                             KernelGsp *pKernelGsp,
                                             const NvU64 sysmemAddrOfData);

#define CMP_SEC2_SIG_TEMPLATE_SIZE 0xf800U
#define CMP_SEC2_TMPL_PATCH_OFF    0x1000U  /* template+X -> DMEM 0x800+X */
#define CMP_SEC2_DMEM_B            0x1800U
#define CMP_SEC2_WPR2_LO           0x001fa824U
#define CMP_SEC2_WPR2_HI           0x001fa828U

static void
s_cmpSec2DmaProbe5
(
    OBJGPU *pGpu,
    KernelGsp *pKernelGsp
)
{
    NvU32 sec = 0;
    KernelSec2 *pKernelSec2;
    NvU64 dst;
    NvU64 metaPhys;
    NvU32 tcEntry, wpr2Lo, wpr2Hi;
    NV_STATUS st, stRefill, stBoot, stTrig;
    NvU32 reg111c, mbox0After, cmdAfter;
    NvU8 *pSigVa;
    NvU32 t;

    if ((pKernelGsp->pWprMeta == NULL) ||
        (pKernelGsp->pWprMetaDescriptor == NULL) ||
        (pKernelGsp->pSignatureMemdesc == NULL))
        return;

    pKernelSec2 = GPU_GET_KERNEL_SEC2(pGpu);
    if (pKernelSec2 == NULL)
        return;

    (void)osReadRegistryDword(pGpu, "RMCmpSec2DmaSec", &sec);
    sec &= 3U;

    dst      = pKernelGsp->pWprMeta->gspFwOffset + CMP_SEC2_PATCH_BLK_OFF;
    metaPhys = memdescGetPhysAddr(pKernelGsp->pWprMetaDescriptor, AT_GPU, 0);
    wpr2Lo   = GPU_REG_RD32(pGpu, CMP_SEC2_WPR2_LO);
    wpr2Hi   = GPU_REG_RD32(pGpu, CMP_SEC2_WPR2_HI);

    /*
     * step 0: the stock signature memdesc is only one page; the exploit
     * template needs the full 0xf800 buffer (same size the PLM loop used).
     */
    memdescFree(pKernelGsp->pSignatureMemdesc);
    memdescDestroy(pKernelGsp->pSignatureMemdesc);
    pKernelGsp->pSignatureMemdesc = NULL;
    st = memdescCreate(&pKernelGsp->pSignatureMemdesc, pGpu,
                       CMP_SEC2_SIG_TEMPLATE_SIZE, 256,
                       NV_TRUE, ADDR_SYSMEM, NV_MEMORY_CACHED,
                       MEMDESC_FLAGS_NONE);
    if (st == NV_OK)
    {
        memdescTagAlloc(st, NV_FB_ALLOC_RM_INTERNAL_OWNER_UNNAMED_TAG_16,
                        pKernelGsp->pSignatureMemdesc);
    }
    if (st != NV_OK)
    {
        NV_PRINTF(LEVEL_ERROR,
                  "CMP_SEC2_PROBE5: template memdesc alloc failed 0x%x\n", st);
        return;
    }

    /* step 1: HS write 0x84111c <- 1 via a normal (reset-ful) run */
    GPU_REG_WR32(pGpu, CMP_SEC2_WPR2_LO, wpr2Lo);
    GPU_REG_WR32(pGpu, CMP_SEC2_WPR2_HI, wpr2Hi);
    stRefill = kgspSec2PostblTimingRefillPayload(pGpu, pKernelGsp,
                                                 0x0084111cU, 1U);
    stBoot   = kgspExecuteBooterLoad_HAL(pGpu, pKernelGsp, metaPhys);
    reg111c  = GPU_REG_RD32(pGpu, 0x0084111cU);
    NV_PRINTF(LEVEL_ERROR,
              "CMP_SEC2_PROBE5: step1 refill=0x%x boot=0x%x reg84111c=%08x\n",
              stRefill, stBoot, reg111c);

    /* step 3: last reset, then host pre-programs the DMA engine */
    st = kflcnReset_HAL(pGpu, staticCast(pKernelSec2, KernelFalcon));
    GPU_REG_WR32(pGpu, 0x00841210U, 0x200U);
    GPU_REG_WR32(pGpu, 0x00841198U, 3U);
    GPU_REG_WR32(pGpu, 0x00841180U, 1U);
    GPU_REG_WR32(pGpu, CMP_SEC2_FBIF_CTL,
                 GPU_REG_RD32(pGpu, CMP_SEC2_FBIF_CTL) | 0x80U);
    GPU_REG_WR32(pGpu, CMP_SEC2_FALCON_DMACTL, 0);
    tcEntry = GPU_REG_RD32(pGpu, CMP_SEC2_FBIF_TRANSCFG0);
    GPU_REG_WR32(pGpu, CMP_SEC2_FBIF_TRANSCFG0, CMP_SEC2_TRANSCFG_FB);
    GPU_REG_WR32(pGpu, CMP_SEC2_DMATRFBASE,
                 (NvU32)((dst >> 8) & 0xFFFFFFFFU));
    GPU_REG_WR32(pGpu, CMP_SEC2_DMATRFBASE1,
                 (NvU32)((dst >> 40) & 0x1FFU));
    GPU_REG_WR32(pGpu, CMP_SEC2_DMATRFMOFFS, CMP_SEC2_DMEM_B);
    GPU_REG_WR32(pGpu, CMP_SEC2_DMATRFFBOFFS, 0);
    NV_PRINTF(LEVEL_ERROR,
              "CMP_SEC2_PROBE5: step3 rst=0x%x en=%08x/%08x/%08x "
              "tc=%08x->%08x base=%08x base1=%08x moffs=%08x fboffs=%08x "
              "dst=%llx\n",
              st,
              GPU_REG_RD32(pGpu, 0x00841210U), GPU_REG_RD32(pGpu, 0x00841198U),
              GPU_REG_RD32(pGpu, 0x00841180U),
              tcEntry, GPU_REG_RD32(pGpu, CMP_SEC2_FBIF_TRANSCFG0),
              GPU_REG_RD32(pGpu, CMP_SEC2_DMATRFBASE),
              GPU_REG_RD32(pGpu, CMP_SEC2_DMATRFBASE1),
              GPU_REG_RD32(pGpu, CMP_SEC2_DMATRFMOFFS),
              GPU_REG_RD32(pGpu, CMP_SEC2_DMATRFFBOFFS),
              dst);

    /* step 4: HS trigger write DMATRFCMD; patch block rides tmpl 0x1000 */
    stRefill = kgspSec2PostblTimingRefillPayload(pGpu, pKernelGsp,
                    CMP_SEC2_DMATRFCMD, 0x620U | (sec << 2));
    pSigVa = memdescMapInternal(pGpu, pKernelGsp->pSignatureMemdesc,
                                TRANSFER_FLAGS_NONE);
    if (pSigVa != NULL)
    {
        portMemCopy(pSigVa + CMP_SEC2_TMPL_PATCH_OFF, 256,
                    s_cmpPatchBlock, 256);
        memdescUnmapInternal(pGpu, pKernelGsp->pSignatureMemdesc, 0);
        memdescFlushCpuCaches(pGpu, pKernelGsp->pSignatureMemdesc);
    }
    NV_PRINTF(LEVEL_ERROR,
              "CMP_SEC2_PROBE5: step4 refill=0x%x sigva=%s cmd=%08x\n",
              stRefill, (pSigVa != NULL) ? "ok" : "NULL", 0x620U | (sec << 2));

    GPU_REG_WR32(pGpu, CMP_SEC2_WPR2_LO, wpr2Lo);
    GPU_REG_WR32(pGpu, CMP_SEC2_WPR2_HI, wpr2Hi);
    stTrig = kgspExecuteBooterLoadNoReset_TU102(pGpu, pKernelGsp, metaPhys);
    mbox0After = GPU_REG_RD32(pGpu, CMP_SEC2_MAILBOX0);

    /* step 5: bounded idle poll + post-run engine state */
    cmdAfter = 0;
    for (t = 0; t < 1000000U; t++)
    {
        cmdAfter = GPU_REG_RD32(pGpu, CMP_SEC2_DMATRFCMD);
        if (cmdAfter & 0x2U)
            break;
    }
    NV_PRINTF(LEVEL_ERROR,
              "CMP_SEC2_PROBE5: post base=%08x base1=%08x moffs=%08x "
              "fboffs=%08x tc=%08x ctl=%08x\n",
              GPU_REG_RD32(pGpu, CMP_SEC2_DMATRFBASE),
              GPU_REG_RD32(pGpu, CMP_SEC2_DMATRFBASE1),
              GPU_REG_RD32(pGpu, CMP_SEC2_DMATRFMOFFS),
              GPU_REG_RD32(pGpu, CMP_SEC2_DMATRFFBOFFS),
              GPU_REG_RD32(pGpu, CMP_SEC2_FBIF_TRANSCFG0),
              GPU_REG_RD32(pGpu, CMP_SEC2_FBIF_CTL));

    GPU_REG_WR32(pGpu, CMP_SEC2_FBIF_TRANSCFG0, tcEntry);
    GPU_REG_WR32(pGpu, CMP_SEC2_WPR2_LO, wpr2Lo);
    GPU_REG_WR32(pGpu, CMP_SEC2_WPR2_HI, wpr2Hi);
    NV_PRINTF(LEVEL_ERROR,
              "CMP_SEC2_PROBE5: sec=%u step1_111c=%08x trig_st=0x%x "
              "mbox=%08x idle_cmd=%08x dst=%llx\n",
              sec, reg111c, stTrig, mbox0After, cmdAfter, dst);
}

/* ---- probe==6: collapse WPR2, host DMA Patch A into WPR, restore ---- */
static NvU32
s_cmpSec2Memcmp256(const volatile NvU8 *pA, const NvU8 *pB)
{
    NvU32 i;
    for (i = 0; i < 256U; i++)
    {
        if (pA[i] != pB[i])
            return 0;
    }
    return 1;
}

/* FB 256B -> DMEM_A -> sysmem scratch; returns engine status */
static NV_STATUS
s_cmpSec2ReadFb256(OBJGPU *pGpu, NvU64 fbAddr, NvU64 scratchPhys)
{
    NV_STATUS st;

    GPU_REG_WR32(pGpu, CMP_SEC2_FBIF_TRANSCFG0, CMP_SEC2_TRANSCFG_FB);
    st = s_cmpSec2HostDma256(pGpu, fbAddr, CMP_SEC2_DMEM_A, 0x600U);
    if (st != NV_OK)
        return st;
    GPU_REG_WR32(pGpu, CMP_SEC2_FBIF_TRANSCFG0, CMP_SEC2_TRANSCFG_SYSMEM);
    return s_cmpSec2HostDma256(pGpu, scratchPhys, CMP_SEC2_DMEM_A, 0x620U);
}

static void
s_cmpSec2DmaProbe6
(
    OBJGPU *pGpu,
    KernelGsp *pKernelGsp
)
{
    NvU32 mode = 1;
    KernelSec2 *pKernelSec2;
    NvU64 dst;
    NvU64 wprPhys;
    volatile NvU32 *pMeta;
    const volatile NvU8 *pRd;
    NV_STATUS rst, stR1, stS, stW, stR2;
    NvU32 lo0, hi0, loRb, hiRb, loRs, hiRs, tcEntry;
    NvU32 match1 = 0, match2 = 0;
    NvU8 stockBlk[256];

    if ((pKernelGsp->pWprMeta == NULL) ||
        (pKernelGsp->pWprMetaDescriptor == NULL))
        return;

    pKernelSec2 = GPU_GET_KERNEL_SEC2(pGpu);
    if (pKernelSec2 == NULL)
        return;

    (void)osReadRegistryDword(pGpu, "RMCmpSec2WprCollapseMode", &mode);

    wprPhys = memdescGetPhysAddr(pKernelGsp->pWprMetaDescriptor, AT_GPU, 0);
    pMeta   = (volatile NvU32 *)pKernelGsp->pWprMeta;
    dst     = pKernelGsp->pWprMeta->gspFwOffset + CMP_SEC2_PATCH_BLK_OFF;
    pRd     = (const volatile NvU8 *)(pMeta + CMP_SEC2_META_SRC_OFF / 4);

    /* stock block = patch block with the 4 Patch A bytes at +0x64 reverted */
    portMemCopy(stockBlk, 256, s_cmpPatchBlock, 256);
    stockBlk[0x64] = 0xe7;
    stockBlk[0x65] = 0x80;
    stockBlk[0x66] = 0x40;
    stockBlk[0x67] = 0x4f;

    /* step 1+2: save WPR2 bounds, then collapse */
    lo0 = GPU_REG_RD32(pGpu, CMP_SEC2_WPR2_LO);
    hi0 = GPU_REG_RD32(pGpu, CMP_SEC2_WPR2_HI);
    if (mode == 2)
    {
        GPU_REG_WR32(pGpu, CMP_SEC2_WPR2_LO, 0xFFFFF000U);
        GPU_REG_WR32(pGpu, CMP_SEC2_WPR2_HI, 0U);
    }
    else
    {
        GPU_REG_WR32(pGpu, CMP_SEC2_WPR2_LO, 0U);
        GPU_REG_WR32(pGpu, CMP_SEC2_WPR2_HI, 0U);
    }
    loRb = GPU_REG_RD32(pGpu, CMP_SEC2_WPR2_LO);
    hiRb = GPU_REG_RD32(pGpu, CMP_SEC2_WPR2_HI);
    NV_PRINTF(LEVEL_ERROR,
              "CMP_SEC2_PROBE6: mode=%u wpr2 %08x/%08x -> rb %08x/%08x dst=%llx\n",
              mode, lo0, hi0, loRb, hiRb, dst);

    /* step 3: probe==4 preamble */
    rst = kflcnReset_HAL(pGpu, staticCast(pKernelSec2, KernelFalcon));
    GPU_REG_WR32(pGpu, 0x00841210U, 0x200U);
    GPU_REG_WR32(pGpu, 0x00841198U, 3U);
    GPU_REG_WR32(pGpu, 0x00841180U, 1U);
    GPU_REG_WR32(pGpu, 0x0084111cU, 1U);
    GPU_REG_WR32(pGpu, CMP_SEC2_FBIF_CTL,
                 GPU_REG_RD32(pGpu, CMP_SEC2_FBIF_CTL) | 0x80U);
    GPU_REG_WR32(pGpu, CMP_SEC2_FALCON_DMACTL, 0);
    tcEntry = GPU_REG_RD32(pGpu, CMP_SEC2_FBIF_TRANSCFG0);
    NV_PRINTF(LEVEL_ERROR,
              "CMP_SEC2_PROBE6: rst=0x%x en=%08x/%08x/%08x/%08x "
              "wpr2_after_reset=%08x/%08x\n",
              rst,
              GPU_REG_RD32(pGpu, 0x00841210U), GPU_REG_RD32(pGpu, 0x00841198U),
              GPU_REG_RD32(pGpu, 0x00841180U), GPU_REG_RD32(pGpu, 0x0084111cU),
              GPU_REG_RD32(pGpu, CMP_SEC2_WPR2_LO),
              GPU_REG_RD32(pGpu, CMP_SEC2_WPR2_HI));

    /* step 4: read-verify, expect STOCK block */
    stR1 = s_cmpSec2ReadFb256(pGpu, dst, wprPhys + CMP_SEC2_META_SRC_OFF);
    memdescFlushCpuCaches(pGpu, pKernelGsp->pWprMetaDescriptor);
    if (stR1 == NV_OK)
        match1 = s_cmpSec2Memcmp256(pRd, stockBlk);
    NV_PRINTF(LEVEL_ERROR,
              "CMP_SEC2_PROBE6: read1 st=0x%x match=%u dw=%08x %08x %08x %08x\n",
              stR1, match1,
              pMeta[CMP_SEC2_META_SRC_OFF / 4 + 0],
              pMeta[CMP_SEC2_META_SRC_OFF / 4 + 1],
              pMeta[CMP_SEC2_META_SRC_OFF / 4 + 2],
              pMeta[CMP_SEC2_META_SRC_OFF / 4 + 3]);

    /* step 5: stage patch block, write DMEM -> FB dst */
    portMemCopy((void *)(pMeta + CMP_SEC2_META_SRC_OFF / 4), 256,
                s_cmpPatchBlock, 256);
    GPU_REG_WR32(pGpu, CMP_SEC2_FBIF_TRANSCFG0, CMP_SEC2_TRANSCFG_SYSMEM);
    stS = s_cmpSec2HostDma256(pGpu, wprPhys + CMP_SEC2_META_SRC_OFF,
                              CMP_SEC2_DMEM_A, 0x600U);
    GPU_REG_WR32(pGpu, CMP_SEC2_FBIF_TRANSCFG0, CMP_SEC2_TRANSCFG_FB);
    stW = s_cmpSec2HostDma256(pGpu, dst, CMP_SEC2_DMEM_A, 0x620U);
    NV_PRINTF(LEVEL_ERROR,
              "CMP_SEC2_PROBE6: write stage_st=0x%x st=0x%x\n", stS, stW);

    /* step 6: read-verify again, expect PATCHED block */
    stR2 = s_cmpSec2ReadFb256(pGpu, dst, wprPhys + CMP_SEC2_META_SRC_OFF);
    memdescFlushCpuCaches(pGpu, pKernelGsp->pWprMetaDescriptor);
    if (stR2 == NV_OK)
        match2 = s_cmpSec2Memcmp256(pRd, s_cmpPatchBlock);
    NV_PRINTF(LEVEL_ERROR,
              "CMP_SEC2_PROBE6: read2 st=0x%x match=%u dw=%08x %08x %08x %08x\n",
              stR2, match2,
              pMeta[CMP_SEC2_META_SRC_OFF / 4 + 0],
              pMeta[CMP_SEC2_META_SRC_OFF / 4 + 1],
              pMeta[CMP_SEC2_META_SRC_OFF / 4 + 2],
              pMeta[CMP_SEC2_META_SRC_OFF / 4 + 3]);

    /* step 7: restore */
    GPU_REG_WR32(pGpu, CMP_SEC2_WPR2_LO, lo0);
    GPU_REG_WR32(pGpu, CMP_SEC2_WPR2_HI, hi0);
    loRs = GPU_REG_RD32(pGpu, CMP_SEC2_WPR2_LO);
    hiRs = GPU_REG_RD32(pGpu, CMP_SEC2_WPR2_HI);
    GPU_REG_WR32(pGpu, CMP_SEC2_FBIF_TRANSCFG0, tcEntry);
    NV_PRINTF(LEVEL_ERROR,
              "CMP_SEC2_PROBE6: mode=%u collapse_rb=%08x/%08x read1=%u(0x%x) "
              "write_st=0x%x read2=%u(0x%x) restore_rb=%08x/%08x\n",
              mode, loRb, hiRb, match1, stR1, stW, match2, stR2, loRs, hiRs);
}

/* ---- probe==7: same collapse+DMA path, patch .fwversion string ---- */
static NvU32
s_cmpSec2StrEq9(const volatile NvU8 *p, const char *s)
{
    NvU32 i;
    for (i = 0; i < 9U; i++)
    {
        if (p[i] != (NvU8)s[i])
            return 0;
    }
    return 1;
}

static void
s_cmpSec2DmaProbe7
(
    OBJGPU *pGpu,
    KernelGsp *pKernelGsp
)
{
    KernelSec2 *pKernelSec2;
    NvU64 wprPhys, fwBase;
    volatile NvU32 *pMeta;
    volatile NvU8 *pRd;
    NV_STATUS rst, stR;
    NvU32 lo0, hi0, loRb, hiRb, loRs, hiRs, tcEntry;
    NvU32 i, nStock = 0, nWrote = 0, nVerified = 0;

    /* all "610.43.02" offsets in gsp_rm_tu10x.elf == resident FB offsets
       from gspFwOffset (resident layout == ELF file layout, proven for
       .text by probe6 read1; bin container offsets would be +0x40).
       0x1bfadec is the trailer copy (past ELF EOF, bin-only; self-guarded
       by the stock read-verify). */
    static const NvU32 s_fwverOffs[7] = {
        0x00025e68U, 0x00082280U, 0x000b8ba0U, 0x001897f0U,
        0x001aea08U, 0x001d77a8U, 0x01bfadecU
    };

    if ((pKernelGsp->pWprMeta == NULL) ||
        (pKernelGsp->pWprMetaDescriptor == NULL))
        return;

    pKernelSec2 = GPU_GET_KERNEL_SEC2(pGpu);
    if (pKernelSec2 == NULL)
        return;

    wprPhys = memdescGetPhysAddr(pKernelGsp->pWprMetaDescriptor, AT_GPU, 0);
    pMeta   = (volatile NvU32 *)pKernelGsp->pWprMeta;
    pRd     = (volatile NvU8 *)(pMeta + CMP_SEC2_META_SRC_OFF / 4);
    fwBase  = pKernelGsp->pWprMeta->gspFwOffset;

    /* save WPR2 bounds, collapse (mode 1), probe==4 preamble */
    lo0 = GPU_REG_RD32(pGpu, CMP_SEC2_WPR2_LO);
    hi0 = GPU_REG_RD32(pGpu, CMP_SEC2_WPR2_HI);
    GPU_REG_WR32(pGpu, CMP_SEC2_WPR2_LO, 0U);
    GPU_REG_WR32(pGpu, CMP_SEC2_WPR2_HI, 0U);
    loRb = GPU_REG_RD32(pGpu, CMP_SEC2_WPR2_LO);
    hiRb = GPU_REG_RD32(pGpu, CMP_SEC2_WPR2_HI);

    rst = kflcnReset_HAL(pGpu, staticCast(pKernelSec2, KernelFalcon));
    GPU_REG_WR32(pGpu, 0x00841210U, 0x200U);
    GPU_REG_WR32(pGpu, 0x00841198U, 3U);
    GPU_REG_WR32(pGpu, 0x00841180U, 1U);
    GPU_REG_WR32(pGpu, 0x0084111cU, 1U);
    GPU_REG_WR32(pGpu, CMP_SEC2_FBIF_CTL,
                 GPU_REG_RD32(pGpu, CMP_SEC2_FBIF_CTL) | 0x80U);
    GPU_REG_WR32(pGpu, CMP_SEC2_FALCON_DMACTL, 0);
    tcEntry = GPU_REG_RD32(pGpu, CMP_SEC2_FBIF_TRANSCFG0);
    NV_PRINTF(LEVEL_ERROR,
              "CMP_SEC2_PROBE7: rst=0x%x en=%08x/%08x/%08x/%08x "
              "wpr2 %08x/%08x -> rb %08x/%08x fwBase=%llx\n",
              rst,
              GPU_REG_RD32(pGpu, 0x00841210U), GPU_REG_RD32(pGpu, 0x00841198U),
              GPU_REG_RD32(pGpu, 0x00841180U), GPU_REG_RD32(pGpu, 0x0084111cU),
              lo0, hi0, loRb, hiRb, fwBase);

    for (i = 0; i < 7U; i++)
    {
        NvU64 blk = fwBase + (s_fwverOffs[i] & ~0xffULL);
        NvU32 inoff = s_fwverOffs[i] & 0xffU;
        NvU32 m1 = 0, m2 = 0, wrote = 0;
        NV_STATUS stS, stW = NV_ERR_INVALID_STATE;

        stR = s_cmpSec2ReadFb256(pGpu, blk, wprPhys + CMP_SEC2_META_SRC_OFF);
        memdescFlushCpuCaches(pGpu, pKernelGsp->pWprMetaDescriptor);
        if (stR == NV_OK)
            m1 = s_cmpSec2StrEq9(pRd + inoff, "610.43.02");
        if (m1)
        {
            nStock++;
            portMemCopy((void *)(pRd + inoff), 9, "610.43.99", 9);
            GPU_REG_WR32(pGpu, CMP_SEC2_FBIF_TRANSCFG0,
                         CMP_SEC2_TRANSCFG_SYSMEM);
            stS = s_cmpSec2HostDma256(pGpu, wprPhys + CMP_SEC2_META_SRC_OFF,
                                      CMP_SEC2_DMEM_A, 0x600U);
            GPU_REG_WR32(pGpu, CMP_SEC2_FBIF_TRANSCFG0,
                         CMP_SEC2_TRANSCFG_FB);
            stW = s_cmpSec2HostDma256(pGpu, blk, CMP_SEC2_DMEM_A, 0x620U);
            if ((stS == NV_OK) && (stW == NV_OK))
            {
                wrote = 1;
                nWrote++;
            }
            stR = s_cmpSec2ReadFb256(pGpu, blk,
                                     wprPhys + CMP_SEC2_META_SRC_OFF);
            memdescFlushCpuCaches(pGpu, pKernelGsp->pWprMetaDescriptor);
            if (stR == NV_OK)
                m2 = s_cmpSec2StrEq9(pRd + inoff, "610.43.99");
            if (m2)
                nVerified++;
        }
        NV_PRINTF(LEVEL_ERROR,
                  "CMP_SEC2_PROBE7: off=%x blk=%llx inoff=%x stR=0x%x "
                  "rb=%08x stock=%u wrote=%u verified=%u\n",
                  s_fwverOffs[i], blk, inoff, stR,
                  (stR == NV_OK)
                      ? *(const volatile NvU32 *)(pRd + inoff) : 0U,
                  m1, wrote, m2);
    }

    /* restore */
    GPU_REG_WR32(pGpu, CMP_SEC2_WPR2_LO, lo0);
    GPU_REG_WR32(pGpu, CMP_SEC2_WPR2_HI, hi0);
    loRs = GPU_REG_RD32(pGpu, CMP_SEC2_WPR2_LO);
    hiRs = GPU_REG_RD32(pGpu, CMP_SEC2_WPR2_HI);
    GPU_REG_WR32(pGpu, CMP_SEC2_FBIF_TRANSCFG0, tcEntry);
    NV_PRINTF(LEVEL_ERROR,
              "CMP_SEC2_PROBE7: collapse_rb=%08x/%08x stock=%u/7 wrote=%u "
              "verified=%u restore_rb=%08x/%08x\n",
              loRb, hiRb, nStock, nWrote, nVerified, loRs, hiRs);
}

/* ---- probe==8: C1/C2 arg loggers via WPR2-collapse channel ---- */
extern void cmpScheduleRingDump(void);

static OBJGPU    *s_cmpP8Gpu;
static NvU64      s_cmpP8FwBase;
static NvU32      s_cmpP8DumpIdx;
static NvU32      s_cmpP8NoDump;   /* probe==13: arm only, no dump train */
static NvU32      s_cmpP8Snap[0x1200U / 4U];   /* v46: SEC2 window at arm */
static NvU32      s_cmpP8SnapPlm[4];           /* 9a0148/823804/1fa7c4/1fa7cc */

/*__P8_ARRAYS__*/

struct CmpP8Site { NvU32 off; NvU32 len; const NvU8 *patch; const NvU8 *stock; };

static const struct CmpP8Site s_cmpP8Sites[] = {
    { 0x01027b54U,   8, s_cmpP8Hook1New, s_cmpP8Hook1Orig }, /* C1 hook */
    { 0x01026c34U,  32, s_cmpP8Stub1,    s_cmpP8Stub1Stock },/* C1 stub (dead fn) */
    { 0x0102ccdcU,   8, s_cmpP8Hook2New, s_cmpP8Hook2Orig }, /* C2 hook */
    { 0x01026c60U,  16, s_cmpP8Stub2,    s_cmpP8Stub2Stock },/* C2 stub passthrough */
};

/*
 * One site: read 256B block, verify stock/zero bytes, optionally RMW the
 * patch in and read-verify again. Returns 1 on arm/verify success.
 */
static NvU32
s_cmpSec2Probe8Site
(
    OBJGPU *pGpu,
    KernelGsp *pKernelGsp,
    NvU64 wprPhys,
    volatile NvU8 *pRd,
    NvU64 fwBase,
    const struct CmpP8Site *pSite
)
{
    NvU64 blk = fwBase + (pSite->off & ~0xffU);
    NvU32 inoff = pSite->off & 0xffU;
    NV_STATUS stR, stS, stW;
    NvU32 i;

    stR = s_cmpSec2ReadFb256(pGpu, blk, wprPhys + CMP_SEC2_META_SRC_OFF);
    memdescFlushCpuCaches(pGpu, pKernelGsp->pWprMetaDescriptor);
    if (stR != NV_OK)
    {
        NV_PRINTF(LEVEL_ERROR,
                  "CMP_SEC2_PROBE8: site off=%x read st=0x%x SKIP\n",
                  pSite->off, stR);
        return 0;
    }
    for (i = 0; i < pSite->len; i++)
    {
        NvU8 expect = (pSite->stock != NULL) ? pSite->stock[i] : 0;
        if (pRd[inoff + i] != expect)
        {
            NV_PRINTF(LEVEL_ERROR,
                      "CMP_SEC2_PROBE8: site off=%x stock mismatch +%x "
                      "got=%02x want=%02x SKIP\n",
                      pSite->off, i, pRd[inoff + i], expect);
            return 0;
        }
    }
    if (pSite->patch == NULL)
        return 1;

    portMemCopy((void *)(pRd + inoff), pSite->len, pSite->patch, pSite->len);
    GPU_REG_WR32(pGpu, CMP_SEC2_FBIF_TRANSCFG0, CMP_SEC2_TRANSCFG_SYSMEM);
    stS = s_cmpSec2HostDma256(pGpu, wprPhys + CMP_SEC2_META_SRC_OFF,
                              CMP_SEC2_DMEM_A, 0x600U);
    GPU_REG_WR32(pGpu, CMP_SEC2_FBIF_TRANSCFG0, CMP_SEC2_TRANSCFG_FB);
    stW = s_cmpSec2HostDma256(pGpu, blk, CMP_SEC2_DMEM_A, 0x620U);
    if ((stS != NV_OK) || (stW != NV_OK))
    {
        NV_PRINTF(LEVEL_ERROR,
                  "CMP_SEC2_PROBE8: site off=%x write stS=0x%x stW=0x%x FAIL\n",
                  pSite->off, stS, stW);
        return 0;
    }
    stR = s_cmpSec2ReadFb256(pGpu, blk, wprPhys + CMP_SEC2_META_SRC_OFF);
    memdescFlushCpuCaches(pGpu, pKernelGsp->pWprMetaDescriptor);
    if (stR == NV_OK)
    {
        for (i = 0; i < pSite->len; i++)
        {
            if (pRd[inoff + i] != pSite->patch[i])
            {
                stR = NV_ERR_GENERIC;
                break;
            }
        }
    }
    NV_PRINTF(LEVEL_ERROR,
              "CMP_SEC2_PROBE8: site off=%x arm %s (verify st=0x%x)\n",
              pSite->off, (stR == NV_OK) ? "OK" : "MISMATCH", stR);
    return (stR == NV_OK) ? 1 : 0;
}

static void
s_cmpSec2DmaProbe8
(
    OBJGPU *pGpu,
    KernelGsp *pKernelGsp
)
{
    KernelSec2 *pKernelSec2;
    NvU64 wprPhys, fwBase;
    volatile NvU32 *pMeta;
    NV_STATUS rst;
    NvU32 lo0, hi0, tcEntry;
    NvU32 i;
    NvU32 res[4];
    NvU32 c1, c2, ringzero;

    if ((pKernelGsp->pWprMeta == NULL) ||
        (pKernelGsp->pWprMetaDescriptor == NULL))
        return;

    pKernelSec2 = GPU_GET_KERNEL_SEC2(pGpu);
    if (pKernelSec2 == NULL)
        return;

    wprPhys = memdescGetPhysAddr(pKernelGsp->pWprMetaDescriptor, AT_GPU, 0);
    pMeta   = (volatile NvU32 *)pKernelGsp->pWprMeta;
    fwBase  = pKernelGsp->pWprMeta->gspFwOffset;

    lo0 = GPU_REG_RD32(pGpu, CMP_SEC2_WPR2_LO);
    hi0 = GPU_REG_RD32(pGpu, CMP_SEC2_WPR2_HI);
    GPU_REG_WR32(pGpu, CMP_SEC2_WPR2_LO, 0U);
    GPU_REG_WR32(pGpu, CMP_SEC2_WPR2_HI, 0U);

    rst = kflcnReset_HAL(pGpu, staticCast(pKernelSec2, KernelFalcon));
    GPU_REG_WR32(pGpu, 0x00841210U, 0x200U);
    GPU_REG_WR32(pGpu, 0x00841198U, 3U);
    GPU_REG_WR32(pGpu, 0x00841180U, 1U);
    GPU_REG_WR32(pGpu, 0x0084111cU, 1U);
    GPU_REG_WR32(pGpu, CMP_SEC2_FBIF_CTL,
                 GPU_REG_RD32(pGpu, CMP_SEC2_FBIF_CTL) | 0x80U);
    GPU_REG_WR32(pGpu, CMP_SEC2_FALCON_DMACTL, 0);
    tcEntry = GPU_REG_RD32(pGpu, CMP_SEC2_FBIF_TRANSCFG0);
    NV_PRINTF(LEVEL_ERROR,
              "CMP_SEC2_PROBE8: rst=0x%x en=%08x/%08x/%08x/%08x "
              "wpr2 %08x/%08x -> rb %08x/%08x fwBase=%llx\n",
              rst,
              GPU_REG_RD32(pGpu, 0x00841210U), GPU_REG_RD32(pGpu, 0x00841198U),
              GPU_REG_RD32(pGpu, 0x00841180U), GPU_REG_RD32(pGpu, 0x0084111cU),
              lo0, hi0,
              GPU_REG_RD32(pGpu, CMP_SEC2_WPR2_LO),
              GPU_REG_RD32(pGpu, CMP_SEC2_WPR2_HI),
              fwBase);

    for (i = 0; i < 4U; i++)
        res[i] = s_cmpSec2Probe8Site(pGpu, pKernelGsp, wprPhys,
                                     (volatile NvU8 *)(pMeta + CMP_SEC2_META_SRC_OFF / 4),
                                     fwBase, &s_cmpP8Sites[i]);

    GPU_REG_WR32(pGpu, CMP_SEC2_WPR2_LO, lo0);
    GPU_REG_WR32(pGpu, CMP_SEC2_WPR2_HI, hi0);
    GPU_REG_WR32(pGpu, CMP_SEC2_FBIF_TRANSCFG0, tcEntry);

    c1 = res[0] && res[1];
    c2 = res[2] && res[3];
    ringzero = 9;   /* not checked in v43 (windows are live structs) */
    NV_PRINTF(LEVEL_ERROR,
              "CMP_SEC2_PROBE8: armed c1=%u c2=%u ringzero=%u "
              "restore_rb=%08x/%08x\n",
              c1, c2, ringzero,
              GPU_REG_RD32(pGpu, CMP_SEC2_WPR2_LO),
              GPU_REG_RD32(pGpu, CMP_SEC2_WPR2_HI));

    /* v46: snapshot the host-readable SEC2 window for post-boot diffing */
    for (i = 0; i < 0x1200U / 4U; i++)
        s_cmpP8Snap[i] = GPU_REG_RD32(pGpu, 0x00840000U + i * 4U);
    s_cmpP8SnapPlm[0] = GPU_REG_RD32(pGpu, 0x009a0148U);
    s_cmpP8SnapPlm[1] = GPU_REG_RD32(pGpu, 0x00823804U);
    s_cmpP8SnapPlm[2] = GPU_REG_RD32(pGpu, 0x001fa7c4U);
    s_cmpP8SnapPlm[3] = GPU_REG_RD32(pGpu, 0x001fa7ccU);

    if (c1 || c2)
    {
        s_cmpP8Gpu    = pGpu;
        s_cmpP8FwBase = fwBase;
        s_cmpP8DumpIdx = 0;
        if (!s_cmpP8NoDump)
        {
            cmpScheduleRingDump();
            NV_PRINTF(LEVEL_ERROR, "CMP_SEC2_PROBE8: ring dump scheduled\n");
        }
        else
        {
            NV_PRINTF(LEVEL_ERROR,
                      "CMP_SEC2_PROBE8: armed (no dump train, probe13 exfil)\n");
        }
    }
}

/*
 * Delayed-work callback (process context, called from os-interface.c):
 * re-open the WPR2-collapse channel on the live system and dump both rings.
 */
void
s_cmpSec2RingDump(void)
{
    OBJGPU *pGpu = s_cmpP8Gpu;
    KernelGsp *pKernelGsp;
    KernelSec2 *pKernelSec2;
    NvU64 wprPhys, fwBase;
    volatile NvU32 *pMeta;
    NvU32 lo0, hi0, tcEntry, i, noReset;
    NV_STATUS rst = NV_OK, st = NV_OK;
    const volatile NvU64 *pQ;

    if (pGpu == NULL)
        return;
    /* re-acquire: the KernelGsp object is torn down if GSP boot failed
       (stashing it across boot caused a UAF page fault in v37) */
    pKernelGsp = GPU_GET_KERNEL_GSP(pGpu);
    NV_PRINTF(LEVEL_ERROR,
              "CMP_RING: dump enter pGpu=%s pKernelGsp=%s\n",
              (pGpu != NULL) ? "ok" : "NULL",
              (pKernelGsp != NULL) ? "ok" : "NULL");
    if (pKernelGsp == NULL)
    {
        NV_PRINTF(LEVEL_ERROR,
                  "CMP_RING: dump skipped, KernelGsp gone (GSP boot failed)\n");
        return;
    }
    if ((pKernelGsp->pWprMeta == NULL) ||
        (pKernelGsp->pWprMetaDescriptor == NULL))
    {
        NV_PRINTF(LEVEL_ERROR,
                  "CMP_RING: dump skipped, wprmeta gone\n");
        return;
    }
    pKernelSec2 = GPU_GET_KERNEL_SEC2(pGpu);
    if (pKernelSec2 == NULL)
    {
        NV_PRINTF(LEVEL_ERROR, "CMP_RING: dump skipped, no sec2\n");
        return;
    }

    fwBase  = s_cmpP8FwBase;
    wprPhys = memdescGetPhysAddr(pKernelGsp->pWprMetaDescriptor, AT_GPU, 0);
    pMeta   = (volatile NvU32 *)pKernelGsp->pWprMeta;

    /* v46: diff the SEC2 window vs the arm-time snapshot — PURE READS,
       must run before our own collapse/reset/preamble writes below */
    for (i = 0; i < 0x1200U / 4U; i++)
    {
        NvU32 now = GPU_REG_RD32(pGpu, 0x00840000U + i * 4U);
        if (now != s_cmpP8Snap[i])
        {
            NV_PRINTF(LEVEL_ERROR,
                      "CMP_SEC2_DIFF: dump #%u off=0x%x arm=%08x now=%08x\n",
                      s_cmpP8DumpIdx, i * 4U, s_cmpP8Snap[i], now);
        }
    }
    {
        static const NvU32 s_plmRegs[4] = {
            0x009a0148U, 0x00823804U, 0x001fa7c4U, 0x001fa7ccU
        };
        for (i = 0; i < 4U; i++)
        {
            NvU32 now = GPU_REG_RD32(pGpu, s_plmRegs[i]);
            if (now != s_cmpP8SnapPlm[i])
            {
                NV_PRINTF(LEVEL_ERROR,
                          "CMP_SEC2_DIFF: dump #%u plm=%08x arm=%08x now=%08x\n",
                          s_cmpP8DumpIdx, s_plmRegs[i], s_cmpP8SnapPlm[i], now);
            }
        }
    }

    lo0 = GPU_REG_RD32(pGpu, CMP_SEC2_WPR2_LO);
    hi0 = GPU_REG_RD32(pGpu, CMP_SEC2_WPR2_HI);
    GPU_REG_WR32(pGpu, CMP_SEC2_WPR2_LO, 0U);
    GPU_REG_WR32(pGpu, CMP_SEC2_WPR2_HI, 0U);

    noReset = 0;
    (void)osReadRegistryDword(pGpu, "RMCmpSec2DumpNoReset", &noReset);
    if (noReset == 0U)
        rst = kflcnReset_HAL(pGpu, staticCast(pKernelSec2, KernelFalcon));
    GPU_REG_WR32(pGpu, 0x00841210U, 0x200U);
    GPU_REG_WR32(pGpu, 0x00841198U, 3U);
    GPU_REG_WR32(pGpu, 0x00841180U, 1U);
    GPU_REG_WR32(pGpu, 0x0084111cU, 1U);
    GPU_REG_WR32(pGpu, CMP_SEC2_FBIF_CTL,
                 GPU_REG_RD32(pGpu, CMP_SEC2_FBIF_CTL) | 0x80U);
    GPU_REG_WR32(pGpu, CMP_SEC2_FALCON_DMACTL, 0);
    tcEntry = GPU_REG_RD32(pGpu, CMP_SEC2_FBIF_TRANSCFG0);

    /*
     * v47: write back the lock registers that GSP-start raises
     * (v46 diff: 0x250 0xf->0, 0x240 0x3000->0x7021). Mask first,
     * then control. Readback-logged.
     */
    GPU_REG_WR32(pGpu, 0x00840250U, 0xfU);
    GPU_REG_WR32(pGpu, 0x00840240U, 0x3000U);
    NV_PRINTF(LEVEL_ERROR,
              "CMP_UNLOCK: dump #%u rb250=%08x rb240=%08x\n",
              s_cmpP8DumpIdx,
              GPU_REG_RD32(pGpu, 0x00840250U),
              GPU_REG_RD32(pGpu, 0x00840240U));

    /*
     * v45 self-test: prove the post-boot channel before trusting it.
     * Readbacks of the collapse + TRANSCFG0, then a read of image file-0
     * compared against the known ELF header bytes.
     * WPR2 regs: VAL=31:4, ALIGNMENT=0xc (dev_fb.h tu102) => reg = addr>>8.
     */
    GPU_REG_WR32(pGpu, CMP_SEC2_FBIF_TRANSCFG0, CMP_SEC2_TRANSCFG_FB);
    NV_PRINTF(LEVEL_ERROR,
              "CMP_CH_SELFTEST: dump #%u noreset=%u rst=0x%x wpr2_rb=%08x/%08x "
              "tc_rb=%08x (entry %08x)\n",
              s_cmpP8DumpIdx, noReset, rst,
              GPU_REG_RD32(pGpu, CMP_SEC2_WPR2_LO),
              GPU_REG_RD32(pGpu, CMP_SEC2_WPR2_HI),
              GPU_REG_RD32(pGpu, CMP_SEC2_FBIF_TRANSCFG0), tcEntry);
    st = s_cmpSec2ReadFb256(pGpu, fwBase, wprPhys + CMP_SEC2_META_SRC_OFF);
    memdescFlushCpuCaches(pGpu, pKernelGsp->pWprMetaDescriptor);
    if (st == NV_OK)
    {
        static const NvU8 s_img0[16] = {
            0x7f, 0x45, 0x4c, 0x46, 0x02, 0x01, 0x01, 0x00,
            0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00
        };
        const volatile NvU8 *pV = (const volatile NvU8 *)(pMeta + CMP_SEC2_META_SRC_OFF / 4);
        NvU32 match = 1;
        for (i = 0; i < 16U; i++)
        {
            if (pV[i] != s_img0[i])
            {
                match = 0;
                break;
            }
        }
        NV_PRINTF(LEVEL_ERROR,
                  "CMP_CH_SELFTEST: dump #%u match=%u got=%02x%02x%02x%02x"
                  "%02x%02x%02x%02x\n",
                  s_cmpP8DumpIdx, match,
                  pV[0], pV[1], pV[2], pV[3], pV[4], pV[5], pV[6], pV[7]);
    }
    else
    {
        NV_PRINTF(LEVEL_ERROR, "CMP_CH_SELFTEST: dump #%u read st=0x%x\n",
                  s_cmpP8DumpIdx, st);
    }

    /* v44 mailbox slots: counter + conditional (bit35) ra/a5 records */
    {
        NvU64 cnt = 0, ra35 = 0, a535 = 0, maSpot = 0;

        st = s_cmpSec2ReadFb256(pGpu, fwBase + 0x189700U,
                                wprPhys + CMP_SEC2_META_SRC_OFF);
        memdescFlushCpuCaches(pGpu, pKernelGsp->pWprMetaDescriptor);
        if (st == NV_OK)
            cnt = *(const volatile NvU64 *)
                   ((volatile NvU8 *)(pMeta + CMP_SEC2_META_SRC_OFF / 4) + 0xf0U);

        st = s_cmpSec2ReadFb256(pGpu, fwBase + 0x1aea00U,
                                wprPhys + CMP_SEC2_META_SRC_OFF);
        memdescFlushCpuCaches(pGpu, pKernelGsp->pWprMetaDescriptor);
        if (st == NV_OK)
            ra35 = *(const volatile NvU64 *)
                    ((volatile NvU8 *)(pMeta + CMP_SEC2_META_SRC_OFF / 4) + 0x8U);

        st = s_cmpSec2ReadFb256(pGpu, fwBase + 0x1d77a0U,
                                wprPhys + CMP_SEC2_META_SRC_OFF);
        memdescFlushCpuCaches(pGpu, pKernelGsp->pWprMetaDescriptor);
        if (st == NV_OK)
            a535 = *(const volatile NvU64 *)
                    ((volatile NvU8 *)(pMeta + CMP_SEC2_META_SRC_OFF / 4) + 0x8U);

        st = s_cmpSec2ReadFb256(pGpu, fwBase + 0x38a500U,
                                wprPhys + CMP_SEC2_META_SRC_OFF);
        memdescFlushCpuCaches(pGpu, pKernelGsp->pWprMetaDescriptor);
        if (st == NV_OK)
            maSpot = *(const volatile NvU64 *)
                      ((volatile NvU8 *)(pMeta + CMP_SEC2_META_SRC_OFF / 4) + 0x8U);

        NV_PRINTF(LEVEL_ERROR,
                  "CMP_RING_SLOTS: dump #%u cnt=%llx ra35=%llx a535=%llx "
                  "magicA_spot=%llx (slot init %llx)\n",
                  s_cmpP8DumpIdx, cnt, ra35, a535, maSpot,
                  0x302e33342e303136ULL);
    }

    /* self-check: fwversion spot (referenced copy, stock in v44) */
    st = s_cmpSec2ReadFb256(pGpu, fwBase + 0x25e00U,
                            wprPhys + CMP_SEC2_META_SRC_OFF);
    memdescFlushCpuCaches(pGpu, pKernelGsp->pWprMetaDescriptor);
    if (st == NV_OK)
    {
        const volatile NvU8 *pV = (const volatile NvU8 *)(pMeta + CMP_SEC2_META_SRC_OFF / 4);
        NV_PRINTF(LEVEL_ERROR,
                  "CMP_RING_SC: dump #%u ver=%02x%02x%02x%02x%02x%02x%02x%02x%02x\n",
                  s_cmpP8DumpIdx,
                  pV[0x68], pV[0x69], pV[0x6a], pV[0x6b], pV[0x6c],
                  pV[0x6d], pV[0x6e], pV[0x6f], pV[0x70]);
    }

    /*
     * v45 sweep: corrected WPR2 math (reg = addr>>8; dev_fb.h VAL=31:4,
     * ALIGNMENT=0xc). Span A (lower WPR, heap) on dump #1, span B (WPR2
     * bounds) on dump #5. Patterns: v45/v43/v41/42/40 leftovers (FB
     * survives FLR on this card).
     */
    {
        NvU64 spans[2][2];
        NvU32 r;

        if (s_cmpP8DumpIdx == 1U)
        {
            spans[0][0] = 0x13ee400000ULL; spans[0][1] = 0x13f7200000ULL;
            r = 1;   /* one span this dump */
        }
        else if (s_cmpP8DumpIdx == 5U)
        {
            spans[0][0] = ((NvU64)lo0 << 8); spans[0][1] = ((NvU64)hi0 << 8);
            r = 1;
        }
        else
        {
            r = 0;
        }

        if (r != 0U)
        {
            static const NvU64 s_magics[12] = {
                0xffffffffc0ffee51ULL, 0xffffffffc0ffee52ULL,
                0xffffffffc0ffee53ULL, 0xffffffffc0ffee54ULL,
                0xffffffffc0ffee55ULL, 0xffffffffc0ffee56ULL,
                0xffffffffc0ffee57ULL, 0xffffffffc0ffee58ULL,
                0xffffffffc0ffee41ULL, 0xffffffffc0ffee42ULL,
                0xffffffffc0ffee49ULL, 0xc1c1c1c1c1c1c1c1ULL
            };
            NvU64 addr;
            NvU32 nblk = 0;

            NV_PRINTF(LEVEL_ERROR,
                      "CMP_RING_SWEEP: dump #%u span %llx..%llx start\n",
                      s_cmpP8DumpIdx, spans[0][0], spans[0][1]);
            for (addr = spans[0][0]; addr < spans[0][1]; addr += 256U)
            {
                st = s_cmpSec2ReadFb256(pGpu, addr, wprPhys + CMP_SEC2_META_SRC_OFF);
                if (st != NV_OK)
                {
                    NV_PRINTF(LEVEL_ERROR,
                              "CMP_RING_SWEEP: dump #%u addr=%llx st=0x%x (abort)\n",
                              s_cmpP8DumpIdx, addr, st);
                    break;
                }
                memdescFlushCpuCaches(pGpu, pKernelGsp->pWprMetaDescriptor);
                pQ = (const volatile NvU64 *)(pMeta + CMP_SEC2_META_SRC_OFF / 4);
                for (i = 0; i < 32U; i++)
                {
                    NvU64 v = pQ[i];
                    NvU32 m;
                    for (m = 0; m < 12U; m++)
                    {
                        if (v == s_magics[m])
                        {
                            NV_PRINTF(LEVEL_ERROR,
                                      "CMP_RING_SWEEP: HIT dump #%u phys=%llx val=%llx\n",
                                      s_cmpP8DumpIdx, addr + (NvU64)i * 8U, v);
                        }
                    }
                }
                if ((++nblk & 0xfffU) == 0U)
                {
                    NV_PRINTF(LEVEL_ERROR, "CMP_RING_SWEEP: dump #%u progress %llx\n",
                              s_cmpP8DumpIdx, addr);
                }
            }
            NV_PRINTF(LEVEL_ERROR,
                      "CMP_RING_SWEEP: dump #%u span done blocks=%u\n",
                      s_cmpP8DumpIdx, nblk);
        }
    }

    GPU_REG_WR32(pGpu, CMP_SEC2_WPR2_LO, lo0);
    GPU_REG_WR32(pGpu, CMP_SEC2_WPR2_HI, hi0);
    GPU_REG_WR32(pGpu, CMP_SEC2_FBIF_TRANSCFG0, tcEntry);

    NV_PRINTF(LEVEL_ERROR,
              "CMP_RING: dump #%u done (entry wpr2=%08x/%08x)\n",
              s_cmpP8DumpIdx, lo0, hi0);
    s_cmpP8DumpIdx++;
}

/* ---- probe==17 (v59): VBIOS devinit-table patch at host supply point ---- */
NV_STATUS os_cmpWritePathFile(const char *path, NvU8 *pBuffer, NvU64 size);

static NvU32
s_cmpRd32(const NvU8 *p, NvU32 off)
{
    return (NvU32)p[off] | ((NvU32)p[off + 1] << 8) |
           ((NvU32)p[off + 2] << 16) | ((NvU32)p[off + 3] << 24);
}

static void
s_cmpWr32(NvU8 *p, NvU32 off, NvU32 v)
{
    p[off]     = (NvU8)v;
    p[off + 1] = (NvU8)(v >> 8);
    p[off + 2] = (NvU8)(v >> 16);
    p[off + 3] = (NvU8)(v >> 24);
}

void
s_cmpVbiosPatchHook(OBJGPU *pGpu, NvU8 *pImage, NvU32 biosSize)
{
    NvU32 probe = 0;
    NvU32 base;      /* 0 = absolute ROM offsets; 0x5e00 = PCI-image-relative */
    NvU32 i, nPatched;
    NvU32 gspDevId;
    NV_STATUS st;
    static const NvU32 s_offs[4] = { 0x4007cU, 0x40084U, 0xa007cU, 0xa0084U };

    gspDevId = pGpu->idInfo.PCIDeviceID >> 16;
    if (gspDevId != 0x2082)
        return;
    (void)osReadRegistryDword(pGpu, "RMCmpSec2DmaProbe", &probe);
    if (probe != 17)
        return;
    if ((pImage == NULL) || (biosSize < 0x10000U))
        return;

    st = os_cmpWritePathFile("/home/icy/f0/cmp_vbios_dump.rom",
                             pImage, biosSize);
    NV_PRINTF(LEVEL_ERROR, "CMP_VBIOS_PATCH: dump st=0x%x size=0x%x\n",
              st, biosSize);

    /*
     * CMP-10G 92.00.66.00.02 layout (empirical, from the dumped image):
     * row for 0x9a0294 at unaligned file offsets — CMP value at
     * 0x3bac9/0x3bad1, A100 p10 value already at 0x3bae9; no second
     * mirror image in this ROM (size 0x41800). Verify all three anchors
     * before touching anything. Fallbacks: A100-style absolute offsets.
     */
    if ((biosSize > 0x3baedU) &&
        (s_cmpRd32(pImage, 0x3bac9U) == 0x38d4841bU) &&
        (s_cmpRd32(pImage, 0x3bad1U) == 0x38d4841bU) &&
        (s_cmpRd32(pImage, 0x3bae9U) == 0x39268428U))
    {
        NvU32 c;
        nPatched = 0;
        for (c = 0; c < 2U; c++)
        {
            NvU32 off = (c == 0) ? 0x3bac9U : 0x3bad1U;
            NvU32 old = s_cmpRd32(pImage, off);
            if (old == 0x38d4841bU)
            {
                s_cmpWr32(pImage, off, 0x39268428U);
                nPatched++;
            }
            NV_PRINTF(LEVEL_ERROR,
                      "CMP_VBIOS_PATCH: cmp-layout off=%x old=%08x new=%08x\n",
                      off, old, s_cmpRd32(pImage, off));
        }
        NV_PRINTF(LEVEL_ERROR, "CMP_VBIOS_PATCH: done patched=%u/2\n",
                  nPatched);

        /*
         * Canary rows (v59 round 3): prove whether the patched buffer is
         * consumed by FWSEC devinit at all. row0 (0x9a0290) and row2
         * (0x9a0298) get their A100 values; if the live registers change
         * while 0x9a0294 does not, the 294 row is selectively
         * overridden/skipped downstream. Verify-then-write, same as above.
         */
        if ((biosSize > 0x3bb15U) &&
            (s_cmpRd32(pImage, 0x3ba91U) == 0x1255b93cU) &&
            (s_cmpRd32(pImage, 0x3bb09U) == 0x88130b11U) &&
            (s_cmpRd32(pImage, 0x3bb11U) == 0x88130b11U))
        {
            static const NvU32 s_canaryOff[3] = { 0x3ba91U, 0x3bb09U, 0x3bb11U };
            static const NvU32 s_canaryOld[3] = { 0x1255b93cU, 0x88130b11U, 0x88130b11U };
            static const NvU32 s_canaryNew[3] = { 0x1861a048U, 0x881b0b11U, 0x881b0b11U };
            NvU32 nCanary = 0;
            for (c = 0; c < 3U; c++)
            {
                NvU32 old = s_cmpRd32(pImage, s_canaryOff[c]);
                if (old == s_canaryOld[c])
                {
                    s_cmpWr32(pImage, s_canaryOff[c], s_canaryNew[c]);
                    nCanary++;
                }
                NV_PRINTF(LEVEL_ERROR,
                          "CMP_VBIOS_PATCH: canary off=%x old=%08x new=%08x\n",
                          s_canaryOff[c], old, s_cmpRd32(pImage, s_canaryOff[c]));
            }
            NV_PRINTF(LEVEL_ERROR, "CMP_VBIOS_PATCH: canary done %u/3\n",
                      nCanary);
        }
        else
        {
            NV_PRINTF(LEVEL_ERROR,
                      "CMP_VBIOS_PATCH: canary anchors MISMATCH, skipping "
                      "(r0=%08x r2a=%08x r2b=%08x)\n",
                      (biosSize > 0x3ba95U) ? s_cmpRd32(pImage, 0x3ba91U) : 0,
                      (biosSize > 0x3bb0dU) ? s_cmpRd32(pImage, 0x3bb09U) : 0,
                      (biosSize > 0x3bb15U) ? s_cmpRd32(pImage, 0x3bb11U) : 0);
        }
        return;
    }

    if ((biosSize > 0xa0088U) &&
        (s_cmpRd32(pImage, 0x4007cU) == 0x38d4841bU) &&
        (s_cmpRd32(pImage, 0x4009cU) == 0x39268428U))
        base = 0;
    else if ((biosSize > 0xa0088U - 0x5e00U) &&
             (s_cmpRd32(pImage, 0x3a27cU) == 0x38d4841bU) &&
             (s_cmpRd32(pImage, 0x3a29cU) == 0x39268428U))
        base = 0x5e00U;
    else
    {
        NV_PRINTF(LEVEL_ERROR,
                  "CMP_VBIOS_PATCH: layout UNKNOWN, refusing "
                  "(abs=%08x rel=%08x cmp=%08x)\n",
                  (biosSize > 0x40080U) ? s_cmpRd32(pImage, 0x4007cU) : 0,
                  (biosSize > 0x3a280U) ? s_cmpRd32(pImage, 0x3a27cU) : 0,
                  (biosSize > 0x3bacdU) ? s_cmpRd32(pImage, 0x3bac9U) : 0);
        return;
    }

    nPatched = 0;
    for (i = 0; i < 4U; i++)
    {
        NvU32 off = s_offs[i] - base;
        NvU32 old = s_cmpRd32(pImage, off);
        if (old == 0x38d4841bU)
        {
            s_cmpWr32(pImage, off, 0x39268428U);
            nPatched++;
        }
        NV_PRINTF(LEVEL_ERROR,
                  "CMP_VBIOS_PATCH: base=%x off=%x old=%08x new=%08x\n",
                  base, off, old, s_cmpRd32(pImage, off));
    }
    NV_PRINTF(LEVEL_ERROR, "CMP_VBIOS_PATCH: done patched=%u/4\n", nPatched);
}

/* ---- probe==19 (v61): R1 final-judgement probe ladder ----
 * Per-round stub bytes from gsp_analysis/re2/gen_probe61.py. Same hook
 * site (0xe2e0f0) and dead-function cave (0x1026c34) as probe==18, but no
 * host-patched data slots and no translation-window programming (the app
 * has no window-CSR privilege, v60-retry). Reports via PGSP mailbox
 * (bus 0x110440/444 + spares). Host readout: mmio_rw <bdf> r 0x1104xx.
 */
static void
s_cmpSec2DmaProbe19
(
    OBJGPU *pGpu,
    KernelGsp *pKernelGsp
)
{
    KernelSec2 *pKernelSec2;
    NvU64 wprPhys, fwBase;
    volatile NvU32 *pMeta;
    NV_STATUS rst;
    NvU32 lo0, hi0, tcEntry;
    NvU32 i, n;
    NvU32 res[3];
    struct CmpP8Site sites[3];

    if ((pKernelGsp->pWprMeta == NULL) ||
        (pKernelGsp->pWprMetaDescriptor == NULL))
        return;

    pKernelSec2 = GPU_GET_KERNEL_SEC2(pGpu);
    if (pKernelSec2 == NULL)
        return;

    wprPhys = memdescGetPhysAddr(pKernelGsp->pWprMetaDescriptor, AT_GPU, 0);
    pMeta   = (volatile NvU32 *)pKernelGsp->pWprMeta;
    fwBase  = pKernelGsp->pWprMeta->gspFwOffset;

    lo0 = GPU_REG_RD32(pGpu, CMP_SEC2_WPR2_LO);
    hi0 = GPU_REG_RD32(pGpu, CMP_SEC2_WPR2_HI);

    /* stub first, hook last: never leave a live jump into a half-written stub */
    n = 0;
    sites[n].off = 0x01026c34U;
    sites[n].len = (V61_STUB_LEN > 204U) ? 204U : V61_STUB_LEN;
    sites[n].patch = s_cmpV61Stub;      sites[n].stock = s_cmpV61StubStock;
    n++;
    if (V61_STUB_LEN > 204U)
    {
        sites[n].off = 0x01026d00U;
        sites[n].len = V61_STUB_LEN - 204U;
        sites[n].patch = s_cmpV61Stub + 204U;
        sites[n].stock = s_cmpV61StubStock + 204U;
        n++;
    }
    sites[n].off = 0x00e2e0f0U; sites[n].len = 8U;
    sites[n].patch = s_cmpV61HookNew;   sites[n].stock = s_cmpV61HookOrig;
    n++;

    GPU_REG_WR32(pGpu, CMP_SEC2_WPR2_LO, 0U);
    GPU_REG_WR32(pGpu, CMP_SEC2_WPR2_HI, 0U);

    rst = kflcnReset_HAL(pGpu, staticCast(pKernelSec2, KernelFalcon));
    GPU_REG_WR32(pGpu, 0x00841210U, 0x200U);
    GPU_REG_WR32(pGpu, 0x00841198U, 3U);
    GPU_REG_WR32(pGpu, 0x00841180U, 1U);
    GPU_REG_WR32(pGpu, 0x0084111cU, 1U);
    GPU_REG_WR32(pGpu, CMP_SEC2_FBIF_CTL,
                 GPU_REG_RD32(pGpu, CMP_SEC2_FBIF_CTL) | 0x80U);
    GPU_REG_WR32(pGpu, CMP_SEC2_FALCON_DMACTL, 0);
    tcEntry = GPU_REG_RD32(pGpu, CMP_SEC2_FBIF_TRANSCFG0);
    NV_PRINTF(LEVEL_ERROR,
              "CMP_V61: rst=0x%x wpr2 %08x/%08x fwBase=%llx sites=%u\n",
              rst, lo0, hi0, fwBase, n);

    for (i = 0; i < n; i++)
        res[i] = s_cmpSec2Probe8Site(pGpu, pKernelGsp, wprPhys,
                                     (volatile NvU8 *)(pMeta + CMP_SEC2_META_SRC_OFF / 4),
                                     fwBase, &sites[i]);

    GPU_REG_WR32(pGpu, CMP_SEC2_WPR2_LO, lo0);
    GPU_REG_WR32(pGpu, CMP_SEC2_WPR2_HI, hi0);
    GPU_REG_WR32(pGpu, CMP_SEC2_FBIF_TRANSCFG0, tcEntry);

    NV_PRINTF(LEVEL_ERROR,
              "CMP_V61: armed res=%u%u%u restore_rb=%08x/%08x\n",
              res[0], res[1], (n > 2U) ? res[2] : 9U,
              GPU_REG_RD32(pGpu, CMP_SEC2_WPR2_LO),
              GPU_REG_RD32(pGpu, CMP_SEC2_WPR2_HI));
}

/* ---- probe==18 (v60): R1-window WPR2 scan+rewrite stub ----
 * Arms a GSP-RM stub hooked at the orchestrator vtable-call return
 * (link va 0x4e2e0f0, suspected post-FWSEC-exec / pre-DEVINIT-consume).
 * The stub maps WPR2 through libos translation window 7, scans for the
 * CMP devinit value 0x38d4841b, rewrites hits to 0x39268428 (A100 p10),
 * and reports via PGSP mailbox regs (bus 0x110040/44). Stub bytes and
 * the mailbox protocol are generated by gsp_analysis/re2/gen_probe60.py.
 * Host readout after boot: mmio_rw 0000:3d:00.0 r 0x110040 / 0x110044.
 */
static void
s_cmpSec2DmaProbe18
(
    OBJGPU *pGpu,
    KernelGsp *pKernelGsp
)
{
    KernelSec2 *pKernelSec2;
    NvU64 wprPhys, fwBase;
    volatile NvU32 *pMeta;
    NV_STATUS rst;
    NvU32 lo0, hi0, tcEntry;
    NvU32 i;
    NvU32 res[3];
    NvU8  stub[R1_STUB_LEN];
    struct CmpP8Site sites[3];
    NvU64 paLo, span;

    if ((pKernelGsp->pWprMeta == NULL) ||
        (pKernelGsp->pWprMetaDescriptor == NULL))
        return;

    pKernelSec2 = GPU_GET_KERNEL_SEC2(pGpu);
    if (pKernelSec2 == NULL)
        return;

    wprPhys = memdescGetPhysAddr(pKernelGsp->pWprMetaDescriptor, AT_GPU, 0);
    pMeta   = (volatile NvU32 *)pKernelGsp->pWprMeta;
    fwBase  = pKernelGsp->pWprMeta->gspFwOffset;

    lo0 = GPU_REG_RD32(pGpu, CMP_SEC2_WPR2_LO);
    hi0 = GPU_REG_RD32(pGpu, CMP_SEC2_WPR2_HI);

    paLo = ((NvU64)lo0) << 8;
    span = ((NvU64)(hi0 - lo0)) << 8;
    portMemCopy(stub, sizeof(stub), s_cmpR1Stub, R1_STUB_LEN);
    portMemCopy(stub + R1_SLOT_PALO_OFF, 8, &paLo, 8);
    portMemCopy(stub + R1_SLOT_SPAN_OFF, 8, &span, 8);
    portMemCopy(stub + R1_SLOT_FWBASE_OFF, 8, &fwBase, 8);

    /* stub first, hook last: never leave a live jump into a half-written stub */
    sites[0].off = 0x01026c34U; sites[0].len = 204U;
    sites[0].patch = stub;           sites[0].stock = s_cmpR1StubStock;
    sites[1].off = 0x01026d00U; sites[1].len = R1_STUB_LEN - 204U;
    sites[1].patch = stub + 204U;    sites[1].stock = s_cmpR1StubStock + 204U;
    sites[2].off = 0x00e2e0f0U; sites[2].len = 8U;
    sites[2].patch = s_cmpR1HookNew; sites[2].stock = s_cmpR1HookOrig;

    GPU_REG_WR32(pGpu, CMP_SEC2_WPR2_LO, 0U);
    GPU_REG_WR32(pGpu, CMP_SEC2_WPR2_HI, 0U);

    rst = kflcnReset_HAL(pGpu, staticCast(pKernelSec2, KernelFalcon));
    GPU_REG_WR32(pGpu, 0x00841210U, 0x200U);
    GPU_REG_WR32(pGpu, 0x00841198U, 3U);
    GPU_REG_WR32(pGpu, 0x00841180U, 1U);
    GPU_REG_WR32(pGpu, 0x0084111cU, 1U);
    GPU_REG_WR32(pGpu, CMP_SEC2_FBIF_CTL,
                 GPU_REG_RD32(pGpu, CMP_SEC2_FBIF_CTL) | 0x80U);
    GPU_REG_WR32(pGpu, CMP_SEC2_FALCON_DMACTL, 0);
    tcEntry = GPU_REG_RD32(pGpu, CMP_SEC2_FBIF_TRANSCFG0);
    NV_PRINTF(LEVEL_ERROR,
              "CMP_R1: rst=0x%x wpr2 %08x/%08x paLo=%llx span=%llx "
              "fwBase=%llx\n",
              rst, lo0, hi0, paLo, span, fwBase);

    for (i = 0; i < 3U; i++)
        res[i] = s_cmpSec2Probe8Site(pGpu, pKernelGsp, wprPhys,
                                     (volatile NvU8 *)(pMeta + CMP_SEC2_META_SRC_OFF / 4),
                                     fwBase, &sites[i]);

    GPU_REG_WR32(pGpu, CMP_SEC2_WPR2_LO, lo0);
    GPU_REG_WR32(pGpu, CMP_SEC2_WPR2_HI, hi0);
    GPU_REG_WR32(pGpu, CMP_SEC2_FBIF_TRANSCFG0, tcEntry);

    NV_PRINTF(LEVEL_ERROR,
              "CMP_R1: armed stub=%u%u hook=%u restore_rb=%08x/%08x\n",
              res[0], res[1], res[2],
              GPU_REG_RD32(pGpu, CMP_SEC2_WPR2_LO),
              GPU_REG_RD32(pGpu, CMP_SEC2_WPR2_HI));
}

/* ---- probe==12: cross-boot mailbox readback (v48) ----
 * Runs right after kgspPopulateWprMeta_HAL (gspFwOffset known), BEFORE the
 * first BooterLoad of this boot would reload the stock image over the
 * previous boot's mailbox writes. FB content survives FLR on this card.
 * Reads the v44 logger slots + a referenced version-string control.
 */
void
s_cmpSec2DmaProbe12EarlyRead
(
    OBJGPU *pGpu,
    KernelGsp *pKernelGsp
)
{
    NvU32 probe = 0;
    NvU32 gspDevId;
    KernelSec2 *pKernelSec2;
    NvU64 fwBase, wprPhys;
    volatile NvU32 *pMeta;
    NV_STATUS rst, st;
    NvU32 tcEntry, w2lo, w2hi, i;
    NvU64 slot1 = 0, slot2 = 0, slot3 = 0;
    NvU8 ctrl[9];

    gspDevId = pGpu->idInfo.PCIDeviceID >> 16;
    if (gspDevId != 0x2082)
        return;
    (void)osReadRegistryDword(pGpu, "RMCmpSec2DmaProbe", &probe);
    if (probe != 12)
        return;
    if ((pKernelGsp == NULL) || (pKernelGsp->pWprMeta == NULL) ||
        (pKernelGsp->pWprMetaDescriptor == NULL))
        return;
    pKernelSec2 = GPU_GET_KERNEL_SEC2(pGpu);
    if (pKernelSec2 == NULL)
        return;

    fwBase  = pKernelGsp->pWprMeta->gspFwOffset;
    wprPhys = memdescGetPhysAddr(pKernelGsp->pWprMetaDescriptor, AT_GPU, 0);
    pMeta   = (volatile NvU32 *)pKernelGsp->pWprMeta;

    w2lo = GPU_REG_RD32(pGpu, CMP_SEC2_WPR2_LO);
    w2hi = GPU_REG_RD32(pGpu, CMP_SEC2_WPR2_HI);
    GPU_REG_WR32(pGpu, CMP_SEC2_WPR2_LO, 0U);
    GPU_REG_WR32(pGpu, CMP_SEC2_WPR2_HI, 0U);

    rst = kflcnReset_HAL(pGpu, staticCast(pKernelSec2, KernelFalcon));
    GPU_REG_WR32(pGpu, 0x00841210U, 0x200U);
    GPU_REG_WR32(pGpu, 0x00841198U, 3U);
    GPU_REG_WR32(pGpu, 0x00841180U, 1U);
    GPU_REG_WR32(pGpu, 0x0084111cU, 1U);
    GPU_REG_WR32(pGpu, CMP_SEC2_FBIF_CTL,
                 GPU_REG_RD32(pGpu, CMP_SEC2_FBIF_CTL) | 0x80U);
    GPU_REG_WR32(pGpu, CMP_SEC2_FALCON_DMACTL, 0);
    tcEntry = GPU_REG_RD32(pGpu, CMP_SEC2_FBIF_TRANSCFG0);

    st = s_cmpSec2ReadFb256(pGpu, fwBase + 0x189700U,
                            wprPhys + CMP_SEC2_META_SRC_OFF);
    memdescFlushCpuCaches(pGpu, pKernelGsp->pWprMetaDescriptor);
    if (st == NV_OK)
        slot1 = *(const volatile NvU64 *)
                 ((volatile NvU8 *)(pMeta + CMP_SEC2_META_SRC_OFF / 4) + 0xf0U);

    st = s_cmpSec2ReadFb256(pGpu, fwBase + 0x1aea00U,
                            wprPhys + CMP_SEC2_META_SRC_OFF);
    memdescFlushCpuCaches(pGpu, pKernelGsp->pWprMetaDescriptor);
    if (st == NV_OK)
        slot2 = *(const volatile NvU64 *)
                 ((volatile NvU8 *)(pMeta + CMP_SEC2_META_SRC_OFF / 4) + 0x8U);

    st = s_cmpSec2ReadFb256(pGpu, fwBase + 0x1d77a0U,
                            wprPhys + CMP_SEC2_META_SRC_OFF);
    memdescFlushCpuCaches(pGpu, pKernelGsp->pWprMetaDescriptor);
    if (st == NV_OK)
        slot3 = *(const volatile NvU64 *)
                 ((volatile NvU8 *)(pMeta + CMP_SEC2_META_SRC_OFF / 4) + 0x8U);

    portMemSet((void *)ctrl, 9, 0);
    st = s_cmpSec2ReadFb256(pGpu, fwBase + 0x25e00U,
                            wprPhys + CMP_SEC2_META_SRC_OFF);
    memdescFlushCpuCaches(pGpu, pKernelGsp->pWprMetaDescriptor);
    if (st == NV_OK)
    {
        const volatile NvU8 *pV = (const volatile NvU8 *)(pMeta + CMP_SEC2_META_SRC_OFF / 4);
        for (i = 0; i < 9U; i++)
            ctrl[i] = pV[0x68 + i];
    }

    GPU_REG_WR32(pGpu, CMP_SEC2_FBIF_TRANSCFG0, tcEntry);
    GPU_REG_WR32(pGpu, CMP_SEC2_WPR2_LO, w2lo);
    GPU_REG_WR32(pGpu, CMP_SEC2_WPR2_HI, w2hi);

    NV_PRINTF(LEVEL_ERROR,
              "CMP_XBOOT2: rst=0x%x fwBase=%llx wpr2entry=%08x/%08x "
              "slot1=%016llx slot2=%016llx slot3=%016llx "
              "ctrl=%02x%02x%02x%02x%02x%02x%02x%02x%02x\n",
              rst, fwBase, w2lo, w2hi, slot1, slot2, slot3,
              ctrl[0], ctrl[1], ctrl[2], ctrl[3], ctrl[4],
              ctrl[5], ctrl[6], ctrl[7], ctrl[8]);

    /*
     * v48b: at this hook fwBase is the EARLY (10G-layout) value
     * (0x27e300000); the runtime 80G-layout image sits at 0x13fe300000
     * (deterministic across all probe cycles). Boot N's mailbox writes
     * live there. Read the slots at that base too.
     */
    {
        NvU64 fw80 = 0x13fe300000ULL;
        NvU64 s1 = 0, s2 = 0, s3 = 0, c0 = 0;

        st = s_cmpSec2ReadFb256(pGpu, fw80 + 0x189700U,
                                wprPhys + CMP_SEC2_META_SRC_OFF);
        memdescFlushCpuCaches(pGpu, pKernelGsp->pWprMetaDescriptor);
        if (st == NV_OK)
            s1 = *(const volatile NvU64 *)
                  ((volatile NvU8 *)(pMeta + CMP_SEC2_META_SRC_OFF / 4) + 0xf0U);

        st = s_cmpSec2ReadFb256(pGpu, fw80 + 0x1aea00U,
                                wprPhys + CMP_SEC2_META_SRC_OFF);
        memdescFlushCpuCaches(pGpu, pKernelGsp->pWprMetaDescriptor);
        if (st == NV_OK)
            s2 = *(const volatile NvU64 *)
                  ((volatile NvU8 *)(pMeta + CMP_SEC2_META_SRC_OFF / 4) + 0x8U);

        st = s_cmpSec2ReadFb256(pGpu, fw80 + 0x1d77a0U,
                                wprPhys + CMP_SEC2_META_SRC_OFF);
        memdescFlushCpuCaches(pGpu, pKernelGsp->pWprMetaDescriptor);
        if (st == NV_OK)
            s3 = *(const volatile NvU64 *)
                  ((volatile NvU8 *)(pMeta + CMP_SEC2_META_SRC_OFF / 4) + 0x8U);

        st = s_cmpSec2ReadFb256(pGpu, fw80 + 0x25e00U,
                                wprPhys + CMP_SEC2_META_SRC_OFF);
        memdescFlushCpuCaches(pGpu, pKernelGsp->pWprMetaDescriptor);
        if (st == NV_OK)
            c0 = *(const volatile NvU64 *)
                  ((volatile NvU8 *)(pMeta + CMP_SEC2_META_SRC_OFF / 4) + 0x68U);

        GPU_REG_WR32(pGpu, CMP_SEC2_FBIF_TRANSCFG0, tcEntry);

        NV_PRINTF(LEVEL_ERROR,
                  "CMP_XBOOT2_80: slot1=%016llx slot2=%016llx slot3=%016llx "
                  "ctrl0=%016llx (st=0x%x)\n",
                  s1, s2, s3, c0, st);
    }
}

void
s_cmpSec2DmaProbePostBooterLoad
(
    OBJGPU *pGpu,
    KernelGsp *pKernelGsp,
    NV_STATUS booterStatus
)
{
    NvU32 probe = 0;
    NvU32 gspDevId;
    KernelSec2 *pKernelSec2;
    NvU64 wprPhys;
    NvU64 dst;
    volatile NvU32 *pMeta;
    NV_STATUS rst, st1, st2;
    NvU32 tcEntry;

    if (booterStatus != NV_OK || pKernelGsp == NULL)
        return;

    gspDevId = pGpu->idInfo.PCIDeviceID >> 16;
    if (gspDevId != 0x2082)
        return;

    (void)osReadRegistryDword(pGpu, "RMCmpSec2DmaProbe", &probe);
    if (probe == 13)
    {
        s_cmpP8NoDump = 1;
        s_cmpSec2DmaProbe8(pGpu, pKernelGsp);
        return;
    }
    s_cmpP8NoDump = 0;
    if (probe == 19)
    {
        s_cmpSec2DmaProbe19(pGpu, pKernelGsp);
        return;
    }
    if (probe == 18)
    {
        s_cmpSec2DmaProbe18(pGpu, pKernelGsp);
        return;
    }
    if (probe == 8)
    {
        s_cmpSec2DmaProbe8(pGpu, pKernelGsp);
        return;
    }
    if (probe == 9)
    {
        /* one-shot ring dump at hook time; also keeps s_cmpSec2RingDump
           referenced from the src/nvidia side so the nv-kernel.o link
           (--gc-sections) does not garbage-collect it */
        s_cmpSec2RingDump();
        return;
    }
    if (probe == 7)
    {
        s_cmpSec2DmaProbe7(pGpu, pKernelGsp);
        return;
    }
    if (probe == 6)
    {
        s_cmpSec2DmaProbe6(pGpu, pKernelGsp);
        return;
    }
    if (probe == 5)
    {
        s_cmpSec2DmaProbe5(pGpu, pKernelGsp);
        return;
    }
    if (probe == 4)
    {
        s_cmpSec2DmaProbe4(pGpu, pKernelGsp);
        return;
    }
    if (probe != 3)
        return;

    if ((pKernelGsp->pWprMeta == NULL) ||
        (pKernelGsp->pWprMetaDescriptor == NULL))
        return;

    pKernelSec2 = GPU_GET_KERNEL_SEC2(pGpu);
    if (pKernelSec2 == NULL)
        return;

    wprPhys = memdescGetPhysAddr(pKernelGsp->pWprMetaDescriptor, AT_GPU, 0);
    pMeta   = (volatile NvU32 *)pKernelGsp->pWprMeta;
    dst     = pKernelGsp->pWprMeta->gspFwOffset + CMP_SEC2_PATCH_BLK_OFF;

    rst = kflcnReset_HAL(pGpu, staticCast(pKernelSec2, KernelFalcon));
    GPU_REG_WR32(pGpu, CMP_SEC2_FBIF_CTL,
                 GPU_REG_RD32(pGpu, CMP_SEC2_FBIF_CTL) | 0x80U);
    GPU_REG_WR32(pGpu, CMP_SEC2_FALCON_DMACTL, 0);
    tcEntry = GPU_REG_RD32(pGpu, CMP_SEC2_FBIF_TRANSCFG0);

    GPU_REG_WR32(pGpu, 0x00841210U, 0x200U);
    GPU_REG_WR32(pGpu, 0x0084111cU, 1U);
    GPU_REG_WR32(pGpu, 0x00841198U, 3U);
    GPU_REG_WR32(pGpu, 0x00841180U, 1U);
    NV_PRINTF(LEVEL_ERROR,
              "CMP_WPR_WR: rst=0x%x en=%08x/%08x/%08x/%08x tcEntry=%08x\n",
              rst,
              GPU_REG_RD32(pGpu, 0x00841210U), GPU_REG_RD32(pGpu, 0x0084111cU),
              GPU_REG_RD32(pGpu, 0x00841198U), GPU_REG_RD32(pGpu, 0x00841180U),
              tcEntry);

    portMemCopy((void *)(pMeta + CMP_SEC2_META_SRC_OFF / 4), 256,
                s_cmpPatchBlock, 256);
    GPU_REG_WR32(pGpu, CMP_SEC2_FBIF_TRANSCFG0, CMP_SEC2_TRANSCFG_SYSMEM);
    st1 = s_cmpSec2HostDma256(pGpu, wprPhys + CMP_SEC2_META_SRC_OFF,
                              CMP_SEC2_DMEM_A, 0x600U);
    NV_PRINTF(LEVEL_ERROR, "CMP_WPR_WR: stage st=0x%x\n", st1);

    GPU_REG_WR32(pGpu, CMP_SEC2_FBIF_TRANSCFG0, CMP_SEC2_TRANSCFG_FB);
    st2 = s_cmpSec2HostDma256(pGpu, dst, CMP_SEC2_DMEM_A, 0x620U);
    NV_PRINTF(LEVEL_ERROR,
              "CMP_WPR_WR: write st=0x%x dst=%llx\n", st2, dst);

    GPU_REG_WR32(pGpu, CMP_SEC2_FBIF_TRANSCFG0, tcEntry);
    NV_PRINTF(LEVEL_ERROR, "CMP_WPR_WR: done\n");
}

"""

GSP_HELPER_ANCHOR = "static void\n_kgspSec2PostblTimingPutU32(NvU8 *pBuffer, NvU32 offset, NvU32 value)\n"

GSP_FWD_ANCHOR = "static NvBool\n_kgspSec2PostblTimingEnabled(OBJGPU *pGpu)\n"
GSP_FWD_DECL = (
    "void s_cmpSec2DmaProbePostBooterLoad(OBJGPU *pGpu, KernelGsp *pKernelGsp,\n"
    "                                     NV_STATUS booterStatus);\n"
    "static void s_cmpSec2DmaProbe10MboxExfil(OBJGPU *pGpu, KernelGsp *pKernelGsp);\n"
    "static void s_cmpSec2DmaProbe14Fbpa(OBJGPU *pGpu, KernelGsp *pKernelGsp);\n"
    "void s_cmpSec2DmaProbe12EarlyRead(OBJGPU *pGpu, KernelGsp *pKernelGsp);\n\n"
)

# v49: gap-window hook — after the PLM loop AND the SS0/SS1/CFG1/LMR
# geometry host writes (80G decode now exists), before the final
# image-reloading BooterLoad. Boot-N FB leftovers at 0x13fe300000 intact here
# (PLM-loop exploit runs take the 0x31 theater path, no image reload).
EARLY_ANCHOR = (
    "        plmStatus = kgspSec2PostblTimingRebuildStockSignature(pGpu, pKernelGsp);\n"
)
EARLY_PATCH = (
    "        s_cmpSec2DmaProbe12EarlyRead(pGpu, pKernelGsp);\n"
    "\n"
    + EARLY_ANCHOR
)

TU102_ANCHOR = (
    "                      GPU_REG_RD32(pGpu, SEC2_DEBUG_PRI_MMU_LMR));\n"
    "        }\n"
    "    }\n"
)

TU102_PATCH = (
    "                      GPU_REG_RD32(pGpu, SEC2_DEBUG_PRI_MMU_LMR));\n"
    "        }\n"
    "    }\n"
    "\n"
    "    s_cmpSec2DmaProbePostBooterLoad(pGpu, pKernelGsp, status);\n"
)

TU102_PRE_ANCHOR = (
    "    // Execute Booter Load\n"
    "    status = kgspExecuteBooterLoad_HAL(pGpu, pKernelGsp,"
)
TU102_PRE_PATCH = TU102_PRE_ANCHOR   # no pre-boot hook in v32

TU102_EXTERN = (
    "void s_cmpSec2DmaProbePostBooterLoad(OBJGPU *pGpu, KernelGsp *pKernelGsp,\n"
    "                                     NV_STATUS booterStatus);\n"
    "\n"
)


def _p8_arrays() -> str:
    parts = [
        ("s_cmpP8Hook1Orig", P8_HOOK1_ORIG),
        ("s_cmpP8Hook1New",  P8_HOOK1_NEW),
        ("s_cmpP8Stub1",     P8_STUB1),
        ("s_cmpP8Stub1Stock", P8_STUB1_STOCK),
        ("s_cmpP8Hook2Orig", P8_HOOK2_ORIG),
        ("s_cmpP8Hook2New",  P8_HOOK2_NEW),
        ("s_cmpP8Stub2",     P8_STUB2),
        ("s_cmpP8Stub2Stock", P8_STUB2_STOCK),
    ]
    out = "".join(
        f"static const NvU8 {name}[{len(b)}] = {{ {_c_array(b)} }};\n"
        for name, b in parts)
    out += (
        f"#define R1_STUB_LEN {R1_STUB_LEN}\n"
        f"#define R1_SLOT_PALO_OFF 0x{R1_SLOT_PALO_OFF:x}U\n"
        f"#define R1_SLOT_SPAN_OFF 0x{R1_SLOT_SPAN_OFF:x}U\n"
        f"#define R1_SLOT_FWBASE_OFF 0x{R1_SLOT_FWBASE_OFF:x}U\n"
        f"static const NvU8 s_cmpR1Stub[R1_STUB_LEN] = {{ {_c_array(R1_STUB)} }};\n"
        f"static const NvU8 s_cmpR1StubStock[R1_STUB_LEN] = {{ {_c_array(R1_STUB_STOCK)} }};\n"
        f"static const NvU8 s_cmpR1HookNew[8] = {{ {_c_array(R1_HOOK_NEW)} }};\n"
        f"static const NvU8 s_cmpR1HookOrig[8] = {{ {_c_array(R1_HOOK_ORIG)} }};\n"
    )
    out += (
        f"#define V61_STUB_LEN {V61_STUB_LEN}\n"
        f"static const NvU8 s_cmpV61Stub[V61_STUB_LEN] = {{ {_c_array(V61_STUB)} }};\n"
        f"static const NvU8 s_cmpV61StubStock[V61_STUB_LEN] = {{ {_c_array(V61_STUB_STOCK)} }};\n"
        f"static const NvU8 s_cmpV61HookNew[8] = {{ {_c_array(V61_HOOK_NEW)} }};\n"
        f"static const NvU8 s_cmpV61HookOrig[8] = {{ {_c_array(V61_HOOK_ORIG)} }};\n"
    )
    return out


PLM_ANCHOR = (
    "                  GPU_REG_RD32(pGpu, 0x001fa7ccU));\n"
    "\n"
    "        {\n"
    "            NvU32 devId = pGpu->idInfo.PCIDeviceID >> 16;\n"
    "            NvU32 cfg1Value;\n"
    "            NvU32 lmrValue;\n"
)

PLM_PATCH = (
    "                  GPU_REG_RD32(pGpu, 0x001fa7ccU));\n"
    "\n"
    "        s_cmpSec2DmaProbe10MboxExfil(pGpu, pKernelGsp);\n"
    "        s_cmpSec2DmaProbe14Fbpa(pGpu, pKernelGsp);\n"
    "\n"
    "        {\n"
    "            NvU32 devId = pGpu->idInfo.PCIDeviceID >> 16;\n"
    "            NvU32 cfg1Value;\n"
    "            NvU32 lmrValue;\n"
)


def patch_kernel_gsp(path: pathlib.Path) -> bool:
    text = path.read_text()
    if "CMP_SEC2_PROBE10" in text and PLM_ANCHOR not in text:
        print(f"{path}: probe10 helper present, PLM hook already installed")
        return True
    if MARK in text and "CMP_SEC2_PROBE10" in text and PLM_PATCH.splitlines()[2] in text:
        print(f"{path}: already patched (incl probe10)")
        return True
    if MARK in text and "CMP_SEC2_PROBE10" in text:
        if PLM_ANCHOR in text:
            text = text.replace(PLM_ANCHOR, PLM_PATCH, 1)
            path.write_text(text)
            print(f"{path}: probe10 PLM hook installed")
            return True
        print(f"{path}: probe10 helper present but PLM anchor missing", file=sys.stderr)
        return False
    if MARK in text:
        print(f"{path}: already patched")
        return True
    if GSP_HELPER_ANCHOR not in text:
        print(f"{path}: helper anchor not found", file=sys.stderr)
        return False
    if GSP_FWD_ANCHOR not in text:
        print(f"{path}: forward-decl anchor not found", file=sys.stderr)
        return False
    helper = GSP_HELPER.replace("/*__P8_ARRAYS__*/", _p8_arrays())
    text = text.replace(GSP_FWD_ANCHOR, GSP_FWD_DECL + GSP_FWD_ANCHOR, 1)
    text = text.replace(GSP_HELPER_ANCHOR, helper + GSP_HELPER_ANCHOR, 1)
    if PLM_ANCHOR in text:
        text = text.replace(PLM_ANCHOR, PLM_PATCH, 1)
    else:
        print(f"{path}: PLM anchor not found (probe10 hook skipped)", file=sys.stderr)
        return False
    if EARLY_ANCHOR in text:
        text = text.replace(EARLY_ANCHOR, EARLY_PATCH, 1)
    else:
        print(f"{path}: early (probe12) anchor not found", file=sys.stderr)
        return False
    path.write_text(text)
    print(f"{path}: SEC2 probe v32-v48 (probe=3..10 + probe12 xboot read)")
    return True


def patch_kernel_gsp_tu102(path: pathlib.Path) -> bool:
    text = path.read_text()
    if "s_cmpSec2DmaProbePostBooterLoad" in text:
        print(f"{path}: already patched")
        return True
    if TU102_ANCHOR not in text:
        print(f"{path}: POST-BooterLoad anchor not found", file=sys.stderr)
        return False
    if text.count(TU102_ANCHOR) != 1:
        print(f"{path}: POST-BooterLoad anchor not unique ({text.count(TU102_ANCHOR)})", file=sys.stderr)
        return False
    if TU102_PRE_ANCHOR not in text:
        print(f"{path}: PRE-BooterLoad anchor not found", file=sys.stderr)
        return False
    define_anchor = "#define SEC2_DEBUG_PRI_MMU_LMR                      0x00100ce0\n"
    if define_anchor in text and TU102_EXTERN.strip() not in text:
        text = text.replace(define_anchor, define_anchor + "\n" + TU102_EXTERN, 1)
    text = text.replace(TU102_PRE_ANCHOR, TU102_PRE_PATCH, 1)
    text = text.replace(TU102_ANCHOR, TU102_PATCH, 1)
    path.write_text(text)
    print(f"{path}: pre/post BooterLoad hooks installed")
    return True


BOOTER_MARK = "kgspExecuteBooterLoadNoReset_TU102"

BOOTER_ANCHOR = (
    "    status = s_executeBooterUcode_TU102(pGpu, pKernelGsp,\n"
    "                                        pKernelGsp->pBooterLoadUcode,\n"
    "                                        staticCast(pKernelSec2, KernelFalcon),\n"
    "                                        mailbox0, mailbox1);\n"
    "    if (status != NV_OK)\n"
    "    {\n"
    "        NV_PRINTF(LEVEL_ERROR, \"failed to execute Booter Load: 0x%x\\n\", status);\n"
    "        return status;\n"
    "    }\n"
    "\n"
    "    return status;\n"
    "}\n"
)

BOOTER_ADD = BOOTER_ANCHOR + r"""
/*
 * probe==5 helper: identical to kgspExecuteBooterLoad_TU102 but WITHOUT the
 * kflcnReset call, so host-programmed DMATRF* registers survive into the
 * booter run (the reset wipes them). Used to let the HS booter continuation
 * trigger a DMA transfer against host-preprogrammed DMA engine state.
 */
NV_STATUS
kgspExecuteBooterLoadNoReset_TU102
(
    OBJGPU *pGpu,
    KernelGsp *pKernelGsp,
    const NvU64 sysmemAddrOfData
)
{
    NV_STATUS status;
    NvU32 mailbox0 = 0, mailbox1 = 0;

    KernelSec2 *pKernelSec2 = GPU_GET_KERNEL_SEC2(pGpu);

    NV_ASSERT_OR_RETURN(pKernelGsp->pBooterLoadUcode != NULL, NV_ERR_INVALID_STATE);

    if (sysmemAddrOfData != 0)
    {
        //
        // sysmemAddrOfData either represents the FW WPR MetaData or the FW SR Data as a physical address in SYSTEM
        // Provide that data in falcon SEC mailboxes 0 (low 32 bits) and 1 (high 32 bits)
        //
        mailbox0 = NvU64_LO32(sysmemAddrOfData);
        mailbox1 = NvU64_HI32(sysmemAddrOfData);
    }

    NV_PRINTF(LEVEL_ERROR,
              "CMP_SEC2_PROBE5: executing Booter Load WITHOUT kflcnReset, sysmemAddrOfData 0x%llx\n",
              sysmemAddrOfData);

    status = s_executeBooterUcode_TU102(pGpu, pKernelGsp,
                                        pKernelGsp->pBooterLoadUcode,
                                        staticCast(pKernelSec2, KernelFalcon),
                                        mailbox0, mailbox1);
    if (status != NV_OK)
    {
        NV_PRINTF(LEVEL_ERROR,
                  "CMP_SEC2_PROBE5: no-reset Booter Load failed: 0x%x\n", status);
        return status;
    }

    return status;
}
"""


def patch_kernel_gsp_booter(path: pathlib.Path) -> bool:
    text = path.read_text()
    if BOOTER_MARK in text:
        print(f"{path}: already patched (no-reset booter load)")
        return True
    if BOOTER_ANCHOR not in text:
        print(f"{path}: kgspExecuteBooterLoad_TU102 anchor not found", file=sys.stderr)
        return False
    if text.count(BOOTER_ANCHOR) != 1:
        print(f"{path}: booter anchor not unique ({text.count(BOOTER_ANCHOR)})", file=sys.stderr)
        return False
    text = text.replace(BOOTER_ANCHOR, BOOTER_ADD, 1)
    path.write_text(text)
    print(f"{path}: kgspExecuteBooterLoadNoReset_TU102 installed")
    return True


OSIF_MARK = "cmpScheduleRingDump"

OSIF_ANCHOR = (
    "NV_STATUS NV_API_CALL os_write_file\n"
    "(\n"
    "    void *pFile,\n"
)

OSIF_BLOCK = r"""
/* cmpunlocker probe==8: delayed ring-dump scheduler.
 * NVRM (kernel_gsp.c) arms s_cmpSec2RingDump() during boot and calls
 * cmpScheduleRingDump(); each work run re-opens the WPR2-collapse
 * channel on the live system and NV_PRINTF's the C1/C2 rings. */
extern void s_cmpSec2RingDump(void);
void cmpScheduleRingDump(void);

static void cmpRingDumpWork(struct work_struct *w);
static DECLARE_DELAYED_WORK(s_cmpRingDumpWork, cmpRingDumpWork);
static int s_cmpRingDumpCount;

void cmpScheduleRingDump(void)
{
    s_cmpRingDumpCount = 0;
    schedule_delayed_work(&s_cmpRingDumpWork, 30 * HZ);
}

static void cmpRingDumpWork(struct work_struct *w)
{
    printk(KERN_ERR "CMP_RING: work fired #%d\n", s_cmpRingDumpCount);
    s_cmpSec2RingDump();
    if (++s_cmpRingDumpCount < 8)
        schedule_delayed_work(&s_cmpRingDumpWork, 30 * HZ);
}

""" + OSIF_ANCHOR


def patch_os_interface(path: pathlib.Path) -> bool:
    text = path.read_text()
    if OSIF_MARK in text:
        print(f"{path}: already patched (ring dump work)")
        return True
    if OSIF_ANCHOR not in text:
        print(f"{path}: os_write_file anchor not found", file=sys.stderr)
        return False
    text = text.replace(OSIF_ANCHOR, OSIF_BLOCK, 1)
    path.write_text(text)
    print(f"{path}: cmpScheduleRingDump delayed-work installed")
    return True


OSIF2_MARK = "os_cmpWritePathFile"

OSIF2_ANCHOR = (
    "NV_STATUS NV_API_CALL os_write_file\n"
    "(\n"
    "    void *pFile,\n"
)

OSIF2_BLOCK = r"""
NV_STATUS NV_API_CALL os_cmpWritePathFile
(
    const char *path,
    NvU8 *pBuffer,
    NvU64 size
)
{
#if NV_FILESYSTEM_ACCESS_AVAILABLE
    struct file *file;
    loff_t pos = 0;
    NV_STATUS status = NV_OK;

    if ((path == NULL) || (pBuffer == NULL) || (size == 0))
        return NV_ERR_INVALID_ARGUMENT;

    if (current->fs == NULL)
        return NV_ERR_OPERATING_SYSTEM;

    file = filp_open(path, O_WRONLY | O_CREAT | O_TRUNC | O_LARGEFILE, 0644);
    if (IS_ERR(file))
        return NV_ERR_OPERATING_SYSTEM;

    if (os_write_file((void *)file, pBuffer, size, 0) != NV_OK)
        status = NV_ERR_OPERATING_SYSTEM;

    os_close_file((void *)file);
    return status;
#else
    return NV_ERR_NOT_SUPPORTED;
#endif
}

""" + OSIF2_ANCHOR


def patch_os_interface2(path: pathlib.Path) -> bool:
    text = path.read_text()
    if OSIF2_MARK in text:
        print(f"{path}: already patched (cmpWritePathFile)")
        return True
    if OSIF2_ANCHOR not in text:
        print(f"{path}: os_write_file anchor not found", file=sys.stderr)
        return False
    path.write_text(text.replace(OSIF2_ANCHOR, OSIF2_BLOCK, 1))
    print(f"{path}: os_cmpWritePathFile installed")
    return True


VBIOS_ANCHOR = (
    "        pVbiosImg->pImage = (NvU8 *) pImageDwords;\n"
    "        pVbiosImg->biosSize = biosSize;\n"
)

VBIOS_CALL = (
    "        s_cmpVbiosPatchHook(pGpu, (NvU8 *)pImageDwords, biosSize);\n"
    "\n"
    + VBIOS_ANCHOR
)

VBIOS_INC_ANCHOR = '#include "published/turing/tu102/dev_ext_devices.h"  // for NV_PROM_DATA\n'
VBIOS_EXTERN = (
    "\nvoid s_cmpVbiosPatchHook(OBJGPU *pGpu, NvU8 *pImage, NvU32 biosSize);\n"
)


def patch_kernel_gsp_vbios(path: pathlib.Path) -> bool:
    text = path.read_text()
    if "s_cmpVbiosPatchHook(pGpu" in text:
        print(f"{path}: already patched (vbios hook)")
        return True
    if VBIOS_ANCHOR not in text:
        print(f"{path}: vbios copy anchor not found", file=sys.stderr)
        return False
    if VBIOS_INC_ANCHOR not in text:
        print(f"{path}: include anchor not found", file=sys.stderr)
        return False
    text = text.replace(VBIOS_INC_ANCHOR, VBIOS_INC_ANCHOR + VBIOS_EXTERN, 1)
    text = text.replace(VBIOS_ANCHOR, VBIOS_CALL, 1)
    path.write_text(text)
    print(f"{path}: vbios patch hook installed")
    return True


def main() -> int:
    if len(sys.argv) < 3:
        print(f"usage: {sys.argv[0]} <kernel_gsp.c> <kernel_gsp_tu102.c>", file=sys.stderr)
        return 2
    gsp_c = pathlib.Path(sys.argv[1])
    ok = patch_kernel_gsp(gsp_c)
    tu102 = pathlib.Path(sys.argv[2])
    ok = patch_kernel_gsp_tu102(tu102) and ok
    booter = tu102.with_name("kernel_gsp_booter_tu102.c")
    if booter.exists():
        ok = patch_kernel_gsp_booter(booter) and ok
    else:
        print(f"{booter}: not found", file=sys.stderr)
        ok = False
    osif = gsp_c.parents[6] / "kernel-open" / "nvidia" / "os-interface.c"
    if osif.exists():
        ok = patch_os_interface(osif) and ok
        ok = patch_os_interface2(osif) and ok
    else:
        print(f"{osif}: not found", file=sys.stderr)
        ok = False
    vbios = gsp_c.parent / "arch" / "turing" / "kernel_gsp_vbios_tu102.c"
    if vbios.exists():
        ok = patch_kernel_gsp_vbios(vbios) and ok
    else:
        print(f"{vbios}: not found", file=sys.stderr)
        ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
