# VBIOS ECC/BBX 数据表解析(283224=A100-80G vs 282161=GRID A100A-32G)

> 2026-08-11。材料:`~/Downloads/283224.rom`、`~/Downloads/282161.rom`。
> **结论先行:0xc2000–0xd2000(以及 0xd3000/0xd7000/0xe6000 各差异块)不是 HBM
> 拓扑/密度表,是 RM 日志消息目录(message catalog)。几何字段扫描为阴性。**
> 墙的密度配置不在这条 plaintext 线上。

## 1. 记录格式(已解,校验自洽)

区域由 **0x80 字节对齐的槽位**组成,每槽 = 9 字节头 + 至 0x10 对齐的 payload(≤0x70 字节):

```
+0x00  u8    type      ∈ {0x04, 0x06}(含义未定,像格式/类别版本)
+0x01  char  sig[3]    子系统签名:ECC BBX NVL RRL DEM RPR APP OCT PPO DED
+0x04  u16le f45       序列偏移:同一 (sig, f67) 子序列内严格 +0x70 递增
+0x06  u16le f67       = (子表索引 << 8) | 0x70(0x70 = 最大 payload 尺寸)
+0x08  u8    b8        校验位:全槽 0x80 字节累加和 & 0xff == 0xfe(type 04)
                       / == 0x00(type 06)——两个 ROM 全区域验证通过
+0x09..0x0f            零填充
+0x10..0x7f            payload(消息字符串/索引表;80G 侧大量槽位为全零)
```

校验示例(80G 0xc2000):sum = 0xfe ✓;32G 0xc2580(带 payload):全槽 sum = 0xfe ✓。

区域头在 **0xc0e00**:`"NV"` 签名 + **NPDS/NPDE** 段标记(0xc0e20/0xc0e40)。
NPDS 里的一个 dword 两 ROM 不同:80G = **0x199**,32G = **0x1f1** —— 疑似记录数
(409 vs 497),与 32G 侧目录更密一致。

## 2. 内容判定:RM 消息目录

payload 里是**标签前缀的 ASCII 消息模板/实例 + u32 偏移/索引表**:

- 80G 侧明文可直接读到的消息:"An uncorrectable double bit error (DBE)...",
  "**HBM, Uncorrectable DRAM error in FBPA 6 subpartition 0 physAddr 0x13...**",
  "Row Remapper Error: (0x00000013fe7fbde0) - Attempting to remap...",
  "GPU recovery action changed from 0x0 (None) to 0x4 (Drain and Re...)",
  "channel 0x0000000e..."、"Graphics SM Warp Exception on (GPC 2, TPC 2, SM 0)...",
  "Out Of Range Address..."等。
- 32G 侧相同区域是全填充的打包形式(熵更高,字符串带 4 字节标签前缀,如
  `43 91 f5 65 "Graphics SM ..."`),另有大段 u32 表(递增序列 = 字符串偏移表;
  如 0xc2710 起:0x171f, 0x199e, 0x8fab, 0xa6a0, 0x135b7, 0x51ecc, ...)。
- 签名 ↔ 子系统:ECC=ECC 错误消息;RPR=repair/Row-Remapper 消息;NVL=NVLink;
  DEM=demote/DRAM 事件类;BBX=black-box(故障记录)类;DED=DED 内存测试;
  RRL/APP/OCT/PPO 同类。**全部是"日志说什么",不是"硬件怎么配"。**

## 3. 80G vs 32G 差异实质

- 同一目录模式、同一校验规则;差异是**目录内容与布局**:
  32G 侧 payload 几乎全满(打包紧凑);80G 侧大量槽位全零、字符串散布在不同槽位,
  f67 子表索引分配也不同(80G 见 0x0070..0x2c70;32G 集中在 0x0070/0x0170/0x0270)。
- 入口 sig 差异(32G 首条 BBX、80G 首条 ECC)只是目录排序不同,不是"表类型变了"。
- 这是**同一 build 不同 SKU 配置产出的消息目录差**(收录的消息集合/顺序不同),
  不含硬件配置语义。

## 4. 几何字段扫描(阴性)

对全部差异块(0xc2000–0xd2000、0xd3000–0xd5000、0xd7000–0xda000、0xe6000–0xe7000)
扫描 LE u32/u64 常量:

- 8G(0x200000000)/4G/2G/1G/32G/40G/80G 的命中全部是**稀疏索引结构里的小整数**
  (8、10、20 之类,count/索引字段),无一处像尺寸/深度字段。
  例:80G ROM 0xd8950 的 "0x1400000000" 实为结构里的 0x14(=20,像 FBPA 计数);
  32G ROM 0xce308 的 "0xa00000000" 实为 10。
- 小整数(5/6/12/13/14/20/24/0x44/0x66)出现次数两侧同量级,无行位宽
  (12/13/14)或 strap tier(0x44/0x66)的可疑聚集。
- 唯一带点"几何味"的是消息**样例字符串**里的烘焙值("FBPA 6"、
  Row-Remapper 样例地址 0x13fe7fbde0 ≈ 75G)——是 80G 卡日志的示例渲染,不是配置。

## 5. 对 40G 墙修复线的含义

1. **plaintext 表线到此为止**:差异区是消息目录;共享明文初始化表(0x40000 区域)
   两 ROM 逐字节相同(VBIOS_ROMS.md 已记)。HBM 深度/密度配置不在任何明文数据表里。
2. 配置差异只可能藏在:**DevInit 压缩字节码的"按 fuse/strap 选行"逻辑**
   (IEP/FWSEC 路径,无社区解码器)——即同一张共享表,脚本决定 80G 选哪几行。
3. 因此推进手段不变:以硬件 oracle(wall_reconfirm2 / vec_scan2)+ LEVEL2 HS 写,
   直接对共享明文表里的候选行/值做二分验证(agent-3 的 v55 线);
   本目录解析不再投入。
