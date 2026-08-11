# FB 地址路由 / 交织配置 RE（GSP-RM 610.43.02, tu10x）

> 2026-08-11。目标：找"按 FB geometry 计算并编程 PA 路由（L2 slice hash / FBHUB swizzle /
> FBPA 交织）"的固件代码，评估 host 侧可修的寄存器。工具：`re2/disasm.txt`（全量反汇编）、
> `re2/xref.py`、`re2/strxref.py`。
>
> **结论先行：PA 折叠配置不在 GSP-RM 里。** 全套反汇编证据表明 GSP-RM 610.43.02 从不编程
> 任何 geometry 相关的路由/哈希/密度寄存器；它是 DevInit（VBIOS 脚本）+ FBFLCN + fuse/strap
> 的地盘。修复路径应走"host BAR0 补齐 DevInit 差量寄存器"（候选清单在 §4），不需要 GSP 代码补丁。

## 1. GSP-RM 的 geometry 输入与去向

- **fbSize 不来自 LMR**：全固件没有 0x100ce0 常量（code/data 均无）——GSP-RM 不读 LMR。
  fbSize 由 **host 算好经 WPR meta 传入**（与开源 `kgspPopulateWprMeta` 一致）。
- GSP 侧落点：regkey 初始化函数 **0x50c80c8**（读 `OverrideFbSize` 等一串 RM key，
  字符串绝对地址 xref 定位）：
  - 入口 `ld a2, 0x638(a1); slli a2, a2, 0x14`：fbSize(MB) → 字节;
  - `OverrideFbSize` 读到后 `value<<20` 存 `obj+0xa28`;
  - fbSize 字节值作为参数传入 **0x50e26cc**（heap/PMA region 初始化：0x30 字节步长的
    region 记录循环、reserved 区域扣除、limit 对齐）。
- **fbSize 的全部去向 = heap/PMA 尺寸计算**。没有任何一条路径把 fbSize（或其移位/对数）
  写进路由寄存器。早前 RE 的负面结论（全固件无 0xA00000000/0x800000000/0x1400000000
  常量）本轮复核成立。

## 2. GSP-RM 对 FB 配置空间的全部写入（完整清单，均为操作性/特性位）

扫描方法：所有 `sw`，追踪 `lui+addi` 地址链到 bus 偏移，全量人工归类。

- **PFB_PRI_MMU 块 (0x100cxx)**：0x100c14、0x100c80、0x100cac、0x100cb0、0x100cb8、
  0x100cbc、0x100cc0、0x100cc4、0x100cc8、0x100ccc —— MMU invalidate/enable 类 RMW。
- **0x100exx 块**：0x100e68/0x100e6c/0x100e74/0x100e78/0x100e80/0x100e94/0x100ec0/
  0x100ec4/0x100ec8/0x100ecc/0x100ed0/0x100ed4/0x100ef8 —— 全部是 `ori` 特性位置位或
  清零（如 0x51bc2a8 按特性标志 `ori 0x10`），**无一个值由 geometry 算出**。
- **FBPA 空间 (0x9a0xxx+i*0x1000)**：读多为状态轮询（0x144/0x170/0x350/0x38c/0x470/
  0x52c/0x974 等）；写只有 0x9a2574、0x9a407c、0x9a4408 三处（特性级）。
  **没有 CFG1(0x204)、CONFIG4(0x2a0)、0x220、0x294/0x298/0x29c、0x39c 的任何读写。**
- **LTC 窗口 (bcast 0x140000 / per-instance 0x17e000)**：**零写入**（lui 0x140/0x17e 的
  几处全部是返回 engine-descriptor 常量的 HAL helper 或地址范围检查）。
- 无任何"连续寄存器哈希表"写循环指向硬件块；memsys CU 里仅有的两个除法魔数
  （0x506aa30、0x50703c4 的 0xCCCCCCCC 序列）实为**无分支 log2/对齐习语**，
  结果喂给软件资源表（0x506a7d8 纯内存表操作），不写硬件。

## 3. 折叠机理推断（与全部实测吻合）

- 折叠点 40G = **20 FBPA × 2G** = A100-40G 的每分区深度；CMP-8G（16 FBPA）能到 64G
  = 4G/FBPA = A100-80G 的每分区深度。两张 CMP 卡 strap-4 同为 0x44，行为却不同
  ⇒ 差异不在 strap 字节本身，而在 **VBIOS DevInit 脚本编程的每分区 HBM 地址深度配置**。
- 我们的运行时补丁（CFG1=0x02779000、LMR=0x028b）把可寻址从 10G 抬到 40G，说明
  CFG1/LMR 参与了 depth 计算；但还差最后 1 bit（2G→4G/FBPA），这个 bit 的配置
  由 DevInit 写进 per-partition FBPA 寄存器，硬件直接消费，GSP-RM 碰不到。
- tail 签名（0x900/0xB800，分段边界 7.5G，256B 粒度）与"HBM 地址 swizzle 在一个
  被截断的地址图上回绕"一致：不是干净的 bit 掩码，而是列/行哈希输入溢出。
- 排除项复核：A100-80G 同为 20 FBPA 且正常工作 ⇒ mod-20 非 2 幂交织本身无 40G 上限，
  不是硬件天花板，是配置问题。

## 4. Host 侧实验（不需要 GSP 补丁；FBPA PLM 已被 R3 打开）

候选寄存器 = **A100-40G↔80G VBIOS DevInit 差量**（早前 VBIOS diff 已列出偏移）：

| 寄存器 (per-partition: 0x9a0000 + i*0x1000 + off) | 备注 |
|---|---|
| 0x9a0200 (CFG0) | 两 VBIOS 差量之一 |
| **0x9a0220** | 差量，高度疑似 density/address-map |
| **0x9a0294 / 0x9a0298 / 0x9a029c** | **只在 40G VBIOS 里出现** —— 40G→80G 的删除/变更项 |
| **0x9a039c** | 差量 |

实验顺序（每步一次冷启动+wall_reconfirm2 single 60/72 验收）：

1. **三方 dump 对比**：CMP-10G（当前 80G 解锁态）、CMP-8G（64G 正常态）、真 A100-80G，
   读上表寄存器的 BCAST 值（0x9a0xxx，必要时逐分区 0x9a0000+i*0x1000 验证一致）。
   金标准 = "CMP-8G 与 A100-80G 一致而 CMP-10G 不同"的寄存器。
2. 把 A100-80G 的值写进 CMP-10G（host BAR0，PLM 已开），复测墙。
   观测：折叠点移动/消失（wall_reconfirm2 污染范围变化）= 命中。
3. **若全部一致** ⇒ 深度状态在 FBFLCN/HBM 控制器内部（MMIO 不可见），
   修复路径只剩：(a) 触发 HBM link re-init/re-train（代价未知），
   (b) VBIOS DevInit 表修正（MAC 签名问题，早前评估过），(c) 接受 40G 有效墙。

补充判别实验（不写寄存器）：读 CMP-10G 的 0x9a0220/0x9a039c 等后，**热态**
（不冷启动）写成 A100 值再跑 vec_scan2 48——若行为立即变化，说明这些寄存器是
运行时生效的；若无变化但冷启动后生效，说明只在 init 时被消费。

## 5. 对 GSP 补丁线的结论

- map 链（dmaUpdateVASpace 等）放弃维持不变（v52 绊线 + 子页 tail 双重否定）。
- GSP-RM 内无 PA 路由配置可打 —— **FB 路由线也不需要在 GSP 固件里找补丁点**。
- 剩余固件侧线索（如需继续）：FBFLCN ucode（HBM init/training）与 VBIOS DevInit
  IEP 脚本（GA100 压缩格式，envytools 解不了码 —— 已知障碍）。

## 6. CMP-10G 实测 dump(2026-08-11,80G 解锁态，驱动 610.43.02)

工具：`~/f0/mmio_list` / `mmio_dump3d`(BAR0 resource0 mmap,BDF 0000:3d:00.0)。
整页存档：`gsp_analysis/re2/fbpa_dump_10g.txt`(0x9a0000–0x9a03ff + LMR/SS0/SS1)。

| 寄存器 | CMP-10G 实测 | 备注 |
|---|---|---|
| 0x9a0200 (CFG0) | `0x07981800` | 候选 |
| 0x9a0204 (CFG1) | `0x02779000` | 与 A100-80G 一致（运行时补丁值） |
| 0x9a0220 | `0x0801900c` | **候选（疑 density/addr-map)** |
| 0x9a0294 | `0x38d4841b` | **候选（40G VBIOS 独有；值形似 hash 多项式）** |
| 0x9a0298 | `0x88130b11` | 同上 |
| 0x9a029c | `0x24002b4a` | 同上 |
| 0x9a02a0 (CONFIG4) | `0xc4030033` | 20-FBPA 值，写保护（B6 已知） |
| 0x9a039c | `0x00000003` | 候选 |
| 0x100ce0 (LMR) | `0x0000028b` | = 80 GiB，正确 |

per-partition 寻址：`0x9a0000+i*0x1000` 不是分区窗口（读出
`0x0007fff0`/`0xbadf1002`/`0xbadf1100` 错误码，与 HANDOFF 记载的
8G 卡完全相同）——只有 BCAST 可读，对比以 BCAST 为准。

**注意：A100-80G dump(`~/Downloads/a100-80g.json`,19 项）不含上述候选
寄存器，金标准只能靠 CMP-8G 卡（64G 正常 = 4G/FBPA 深度）对比。**

顺手发现：当前启动 SS0/SS1 = `0x88888888`/`0x00000008`(R3 PLM 循环的
调试残留，B4 修复未生效），且驱动装载的是 8 月 8 日带 CMP_PTE_VA/
CMP_PT_ALLOC 日志的仪表 build —— 算力可能处于未修复状态，正式验收前
需要换回干净 build 复核。

## 7. CMP-8G 对比 dump(2026-08-11,64G 解锁态，VBIOS 92.00.6d.00.0a)

存档：`gsp_analysis/re2/fbpa_dump_8g.txt`、`ltc_fbpa_ext_8g.txt`。
挥发性检查（同卡连读两次）:**只有 0x9a0210 是动态状态寄存器**
(0x80005555→0x80005e5e，逐 nibble 变化，疑 HBM 温度/training 状态），
其余全部稳定 —— diff 里除 0x210 外均为真实配置差异。

### diff 分级（10G vs 8G，已剔除 0x210)

**A 级 —— 深度杠杆嫌疑（单 bit / 单 nibble / 整倍数关系）:**

| 寄存器 | 10G | 8G | xor | 疑点 |
|---|---|---|---|---|
| 0x9a0164 | 0xa | 0x8 | 0x2 | 10 vs 8(HBM site 数 ×2?) |
| 0x9a016c | 0x14 | 0x10 | 0x4 | **恰为 20 vs 16 = FBPA 数**，随 CONFIG4 |
| 0x9a0224 | 0x12050d12 | 0x120a0d12 | 0xf0000 | nibble 5→a(**×2**) |
| 0x9a0248 | 0x0a267444 | 0x0a2c7444 | 0xa0000 | nibble 6→c(**×2**) |
| 0x9a0250 | 0x0bb800a1 | 0x0bb800b1 | 0x10 | **单 bit** |
| 0x9a02c8 | 0x27380101 | 0x29380101 | 0xe000000 | nibble 7→9 |
| 0x9a02d8 | 0x160e1024 | 0x180e1024 | 0xe000000 | nibble 6→8 |
| 0x9a02f4 | 0x10 | 0x11 | 0x1 | **单 bit** |
| 0x9a03e4/e8 | 6 / 0x28000006 | 5 / 0x28000005 | 0x3 | 同字段镜像 |

**B 级 —— RE 原候选（40G-VBIOS 独有 DevInit 项，多 nibble 差异，形似
swizzle/hash 表）:** 0x9a0290、**0x9a0294 / 0x9a0298 / 0x9a029c**。
0x298/0x29c 两卡结构高度相似（仅低位字段不同）⇒ 内容随 SKU 几何缩放。

**C 级 —— 疑 HBM 时序/refresh（跟随不同 HBM 料号，与深度无关概率大）:**
0x9a0254(≈×1.5)、0x9a0288、0x9a02b0–0x9a02e4 九个打包表、
0x9a0330–0x9a033c、0x9a0394/0x9a03f8。

**已知排除:** 0x9a0200/0x9a0220/0x9a039c 两卡相同（不再是候选）;
CONFIG4 0x9a02a0(0xc4030033 vs 0xc4028033)= 20/16 FBPA 判别，写保护。

### 热写实验阶梯（换回 10G 卡后执行，无需冷启动，wedged 则 FLR)

0. **可写性探测**:mmio_list 写后门——先写原值再写变体读回。
   写一个 mmio_write 工具（当前只有读）。
1. A 级逐个热写 8G 值（每次一个）→ wall 快速探针（见下）→ 观察折叠点
   移动/消失。优先级：0x250（单 bit)> 0x2f4（单 bit)> 0x224/0x248(×2
   nibble)> 0x2c8/0x2d8 > 0x164/0x16c > 0x3e4/0x3e8。
2. B 级 0x294/298/29c 整组热写（swizzle 表通常要一起换）。
3. 全部无感 ⇒ 状态在 FBFLCN/HBM 控制器内部，转入 VBIOS DevInit 修正线
   (MAC 签名障碍）或接受 40G 有效墙。
4. 任何"折叠点动了"的命中 → 该寄存器即杠杆；再解 20-FBPA/4G 深度的
   正确值（8G 值是 16-FBPA 的，只能当探针不能当解药）。

**wall 快速探针（替代 2.5 分钟的 cross48)**：小对象定点测试——
分配 8G,cudaMemcpy 写 logical [4G,5G)（洞上段）,host 回读 logical
[0,1G) 是否被污染 + [4G,5G) 自读是否自洽。~20 秒出结果。
需要给 wall_reconfirm 加 `probe` 模式（测试 agent 可协助）。

## 8. 热写实验结果（2026-08-11,10G 卡）：全军覆没

基线：`wall_reconfirm2 single 40` 坏桶 [0,5G),bad_qwords=617,349,120
（可复现，40G 对象即可探墙，不用 cross48)。

**全部 14 个候选寄存器写拒绝（REJECTED，读回=原值）:**
0x9a0250、0x9a02f4、0x9a0224、0x9a0248、0x9a02c8、0x9a02d8、0x9a0164、
0x9a016c、0x9a03e4、0x9a03e8、0x9a0290、0x9a0294、0x9a0298、0x9a029c。

- 注意：写**原值**会假 STICKS（自写无判别力），写**不同值**全部
  REJECTED ⇒ 整个 FBPA 配置空间对 host PL0 只读。
- 0x9a0148(FBPA PLM)= 0xffffffff 已全开；**0x9a014c = 0xffffff8f
  且自身写拒绝**——这扇门在我们的 PLM 权限之下。
- 每分区窗口探测全灭：0x9a4000+/0x9b/0x9c/0x9d/0x9e/0x9f 各基址
  均返回 0xbadf**** 错误码，host 侧无 per-partition 通道。
- 与 B6(CONFIG4 静默拒绝）、HANDOFF 的 FUSE_EN_SW_OVERRIDE=0
  警告一致：**FBPA 配置是 fuse/DevInit 级锁定**。

### 判别实验（下一步）：SEC2 HS payload 写 FBPA

host PL0 已证无解。唯一未试的写路径 = 我们的 **SEC2 booter HS payload**
(LEVEL2，驱动 init 时 BooterLoad 阶段执行，目前用于开 PLM，已证实能写
PLM 寄存器）。判别方法：payload 里加一条写 0x9a0250=0x0bb800b1
（或 0x9a016c=0x10)，启动后 host 读回：

- 值变了 ⇒ LEVEL2 能写 FBPA,**修复路径打通**（在 payload 里写全套
  深度配置，再解决"正确值是什么")。
- 值没变 ⇒ fuse/source-ID 锁定（同论文 HBM MRS 困境：FB/HBM 侧只认
  FBFLCN 的命令），剩余路径只有 VBIOS strap-4 修正（MAC 签名障碍）
  或接受 40G 有效墙。

时序风险：若 VBIOS DevInit 在同一 RM init 内、我们的 payload 之后执行，
写入会被 DevInit 覆盖——判别实验结果为"没变"时需先排除这个再下结论。
EOF
