# GSP-RM 固件逆向:32G PTE 混叠 bug — 第一轮分析报告

> 2026-08-08,子任务:在 `gsp_analysis/gsp_rm.elf` 中定位"洞上物理页(PA≥44G)的
> PTE 被额外写入 VA−32G 槽位"的固件代码。本轮为纯静态分析,未修改任何项目文件。
> **结论先行:确切的肇事指令尚未钉死,但 (1) 排除了"固件内残留 40G/32G 常量"
> 假设(全二进制扫描零命中),(2) 把算术特征钉死为"bit 35 丢失"(2^35 掩码),
> (3) 建立了完整的固件函数地标表和 xref 工具链,(4) 给出两个候选机理模型及
> 判别实验。**

## 1. 二进制基本信息(实测)

- `gsp_rm.elf`:ELF64 RISC-V RV64(软浮点,RVC),ET_EXEC,静态链接,stripped。
- Program headers(section header 表确实被清零,shoff=0x106c098 处 8 项全 0):

  | 段 | file off | vaddr | 大小 | 属性 |
  |---|---|---|---|---|
  | PH0 | 0x0 | 0x1000000 | 0xe97000 | R-X(.text + rodata 同段,字符串在 VA 0x1d8xxxx 一带) |
  | PH1 | 0xe97000 | 0x4000000 | 0x1d5000 | RW-(.data/.bss) |
  | PH2/PH3 | — | 0x203c5000 / 0x20000000 | — | TLS/自定义 |

- 入口 0x1a9ba00。**file offset = vaddr − 0x1000000**(text 段)。
- 全量线性反汇编已完成(capstone 5.0.7,RISCV64+RISCVC,skipdata):
  543.7 万条指令。产物在 `/tmp/gsp_re/`(见 §8,易失,附再生成方法)。
- 函数起点索引(prologue 启发式):24473 个。

## 2. 已排除的路径(重要负面结果)

### 2.1 固件内没有任何 40G/32G/80G 常量 —— "残留尺寸常量"假设证伪

对全二进制做了两轮扫描:

- **指令级常量传播**(跟踪 li/lui/addi/addiw/slli/srli/add/sub 序列,控制流边界清空):
  目标值 0xA00000000 / 0x800000000 / 0x1400000000 / 各 ±1 掩码 / 页数形式
  (0x5000=40G/2M 等)**零命中**。仅有的 0x8000000 级命中全是 `lui x, 0x800`
  (=8MB,页表/PTE 代码里的常见量)之类的无关值。
- **数据级 qword 扫描**(rodata + data 段直接搜小端字节):所有疑似命中逐一核
  查后确认全是误报——0xc0b2xx 一带的 "0xA00000000" 实为 NVOC 属性描述符数组
  (24 字节/项的小整数结构体)中相邻字段拼出的字节模式。

**推论:几何全部是运行时数据(经 LMR/CFG1 → GSP boot 自检 → static info 流入),
bug 不是"忘了改的 40G 立即数",而是某个推导/位宽缺陷在 80G 几何下才暴露。**
`fb_length>>1` 形式的推导若存在,是对运行时变量右移,无法用常量扫描找到。

### 2.2 walk 路径的 NV_PRINTF 字符串全部被编译裁剪

`mmuWalkMap` 的 "Failed to map VA Range..."、`_gmmuWalkCBMapNextEntries_RmAperture`
的 "[GPU%u]: PA 0x%llX, Entries..."、`dmaUpdateVASpace` 的 "can't update VA
space..." 在固件字符串表中**均不存在**。字符串 XREF 无法直接锚定建表代码;
只能靠注册表名(osReadRegistry 的 key 字符串都在)和结构特征锚定。

### 2.3 phantom dump 内容判读

- `phantom_37g.bin`(64MB @37G):全 16384 页非零,内容是**大位图**(绝大多数
  qword ≈ 全 1,稀疏清位)——是池/PMA 的分配位图类元数据,**不是页表内容**。
- `phantom_7g.bin`、`phantom_ctrl.bin`、`vram_9g.bin`:按"连续 8+ 项步进
  0x200000 的有效 PTE"模式扫描,零命中。本地 dump 里没有客户端 PT 页,
  无法离线区分"双写 vs PDE 共享"(见 §4)。

## 3. 机理推导:算术特征钉死为"bit 35 丢失"

实测经验定律(docs/RESEARCH_REPORT_20260808.md):PA≥44G(洞 [36G,44G) 之上)
的 2MB 页,PTE 落到 VA−32G 槽位,偏移**恒定** 32G。

- −32G = −2^35。等价表述:字节地址清 bit 35;2MB 页索引减 0x4000(清 bit 14);
  1G 页表页索引减 32(清 bit 5)。
- **可以排除 NvU32(32-bit)截断**:mod-4G 的截断在受害带 [3.4G,16G) 对应的源
  带 [35.4G,48G) 上会给出 −32G/−36G/−40G/−44G 四种不同偏移(每过 4G 边界变
  一次),与实测"恒定 32G"矛盾。**唯一干净的解释是 35-bit 掩码/字段**
  (x & 0x7FFFFFFFF),或等价的"索引位宽少 1 bit"。
- 触发阈值本身无法从现有数据区分:PA≥36G / PA≥40G(=fb_length>>1)/ PA≥44G /
  "物理不连续点之后的第二段"全部等价(洞 [36G,44G) 内无页)。

### "两处都有正确 PTE 值"的约束 → 两个候选模型

观测:VA x+32G(高)翻译正常,VA x(低)读到高处的内容 ⇒ 高槽位写对了,
低槽位被高处页的 PTE 覆盖。满足全部观测的结构模型有两个:

- **模型 A(双写)**:walk/传输路径对每个洞上页把同一 PTE 值写两次,第二次的
  目标槽位按 VA−32G 计算。要求存在一条"重放/补写"路径,其槽位推导丢 bit 35。
- **模型 B(PT 页/PDE 共享,单写即可)**:`[x, x+1G)` 与 `[x+32G, x+33G)` 两个
  VA 区间的 **PDE 指向同一个物理 PT 页**(2MB 叶级一页盖 1G VA,两级索引恰好
  差 32)。填高区时覆盖共享页 ⇒ 低 VA 全部读到高处内容,"高者胜"。
  只需要 PT 页分配/实例查找的 key 丢 bit 35(或实例缓存 mod-32 混叠)。
  **模型 B 更经济**(一次写解释一切),且与 walk 的 level-instance btree
  (mmu_walk.c:1127-1155,btreeSearch/btreeInsert by VA)或页表池分配器吻合。

判别实验(需上机,一次冷启动内可做完):用 `NV0080_CTRL_CMD_DMA_PTE_INFO`
(kgmmuExtractPteInfo_IMPL,host 可发)分别查询 VA x 与 VA x+32G 的 PTE;
再用 f0_phantom_peek 思路物理读两处的 PDE:
- 若两个 PDE 的 PT 页物理地址**相同** → 模型 B(查池分配/实例 btree);
- 若不同且两 PT 页同 index 处确为同一 PTE 值 → 模型 A(查传输重放路径)。

方向 C 实验(文档已列:洞挪到 [44G,52G) 看偏移变 40G 还是仍 32G)进一步区分
"常量 2^35"与"洞起点−4G"。两个实验正交,建议都做。

## 4. 源码路径梳理(host 同源,610.43.02)

客户端大映射全程在 GSP 内(已实锤 host 无 mmuWalkMap 调用):

```
host: NV_RM_RPC_MAP_MEMORY_DMA (virtual_mem.c:1508, rpcMapMemoryDma_HAL)
      → RPC 只带 handle + offset/length/dmaOffset,页数组在 GSP 侧内存对象里
GSP : (≈dmaMapMemory) → dmaUpdateVASpace_GF100 (virt_mem_allocator_gm107.c:2129)
      → gvaspaceMap_IMPL (gpu_vaspace.c:1981) → mmuWalkMap (mmu_walk_map.c:46)
      → _mmuWalkMap → pTarget->MapNextEntries
      = _gmmuWalkCBMapNextEntries_RmAperture (virt_mem_allocator_gm107.c:1979)
        · surf.offset = entryIndexLo * entrySize        (槽位内偏移)
        · memmgrMemBeginTransfer(..., SHADOW_ALLOC|SHADOW_INIT_MEM = 0x6)
        · _gmmuWalkCBMapNextEntries_Direct (:1769,逐页编码 PTE 进 shadow)
        · memmgrMemEndTransfer → GSP 侧 DMA 写 PT 页(目标 = pLevelMem 物理+offset)
```

关键事实:

- 页数组由 `dmaPageArrayGetPhysAddr`(dma.c:1199)**线性**消费,对洞跳变无感;
  `mmuWalkMap`/`_mmuWalkMap` 的槽位推导只依赖 VA。**共享 walk 逻辑里没有
  任何能把 PA 值耦合进 VA 槽位的通道** ⇒ bug 大概率在 GSP 特有部分:
  (a) PT 页的 `pLevelMem` memdesc 的物理地址来源(页表池 pool_alloc.c /
  allocUpstreamTopPool,GSP 侧 PMA 视图分配,幻影带 37-40G);
  (b) Begin/EndTransfer 的 GSP 实现(GSP 自访 FB 的 DMA/窗口路径);
  (c) 建表前的 PT 页分配与 PDE 回填(mmu_walk.c LevelAlloc/Fill)。
- `_dmaApplyWarForBug2720120` 只对 512M 页生效,与本 bug 无关,排除。
- 压缩/comptag 路径只改 PTE 值,不产生额外槽位写,排除(PTE 值实测正确)。

## 5. 固件函数地标表(本轮定位,VA;file off = VA − 0x1000000)

| VA | 判定 | 依据 |
|---|---|---|
| 0x135d928 (sz 0x3100) | memmgr/PMA 初始化 | 读 `RMEnablePMA`(0x135e81e)+`RMEnablePmaManagedPtables`(0x135e870) |
| 0x1458134 一带 | 页表池("Fermi page pool")注册表初始化 | 读 `RMFermiPagepoolSize` ×2(0x1458f06/0x1459caa)等一串 RM* key 入结构体 |
| 0x12e1914 (sz 0x59cc) | **≈dmaMapMemory 大映射入口(GSP 侧 RPC 落点)** | 读 `RmDisableMmuInvalidate`(0x12e48fc)+`RMMmuMemoryMap`(0x12e492c)+`RMRestrictVARange`(0x12e4988) |
| 0x13de0dc (sz 0xfb4) | vaspace/gmmu 页尺寸初始化 | 读 `RmVmmuSegmentSizeOverride`(0x13de8aa)、`RmDisableBigPagePerAddressSpace`、`RMFermiBigPageSize` |
| 0x1aaa8cc (sz 0x562c) | 同上,另一处(gmmu 侧) | 读 `RmDisableBigPagePerAddressSpace`(0x1aad5ec)、`RMFermiBigPageSize`(0x1aad6dc) |
| 0x100d48c | SYS 特性初始化 | 读 `RMEnDynamicGranularityPageArrays`(0x100d8cc) |
| 0x1b298ec (sz 0x2b88) | subdevice ctrl 0xFF0000BA 派发器(前次会话定位) | 崩溃现场 0x1b2b804 在其中;memsys/MMU 编译单元 |
| 0x1a5xxxx–0x1b2xxxx | memsys+MMU+gmmu 编译单元(链接序聚集) | 前次会话 walker 候选 0x1a54b7e/0x1a6bc36/0x1b24cb0 在其中 |

下一轮最高优先级锚点:**0x12e1914 → dmaUpdateVASpace_GF100 的 NVOC 间接调用点**
(pDma vtable `__dmaUpdateVASpace__` 槽;特征:调用前大量 `sd` 栈溢出参数,
约 19 个参数),顺链下去就是 gvaspaceMap/mmuWalkMap/MapNextEntries。

另一个高效过滤器(已跑):全固件含 ≥2 处 `li a3, 6`(=TRANSFER_FLAGS
SHADOW_ALLOC|SHADOW_INIT_MEM)的函数清单已生成(见 §8 hits 文件思路);
_ RmAperture 形态 = 两次 a3=6 之间夹一个直接调用(Direct)。紧凑对候选:
0x11e53c8、0x11eeb3c、0x1200b7c、0x1859898、0x1b1fb00、0x11c8dac 等,
逐个人工甄别即可命中 _gmmuWalkCBMapNextEntries_RmAperture。

## 6. 修补前景(诚实评估)

- **字节级补丁位置本轮未能给出**——锁定肇事指令需要先在 §4 的两个模型间裁决
  (判别实验),再沿 §5 的地标顺链。任何现在给出的 offset 都是猜测,不写。
- 补丁形态预判:
  - 模型 A(重放路径槽位算错):把第二次写的槽位/索引计算修成与第一次一致,
    或直接 NOP 掉重放写(若它是 WAR 性质的冗余写)。
  - 模型 B(PT 页共享):修实例查找 key 的位宽(35→36+ bit),或修池分配的
    索引回绕。
- 签名风险:版本号探针实验(主代理进行中)将决定固件可否直接改;若签名拦截,
  剩 SEC2 booter payload 注字节这条路(R3 已有先例)。
- 非固件缓解(已验证有效,维持不变):洞保持在堆中部时单进程总映射 ≤~30G;
  或方向 A(洞挪堆顶 [72G,80G),让 ≤70G 映射物理全在洞下)——与本 bug 的根修
  正交,可先行。

## 7. 下一轮行动清单(按性价比排序)

1. **判别实验**(§3):PTE_INFO + PDE 物理读,一次冷启动裁决模型 A/B。
2. 从 0x12e1914 顺 NVOC 间接调用链到 dmaUpdateVASpace_GF100(参数个数特征),
   再到 MapNextEntries;同时人工甄别 §5 的 a3=6 紧凑对。
3. 若模型 B 证实:重点审 mmu_walk.c LevelAlloc 回调链与 pool_alloc.c 的
   upstream-top-pool 分配(幻影带 37-40G 的池,其位图即 phantom_37g.bin 内容)。
4. 若模型 A 证实:审 GSP 侧 memmgrMemEndTransfer 的 DMA 提交路径(目标地址 =
   memdesc 物理 + surf.offset 的计算位宽)。

## 8. 工具与产物(易失,附再生成)

`/tmp/gsp_re/`(重启即丢):

- `disasm.txt` — 全量线性反汇编(543.7 万行)。再生成:
  ```bash
  .venv/bin/python - <<'EOF'
  from capstone import *
  data = open('gsp_rm.elf','rb').read()
  md = Cs(CS_ARCH_RISCV, CS_MODE_RISCV64|CS_MODE_RISCVC); md.skipdata = True
  out = open('/tmp/gsp_re/disasm.txt','w')
  for i in md.disasm(data[:0xe97000], 0x1000000):
      out.write(f"{i.address:012x}: {i.mnemonic:10s} {i.op_str}\n")
  EOF
  ```
- `strings.txt` — 字符串 + file off + VA(4457 条,正则 `[\x20-\x7e]{6,}\x00`)。
- `strxrefs.txt` — 字符串→代码 XREF(3962 条;auipc+addi 配对分析,
  本轮地标全靠它)。
- `funcstarts.txt` — 24473 个函数起点(prologue 启发式)。
- capstone 已装入 `gsp_analysis/.venv`(5.0.7)。

注:线性扫描在 rodata 区(VA ≈0x1c0b000 起有内嵌数据)会产生垃圾指令,
函数边界分析以 funcstarts.txt 为准交叉验证。

---

# 第二轮(2026-08-08 晚):建表回调链全部定位并排除,rogue 写在更上层

> 新实验输入(主代理实测):模型 A(逐条目 rogue 重复写)确认,模型 B(共享 PT 页)
> 排除——受害带起点在叶级 PT 页内部(页内分裂);高区自身翻译正常(正确槽位也写了);
> 触发 = 源页 PA ≥ TH,TH ∈ (32.6G, 40.6G](与"洞后第二段"不可区分);rogue 槽位 =
> 真实槽位 − 2^35 恒定。

## 2.1 回调链函数全部验明正身(file offset = VA − 0x1000000)

方法:用宿主 x86-64 构建产物(`_out/Linux_x86_64/virt_mem_allocator_gm107.o`,带符号)
提取 `MMU_MAP_ITERATOR` / `MMU_FMT_LEVEL` 的字段偏移指纹(同一头文件,64-bit 布局一致,
可跨 ISA 移植;OBJGPU 内部偏移不可移植,已证实不同),在固件里反查。

> ⚠️ **2026-08-09 勘误**:本表早先版本的 file off 写成了 VA−0x100000(少减一个 0);
> 正确规则是 **file off = VA − 0x1000000**。下表已修正。补丁 A/B 当时用的
> 0xACBBE2/0xACBBB6 本来就是对的,不受影响。

| 函数 | VA | file off(正确) | 识别证据 |
|---|---|---|---|
| `_gmmuWalkCBMapNextEntries_Direct` | 0x1b6a2f0 | 0xb6a2f0 | pIter 标志字节四连 lbu 0x78/0x79/0x7a/0x7b(bApplyWar/bUpdatePhysAddr/bUpdateCompr/bReadPtes)、pMap@+0x80、physAddr@+0x28、currIdx@+0x18、currPageOffset@+0x1c、pAddrField@+0x70、pteTemplate@+0x60、pPageArray@+0x10(count@+0xc)、pLevelFmt(virtAddrBitLo@+0、entrySize@+2)、`1<<virtAddrBitLo`=pageSize、ALIGN_DOWN(physAddr,−pageSize) |
| `_gmmuWalkCBMapNextEntries_RmAperture` | 0x12e4d30 | 0x2e4d30 | surf.offset=entryIndexLo*entrySize、sizeOfEntries=(hi−lo+1)*entrySize、transferFlags=6(SHADOW_ALLOC|SHADOW_INIT_MEM)、`sd a0, 0x80(s11)` 存 pIter->pMap、VASPACE_FLAGS_BAR_BAR1(&0x80)与 bBug4686457WAR 检查 |
| `memmgrMemBeginTransfer_IMPL` | 0x1367110 | 0x367110 | _RmAperture 在 0x12e4dde 调用 |
| `memmgrMemEndTransfer_IMPL` | 0x13676b4 | 0x3676b4 | _RmAperture 在 0x12e4e04 调用 |
| `dmaUpdateVASpace_GF100` | ≈0x12ebc5c | ≈0x2ebc5c | 含源码 2280-2300 行的"non-contig 4KB pages"连续性校验循环(remu+逐页比较+报错字符串 xref)、FillPteMem 分支在 0x12ec56c 直调 _Direct、0x12ec1ae 物化 _RmAperture 进 mapTarget.MapNextEntries |
| dmaMapMemory 入口族 | ≈0x12e1914 | ≈0x2e1914 | 注册表三连 RmDisableMmuInvalidate/RMMmuMemoryMap/RMRestrictVARange |
| portMemCopy | 0x1b3bb30 | 0xb3bb30 | _Direct 的 4 处拷贝调用汇聚点 |

**直接调用关系(全部实查)**:
dmaUpdateVASpace_GF100 的直调方:0x10ab6ec、0x12ecdcc(×3)、0x1ac8568、0x1b57e34
(后者调用点 0x1b588bc 带 8 寄存器+11 栈参数,与 19 参签名吻合)。

## 2.2 关键排除:_Direct 与 _RmAperture 全文精读,无 rogue 写

- `_Direct`(0x1b6a2f0–0x1b6a8d8)逐指令与共享源码对齐:模板拷贝(bReadPtes/pteTemplate
  @s1+0x60)→ bUpdatePhysAddr(dmaPageArrayGetPhysAddr + ALIGN_DOWN,结果存
  pIter->physAddr@+0x28)→ bUpdateCompr(fbRegion 循环在 pMemoryManager+0x660、
  步长 0x30,比较 base/limit;kind/comptag 字段设置;PLC WAR;1to1 comptag 两次调用
  0x1b6a78a/0x1b6a7bc)→ **每个 entry 恰好一次** portMemCopy 提交进 shadow
  (0x1b6a4c2 块)→ granularity/currIdx 更新(SYS_GET_INSTANCE()->
  bEnableDynamicGranularityPageArrays @0x1b6a4dc)。**没有任何对 physAddr 的阈值
  比较,没有第二次写。**
- `_RmAperture`(0x12e4d30–0x12e4e0a)同样干净:BeginTransfer → _Direct →
  EndTransfer,各一次。

**推论:rogue 写不在共享 walk 回调里。** 由于 rogue 内容 = 最终 PTE 值(正确 PA)、
且按"页 PA ≥ TH"或"第二段"选择,剩余嫌疑只剩两处 **GSP 特有代码**:

1. **MAP_MEMORY_DMA RPC handler / GSP 侧内存对象的分段处理**(段循环把段 2 建表两次,
   第二次 VA−2^35)——当前最可能;
2. EndTransfer 之下的 GSP FB 写回路径(但那是批级,与页内分裂矛盾,除非批边界恰在
   段起点——见下)。

## 2.3 机理模型修正:段级双写 vs 逐条目,现有数据无法区分,但判别实验便宜

洞察:若建表按**物理段**分两次调用(洞把页数组切成两段),则段 2 的第一个批 =
[35.4G, 36G)(PT 页内 entries 204–511)→ 整批复制到 [3.4G, 4G) 与"页内分裂"观测
**完全一致**——幸存边界就是段起点,不需要逐条目触发。两个模型对现有全部数据
预测相同:

- **模型 A1(逐条目 PA 阈值)**:任何含 PA≥TH 页的映射(哪怕单页小映射)都会 rogue。
- **模型 A2(段 2 双写)**:只有跨洞分段的大映射才 rogue;单段映射(哪怕页在洞上)干净。

**判别实验(零风险,无需固件补丁)**:

- E1:在洞上制造**单段小映射**(如先分配塞满洞下到刚好,单个 cuMemAlloc 几页,
  使其物理页落在 44G 之上),alias_read 检查其 VA−32G 处是否被覆盖。
  A1 预测:被覆盖;A2 预测:干净。
- E2:host 侧给 `rpcMapMemoryDma_HAL`(src/nvidia/src/kernel/vgpu/rpc.c:4305)
  加 printk,记录每次 map RPC 的 (hMemory, offset, length, dmaOffset)。
  48G cudaMalloc 若只见 1 次 RPC → 分段在 GSP 内;若见多次且某次 dmaOffset 异常
  (≈正确值−32G)→ bug 在 host 调用方(可源码修复,不动固件!)。

## 2.4 补丁/探针建议

当前不建议盲改固件:_Direct/_RmAperture 已排除,dmaUpdateVASpace 以下无 PA 条件写;
真正肇事点(GSP map handler 的段处理)尚未定位到指令级。可执行的探针:

- **P1(推荐,零风险)**:E2 的 host printk。一次热加载即可,直接划分 host/GSP 责任。
- **P2(固件探针,区分 rogue 是否过 _Direct)**:在 _Direct 的 bUpdateCompr 关闭路径
  给 entry 加标记位不可行(无双字空闲位把握),改为:**把 _Direct 里
  `lbu a5, 0x7a(s1)`(bUpdateCompr 读取,0x1b6a458)之后的 kind 设置**路径……
  搁置——风险高于收益,等 P1 结果。
- **P3(机理确认后的根治方向)**:若 P1 证实单 RPC ⇒ 定位 GSP 段循环(从
  dmaUpdateVASpace 的四个直调方向上:0x1b57e34/0x1ac8568 是 memmgr CU 里的
  map 级函数,其调用者 0x184e06e/0x1680e26/0x168deee 一带即 RPC 层),
  找段基址 VA 的 −2^35/位 35 清除点,单指令修补(bgeu→j 或移位位数)。
- 若 P1 见到多次 RPC 且第二个 dmaOffset 已错 ⇒ host 侧 virtual_mem.c/调用方修复,
  不动固件。

## 2.5 本轮新增分析产物

- `/tmp/gsp_re/funcptrs.txt`(65013 条 auipc+addi 函数指针物化记录)——本轮定位的
  关键工具。
- `/tmp/gsp_re/direct.txt`(_Direct 全文转储)。
- 函数起点启发式有盲区:`c.addi16sp` 序言未计入 funcstarts.txt(0x12e4d30 即漏),
  下轮重建时应把 `c.addi16sp`/`c.addi sp, -` 一并纳入。

## 2.6 下一轮精确行动

1. 跑 P1(host printk)+ E1(洞上单段小映射)。
2. 若指向 GSP:从 0x1b57e34 / 0x1ac8568 向上追到 RPC handler,精读段循环,
   找段基址推导(重点:any `(cumBytes)`、`(segPA − basePA)`、对 dmaOffset 的加法,
   以及任何 35-bit 字段/掩码)。
3. 若指向 host:virtual_mem.c 的 map 参数构造审查(它能看到分段!).

---

# 第三轮(2026-08-08 深夜):map 分段循环定位,探针补丁候选

> E1 判别实验(主代理实测):洞上单段对象(W 8G 洞下 + B 8G 全在洞上,VA 相隔 32G)
> **完全干净** ⇒ 逐页 PA 阈值模型(A1)死;**触发 = 同一内存对象的页数组被洞切成 ≥2 段**;
> rogue 覆盖 seg2 全部页,位置 = 对象内 2MB 页索引清 bit 14(=VA−32G)。
> host 侧已证:MAP RPC 只带句柄,单次调用(virtual_mem.c:1496-1508),页数组由
> ALLOC_MEMORY RPC 经 pteDesc 平坦传给 GSP(rpc.c:3441 `rpcAllocMemory_v13_01` →
> `_issuePteDescRpc`,idr=NONE,length:16bit,64K/2MB 粒度平坦数组)。
> ⇒ **分段与双写都在 GSP 固件内**。

## 3.1 本轮定位:GSP 的客户端 map 是"段链表 → 逐 2MB 块"两趟结构

从 dmaUpdateVASpace(0x12ebc5c)的直调方反查,发现一条与 host 公开源码**不同**的
GSP 特有建表路径(host dmaMapMemory 是单次调用;固件里多出一套分块循环):

- **chunkloop = 0x1acba1c**(file 0xacba1c)。循环体(0x1acbb78–0x1acbbec):
  对每个 s4 大小(=pageSize≥2MB,`lui 0x200` 门控)的块:
  - 调 getter(0x1a5fd6c,含位展开/div/mul 的物理地址推导)以 cursor s2 取回 t3,
    t3 存入**单条目页数组**(栈上 DMA_PAGE_ARRAY:pData=栈 buf、buf[0]=t3、count=1,
    结构布局与 x86 指纹 +0xc=count 吻合);
  - `vAddr = ld(0(s3)) + t3`,`vAddrLimit = vAddr + s4 − 1`;
  - 调 dmaUpdateVASpace(flags=0x8007ff,valid=1,pageSize=s7,fabricAddr=-1…);
  - s2 += s4,循环至 s6(=params+0x30,段总长)。
  - 由于 t3 是**物理地址**(进页数组),vAddr = D + PA 形态 ⇒
    **D = ld(0(s3)) = 该段的 (VA起点 − PA起点) delta**,按段生效
    (s3 = a1 + (subdev+5)*0x100,a1 对象的 0x100 字节条目:+0x430/+0x50 memdesc、
    +0x4e8 标志字节、+0x500 VA delta)。
- **段链表 walker**:0x1a2add4(a3=1,跳过建表,似 unmap/计数)与 0x1acbde0
  (a3=0,执行建表;0x1ac8568 区域内)。记录字段:+0x64 有效标志、+0x74、+0x84
  (==0x100 检查)、+0xc8 next、+0x134 refcount、+0x138 id、+0x13c。
  walkerB 的调用者:0x1a2b7d6、0x1a2c7e4(0x1a2axxx VAS 映射管理族)。

## 3.2 机理复述(与全部实测吻合)

seg2(PA≥44G)被建表两次:一次正确(VA = dmaOffset + 35.4G + k,由主 map 完成),
一次 rogue(VA = dmaOffset + 3.4G + k = 正确值 − 32G)。在 D+PA 结构下,
**rogue 等价于 seg2 的 delta 用了 D1 − fb_length/2(D1 = dmaOffset − PA0)**
——即"第二段的物理→线性偏移"按 fb/2=40G 折算,而不是按真实洞尺寸(8G)折算;
或用对象内页索引表示:索引 bit 14(2^14 页 = 32G)被清。40G profile 下没有
PA≥40G 的页,永不触发 ✓;E1 的单段对象没有第二段,永不触发 ✓。
两种实现候选:(i) 段链表中 seg2 存在**两条记录**(正确 + delta−32G 的幽灵记录),
walkerB 各建一遍;(ii) chunkloop 的 VA delta 计算对 seg2 丢 bit 35,而正确写由
另一路(0x1b57e34 / 0x10ab6ec 的单次线性 map)完成。下一跳读物:段链表记录的
创建点(+0x134/+0x138/+0x13c 初始化处,尚未定位)。

## 3.3 字节级补丁候选(file offset 相对 gsp_rm.elf;容器内偏移需再 +0x19f040)

### 补丁 A(首选探针,可能是根治):NOP 掉 chunkloop 的逐块建表调用

- 位置:VA 0x1acbbe2,file off **0xACBBE2**
- 原字节:`E7 80 E0 07`(`jalr ra, ra, 0x7e` → dmaUpdateVASpace)
- 新字节:`13 05 00 00`(`addi a0, zero, 0`,a0=0 = NV_OK,循环空转后正常返回)
- 语义:废掉这条"每段逐 2MB 块二次建表"的路径。
- 预期:
  - **若混叠消失且 48G 全绿** ⇒ 该循环就是 rogue 写来源(冗余第二趟),修复完成;
    顺带证明主 map(正确趟)另有其路。
  - 若大映射直接坏掉(cudaMalloc/启动即失败)⇒ 该循环是唯一建表路径,立即回退;
    rogue 在更上游(段记录创建点)。
  - 若混叠不变 ⇒ 循环与客户端 map 无关,rogue 在上游。
- 副作用面:chunkloop 被 walkerB(0x1acbde0)调用,walkerB 服务于多类 VAS map;
  若为冗余路径则无功能影响。

### 补丁 B(备选探针):把 vAddr 从 D+PA 改为 D+线性游标

- 位置:VA 0x1acbbb6,file off **0xACBBB6**
- 原字节:`F2 97`(`c.add a5, t3`)
- 新字节:`C8 97`(`c.add a5, s2`)
- 语义:vAddr = ld(0(s3)) + s2(线性字节游标),绕开 PA 折算。
- 预期:若 rogue 目标来自 PA 折算,混叠偏移会改变/消失;若该循环是主建表路径,
  seg2 会被映射到错误 VA(立即可见)。信息量大但可能引入新错误,放在 A 之后试。

### 补丁 C(根治方向,待 A/B 反馈后定)

- 若 A 证明循环是冗余 rogue:A 即为最终补丁(或更精细地把循环入口条件
  `bltu s7, 0x200000` 改成永假/把 `bnez s5` 改掉,效果等同)。
- 若 A 证明循环是主路径:rogue 在段记录创建点,届时沿 +0x134/+0x138 初始化点
  反查创建者,修正/丢弃幽灵段记录。

## 3.4 实验记录锚点

- E1(2026-08-08):W/B 分离对象零污染 ⇒ 触发=对象内分段,排除逐页 PA 阈值。
- 补丁落盘注意:e_entry 不可信(booter load 被宿主 patch 跳过);补丁打在
  gsp_ga10x.bin 容器时偏移 = 上表 file off + 0x19f040;签名验证已被版本号探针
  放行(主线实测)。

---

# 第四轮(2026-08-09):补丁 A 无效后的收敛 —— 主建表链全部验证干净

> 补丁 A 实验(真冷启动,固件重载确认):NOP 掉 chunkloop(0x1acba1c)里的
> dmaUpdateVASpace 调用后,**混叠带形状/坏点数零变化** ⇒ chunkloop 既不是主建表
> 路径也不是 rogue 路径。固件→执行链路 100% 可信(空文件=GPU 死)。

## 4.1 本轮新排除(全部精读验证)

- **dmaUpdateVASpace_GF100(0x12ebc5c)与公开源码逐段吻合**:含 2280-2300 行的
  连续性检查循环(dmaPageArrayGetPhysAddr=0x12ebb20 两次调用 + 4K 帧比较 +
  NV_PRINTF),**GSP 构建里没有额外的按段拆分逻辑**;单次 gvaspaceMap。
- **dmaPageArrayGetPhysAddr(0x12ebb20)**:与 dma.c:1199 一致(pData/startIndex@8/
  count@0xc/PteAdjust@0x20/bLocalized@0x12/localizedMask@0x18/bOsFormat@0x10/
  bDuplicate@0x11),无任何截断。
- **dmaMapMemory 族(0x12ecdcc)**:单次 dmaUpdateVASpace 调用(0x12ed3c8),
  调用点不在循环内(周围 backward branch 全是错误处理路径)。
- **其上层 0x12edaa4(dmaAllocMap 形态:调 dmaMapMemory + 建 mapping record +
  dmaPageArrayGetPhysAddr)**:直线流,NVOC 间接被调(无直接 caller)。
- chunkloop(0x1acba1c)及其 walker(0x1acbde0/0x1a2add4):补丁 A 证明与客户端
  map 无关(可能是 BAR/内部 VAS 的按需映射缓存)。

## 4.2 rogue 签名进一步收窄(机理约束更新)

- rogue = 一次**额外的完整建表**(读页数组→编码 PTE→写 victim PT 页),参数特征:
  **页数组偏移完好(seg2 的 PA 全对),但 dmaOffset ≡ 正确值 − 2^35**
  (等价:offset 经 35-bit 截断后加 base,或绝对 VA 被清 bit 35;两者在
  当前 base 下不可区分)。
- 任何"共享代码里 VA 驱动"的机制均被排除;触发依赖"对象页数组在洞处断开"
  ⇒ 拆分发生在 GSP 特有层:MAP_MEMORY_DMA RPC handler → serverInterMap →
  dmaAllocMap(0x12edaa4) 之间的**段处理/两次调用**,或 handler 之上的
  内存对象段列表展开。这一层是 proprietary(GSP client 分支),公开源码无对应。

## 4.3 下一步实验(按性价比)

1. **P-E(host,零风险,先做)**:printk 两个 RPC:
   - `rpcAllocMemory_v13_01`(rpc.c:3441):pteCount、跳跃点前后的数组项
     (确认 GSP 收到的数组形态/粒度,2MB 还是 64K);
   - `rpcMapMemoryDma_HAL`(rpc.c:4305):offset/length/dmaOffset。
   一次 48G cudaMalloc 跑一次即可。若 map RPC 只有 1 次(预期),则铁证分段在 GSP 内。
2. **P-G(固件探针,判别"第二趟 map" vs "PTE 复制")**:给
   dmaPageArrayGetPhysAddr 的返回加 +2MB 毒性:
   - 位置:file **0x2EBB5C**(VA 0x12ebb5c;⚠️ 曾误写 0x11EBB5C,已勘误),
     原 8 字节 `3A 95 19 C2 9C 6F 5D 8D`
     (c.add a0,a4; c.beqz a2,6; c.ld a5,0x18(a5); c.or a0,a5);
   - 新字节:`37 07 20 00 33 05 E5 00`(lui a4,0x200; add a0,a0,a4,
     即返回 PA+0x200000;丢弃 bLocalized 的 OR,本卡未用 localized);
   - 预测:若 rogue 是"第二趟真 map"(读数组)→ rogue 槽位 PTE 的 PA 也 +2MB,
     alias_read 解码的受害内容会整体移一页;若 rogue 是"复制已写好的 PT 槽位"
     → rogue PTE 不变。副作用:所有经此函数的地址都 +2MB,系统可能起不来
     (RM 内部 map 也用它)——**属破坏性探针,仅供一次性判别,做完即回退**。
   - ⚠️ 因破坏面大,建议先试 P-E;P-G 只在 P-E 之后仍需区分两种 GSP 内部机制时上。
3. 若确认"第二趟真 map":剩余定位点 = RPC handler 到 0x12edaa4 之间的 NVOC
   间接层;可用 NVOC vtable(0x1c0xxxx 的指针表)逐表校验哪个槽位=0x12edaa4
   (vtable 项是绝对指针,直接全文搜 qword 0x12edaa4 的父表再按槽位序号反查
   NVOC 方法序)。

## 4.4 备注

- 函数边界启发式已修正(补收 c.addi16sp 序言),但共享尾声/tail-merge 仍会让
  func_of 把尾声判进下一个函数,人工核对为准。
- 服务器 ssh 在本轮中段失联(host down),host 侧查证改用已缓存源码认知;
  P-E 的精确行号以服务器树为准(rpc.c:4305 rpcMapMemoryDma_v2C_05)。
