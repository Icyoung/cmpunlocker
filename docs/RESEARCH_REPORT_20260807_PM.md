# CMP 170HX 10GB→80GB — 2026-08-07 下午 Kimi 会话进展

_接续 RESEARCH_REPORT_20260807.md 与 HANDOFF_TO_KIMI.md。本文档记录幻影结构体（phantom）调查的全部实证结论。_

## 结论速览

**80G 崩溃的根因已定位为 RM/GSP 内存管理层的双重分配，与 DRAM/strap/VBIOS 无关。**
一个 GSP 侧放置的元数据结构（内容证据指向 MMU 页表/指针表）落在用户可分配堆内
（80G 配置下约 PA 37–40G 处，随堆布局漂移）。大写入覆盖它之后，GSP 下次访问即死。

## 关键实验证据链（按时间序）

1. **物理层完全排除**：扩展 alias probe（`~/f0/f0_alias_probe_ext`）在 36/40/44/48/52/56/60/64/68/70/72/76G
   十二个边界全部 DISTINCT——物理 80G 每页独立可寻址，无折叠。（配合 Claude B8 的 40G 单点结论。）
2. **zone 隔离**（`f0_zone_probe`）：78G alloc 内 [0,20G)/[40,60G)/[58,78G) 三个 20G 区域写入均全速健康。
   无毒地址区间。**注意 [20G,40G) 当时从未被单独测过——幻影一直藏在盲区。**
3. **死亡点定位**（`f0_slow_drip`，逐 GiB 写入 + 落盘进度）：
   - 无 pin 启动 ×2(60G/78G alloc)：均在 alloc VA **39G+704M** wedge。78G alloc 同点 ⇒ 当时推断为固定 PA ≈ 0xA00010000。
   - PMA pin [0xA0000000,0xA8000000) 后：死亡点移到 VA ~37G ⇒ **幻影位置随堆布局变化，不是硬编码地址**。
4. **崩溃取证**：
   - `mcause=4, mbadaddr=0x5a5a5a5a5a5a5a5a`（我们的填充 pattern）——GSP 把用户数据当指针解引用。
   - 另一变体 `mcause=7 store access fault @ heap VA 0x5bf8198`，死前 trace 有 128G/256G 越界中间值。
   - 经典签名 `pc:0x5b2b940 illegal instruction` = 跳进 GSP 堆 VA（代码段在 16–32MB，堆在 ~66MB+）。
5. **内容鉴定**（`f0_phantom_scan`，只读扫描冷启动后首个 60G alloc 的非零块）：
   - VA ~37.5G 处有**页表形态内容**（重复 flag+地址结构的 64 位项，如 `0x1e640000000000`/`0xfea0003800000`/`0x7918`）。
   - 相邻有**指针表**（`0x23a02006045ec` 起、步长 0x238=568B 的结构体指针数组）。
   - 另有 10G 几何的 WPR-meta 残留（`0x27ff00000`=10G wprEnd、`0x6900000`=10G heapSize）出现在用户页中。
6. **CMP_FBALLOC 日志**（memdescAlloc 全量打印）：RM 侧 ≥1MB 分配全在 FB 顶部或低端，
   **没有任何 RM memdesc 分配覆盖幻影区**；用户 cudaMalloc 不经 memdescAlloc（GSP 侧分配）。
   ⇒ 幻影由 GSP 侧放置，CPU PMA 不可见。

## 被否决的方案

- **区域裂解挖洞**（apply_phantom_carve.py):GSP 可见 fbRegionInfo 裂出保留洞 →
  启动期 GSP 即死（Xid 1 @ boot)。该区域图对 GSP 自身放置逻辑是 load-bearing 的，不可改。已撤销。
- **PMA pin 挖洞**（apply_phantom_reserve.py + MIG zero-check 容忍）:pin 本身工作正常
  （`CMP_MEM_RSV: pinned [0xa0000000,0xa7ffffff]`），但幻影随布局漂移，堵一个洞它就搬家。
  **而且**：pin 会触发 `memmgrCheckZeroPmaUsage` 断言（MIG 路径），需附带容忍补丁。
- CONFIG4 / FEAT / SS / P1a / P1c（详见主报告与 handoff，均被实验否决）。

## 当前最佳机理假说

GSP 在 80G 几何下把映射元数据（页表页等）放进了用户堆中段（~40G，恰好是"半量"位置）;
8GB 卡 64G 配置下这些结构落在保留区/顶部，不冲突。可能机制：GSP 侧元数据池按某个尺寸源
（疑与 40G=80G/2 或 10G×4 相关）放置，与 fbSize=80G 的用户堆重叠。

## 下一步候选（未决）

- A. GSP 固件里找元数据放置逻辑（ELF 已提取到 /tmp/gspfw/，主镜像在 gsp_ga10x.bin +0x19f040)。
- B. 8GB 卡对照扫描（f0_phantom_scan 在其 64G 配置上）——其结构落点即"正确位置"的参照。
- C. 找 GSP 侧内部堆尺寸/位置的可调项（registry 或静态配置），把元数据池移出用户区。

## 2026-08-07 深夜补记 —— 突破：12G 挖洞搞定（Kimi，第三轮）

**10GB 卡 80G 配置首次全项通过。** 方案：PMA pin [32G, 44G)（12G,STATE_PIN)+ MIG zero-check 容忍，
可用 ~67G。通过项：drip 全程 [0,60G)（此前必死于 39G+704M)、60G/65G/67G 单次全速写、
torture 65G×3、verify×3(30 轮）、corrupt_map 全扫零坏页。

最终定性：
- 幻影 = **GSP 内部分配器放置的客户页表/元数据页**（内容三重鉴定：PT 格式条目与固件内
  GMMU 模板逐字节吻合；指针表；日志环）。CPU 侧 PMA 完全看不到这些页（≥32G 专项日志里
  只有用户自己的分配）→ PMA 把它们当空闲发给用户 → 写入即踩死 GSP。
- `RMEnablePmaManagedPtables=0` 实验：死亡点 39G→23G，直接证明幻影跟随页表分配路径。
- 位置随分配器布局漂移（37-40G 带），但 12G 宽洞 [32G,44G) 足够罩住它的全部活动范围。
- 8GB 卡（16 FBPA）无此问题；10GB 卡（20 FBPA）在 64G/80G profile 下均有。

固化：`apply_phantom_reserve.py` 已纳入 build.sh 的 prep 流程（紧随 apply_profile.py),
并计入构建 stamp。12G 是保守余量；幻影实测活动带约 37-40G，后续可尝试收窄到 8G。

代价：可用容量 80G→~67G;MIG zero-usage 检查打了一针容忍（cmpPhantomRsv)。


1. **8GB 卡对照扫描完成**：其 60G alloc 内只有"死残骸"（与 10GB 卡同魔数 `0xdc3aae21371a60b3`
   的 stock 几何 WPR-meta 残留，8G 版常数），无活幻影。10GB 卡@80G 的幻影是活的、独有的。
2. **PMA 分配日志（CMP_PMA_ALLOC）**：启动期所有 ≥1MB PMA 分配都在 ≤0x26600000 低端；
   幻影区（37-40G）无任何 CPU 侧分配 ⇒ 幻影由 GSP 内部分配器放置，不经 CPU PMA。
   60G cudaMalloc = count=30720 × 2MB 页，经 PMA 路径（vidmemPmaAlloc）。
3. **逐 GiB 页表打印（CMP_PMA_MAP）失败**：hook 点 pPages 数组未填满（仅前几个有效），弃用。
4. **PMA pin 实验（[40G,40G+128M) STATE_PIN）**：pin 生效但死亡点从 VA 39.7G 漂到 37G——
   幻影随分配器布局漂移，不是硬编码 PA。挖洞思路（无论 GSP 地图裂解还是 PMA pin）均否决。
   pin 附带坑：`memmgrCheckZeroPmaUsage` 断言需容忍（已在 apply_phantom_reserve.py 处理）。
5. **区域裂解（CMP_CARVE）使 GSP 启动即死**：fbRegionInfo 地图对 GSP 是 load-bearing，不可改。
6. **当前最可能机理**：GSP 的元数据（页表/日志环/指针对象表，内容已鉴定）放置逻辑在
   fbSize=80G 时把结构放进了用户堆中段；64G（8GB 卡）时恰好安全。疑似某个尺寸/阈值计算
   在 64G（2^36）之上出错。
7. **进行中**：新增 `10gb64` profile（10GB 卡跑 64G）。若稳定，用户可直接用 64G（比 40G 多 24G），
   且"64G 稳定 / 80G 崩"将进一步锁定阈值在 2^36 附近。


## 本次新增资产

- `tools/f0_alias_probe_ext.cu` — 多边界别名探针
- `tools/f0_slow_drip.cu` — 逐 GiB/64MB 滴水写入定位（落盘进度）
- `tools/f0_zone_probe.cu` — 区域隔离写入
- `tools/f0_size_probe.cu` — 单次大写尺寸阶梯（含 SELFLOC 自定位填充、WATCH 观察窗）
- `tools/f0_phantom_scan.cu` — 只读非零块扫描+小块转储
- `tools/f0_phantom_peek.cu` — 指定窗口只读转储
- `tools/f0_corrupt_map.cu` — 全量 20G SM 校验扫描
- `tools/mmio_dump.c` / 服务器端 `/tmp/mmio_read`(mmap 版） — BAR0 寄存器读取
- `driver/apply_fballoc_log.py` — memdescAlloc 全量日志诊断
- `driver/apply_phantom_carve.py` + `revert_phantom_carve.py` — 区域裂解（已否决，留档）
- `driver/apply_phantom_reserve.py` — PMA pin 挖洞（含 MIG 容忍；未解决问题，留档）
- `driver/apply_config4_probe.py` — 已废弃（VBIOS 证明 0xc4030033 是正确值）
- 服务器日志：~/f0_logs/E1_*（各次崩溃的完整 dmesg）
