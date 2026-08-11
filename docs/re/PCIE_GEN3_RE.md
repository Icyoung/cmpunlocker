# PCIE_GEN3_RE — PCIe Gen3 解锁的编码位置判定

> 2026-08-11。纯本地 RE。材料:`/tmp/cmp_vbios.rom`(CMP-10G,在位)、283224.rom /
> NVIDIA.A100.40960.200214.rom / 282151.rom、`gsp_rm_tu10x.elf` 全量反汇编。
>
> **判定:(c) —— Gen3 既不在 devinit 明文表,也不在 GSP-RM 可见代码的条件分支里;
> 拦点在 fuse + PHY 校准层(加密/压缩区),与显存墙同属"固件补丁够不着"一类。**

## 1. 表内搜索(0x8xxxx XP 写行)— 阴性

- 明文 per-partition 表(23 行 × 16 dword)全部 23 行经运行值碰撞均为 **FBPA
  (0x9a0xxx) 值**,无任何 XP/PCIe 寄存器行(A100_40G_80G_COLUMN_DIFF.md 已全量逐单元对比)。
- 压缩 IEP 脚本扫描:`d9 <addr32>` 绝对写 opcode,全 ROM 范围内
  **0x88000–0x90000 零命中**(仅 0xe5089/0xe508e/0xf1d89 三处区外命中,
  CMP 侧只有 0x9a033c 一处 FBPA 写——与 agent-6 的既有结论一致)。
- XP 基址(0x88000/0x8a000/0x8c000)作为数据在全部 4 个 ROM 中零命中。

## 2. 表外搜索(strap/fuse shadow/BIT/明文段)— 阴性

- 四个 ROM 全文搜索 `xp3g/XP3G/gen3/Gen3/GEN3/pcie/PCIE/uphy/UPHY` —— **全零命中**
  (VBIOS 里没有任何明文 PCIe/UPHY 配置表或调试段;282151 ES 版同样没有)。
- CMP 与 A100 的明文区差异早前已全量盘点(0xc2000 等区域 = RM 消息目录,
  VBIOS_ECC_BBX.md);init 表区 (0x3f000–0x42000 等) 逐字节相同。
  ⇒ PCIe 配置不在任何明文数据里;它要么在压缩 IEP 段(不可解码),
  要么在签名/加密的 devinit 表与 UPHY 配置里。

## 3. GSP-RM 侧 — regkey 流向与 Gen3 拦点

- **`RmForceEnableGen2`(file 0x81230)在 GSP-RM 里没有任何代码 xref** ——
  它是 host 侧(CPU RM)消费的 regkey,固件不读。
- `RMPcieLinkSpeed` 的 4 个读取方(0x4c96a88、0x4ca3214、0x4cad8bc、0x4cadebc)
  全是**能力解码/校验**逻辑:读 obj+0xa40 的 2-bit 能力域逐一比对(==1/==2),
  尾段从 pGpu 缓存结构读 4-bit max-speed 字段(0x4cad9c0 一带,
  `lw 0x32c(...); andi 0xf`)——**这是 PCIe LNKCAP 风格的"最大链路速率"字段,
  其来源是硬件/复位时的能力寄存器,本身是 fuse/strap 驱动的**。
  regkey 只在这之下做钳制(min/max),不产生能力。
- 全固件**不存在**"if (fuse) 禁 Gen3"或"Gen3 能力开/关"的条件分支;
  没有任何代码把速率上限写成可改的表/常量。
- 旁证 regkey 群(RMPcieStickyGenSpeed、RMPcieGenSwitchOnPmu、
  RmForceEnablePcieGenSwitching)全部围绕"运行时 Gen 切换(pstate)",
  不是能力解锁。

## 4. 判定 (c) 与含义

1. Gen3 的拦点与论文结论一致:**fuse(FUSE_PCIE_GEN23_DIS 族)+ PHY 校准**。
   Gen2 能被 fuse-shadow override 解开,是因为 Gen2 的 PHY 配置仍在;
   Gen3(8 GT/s)需要不同的 TX de-emphasis / RX EQ 校准数据,该数据/路径
   在此 SKU 上被 fuse 关掉 —— **没有软件表可补**。
2. 即使 Gen3 校准数据藏在 VBIOS 里,它也只在**压缩 IEP 段或签名 devinit 表**中
   (明文区已穷尽)——与显存墙的 FWSEC 认证同一堵墙;R1 窗口若不成立,
   这条也同样无解。
3. 物理层附加说明:用户链路是雷电三 x4;即便 Gen3 解锁,TB3 的
   实际吞吐上限 ~22 Gbps 数据 —— Gen2 x4(16 GT/s 有效 ~14 Gbps after overhead)
   与 Gen3 x4(~28 Gbps,被 TB3 钳到 ~22)差距有限。**投入产出建议:
   Gen3 与显存墙同等待遇——R1 窗口探针若死,两线一起收尾。**

## 附:已排除清单(供后人不重复扫)

| 位置 | 结果 |
|---|---|
| 明文表 23 行 | 全 FBPA,无 XP 行 |
| IEP `d9` 绝对写 | 0x88xxx–0x90xxx 零命中 |
| 4 ROM 的 PCIe/UPHY/gen3 字符串 | 零命中 |
| XP 基址常量(0x88000/0x8a000/0x8c000) | 零命中 |
| GSP-RM RmForceEnableGen2 消费方 | 不存在(host 侧 regkey) |
| GSP-RM Gen3 条件分支/fuse 检查 | 不存在(能力来自硬件 LNKCAP 缓存) |
