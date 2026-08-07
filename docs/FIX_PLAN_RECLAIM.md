# 显存收复计划：从 67G 到 80G

> 状态（2026-08-08 更新）：挖洞已收窄到 **5G `[36G,41G)`**，可用显存 **~72G**
> (72G 分配+写通过，74G OOM)。验证：72G 全程 drip 无幻影命中、torture×10、
> verify×10、BF16 169.6 TFLOPS。方案 2 的 regkey 根修路已证伪（详见下文）。
> 洞的运行时开关 `RMCmpPhantomReserve` 已固化（默认开）。

## 根因回顾

GSP 内部分配器把客户页表/元数据页放进了用户可分配堆（幻影实测活动带 ~37–40G),
CPU 侧 PMA 无账 → 用户大写入与之双重分配 → 踩中即 GSP 死。
当前修复 = `driver/apply_phantom_reserve.py` 对 `[0x800000000, 0xAFFFFFFF)`(32G–44G)
做 PMA STATE_PIN + `memmgrCheckZeroPmaUsage` 容忍补丁（0x300000000)。

## 方案 1：收窄挖洞 12G → 8G（确定性高，先做）

活动带实测只有 ~3G 宽（37–40G),12G 是保守余量。按 die 对齐收窄：

- 候选区间：**36G–44G**(`[0x900000000, 0xAFFFFFFF)`)。完整覆盖 37–40G 活动带，
  上留 ~1G、下留 ~4G 余量，且是一个 8G 对齐块（匹配 8G/die 的物理布局，
  避免单 die 部分 pin 造成的碎片化）。
- 改动点：`driver/apply_phantom_reserve.py` 中 pin 循环的起止地址两个常量。
  `memmgrCheckZeroPmaUsage` 容忍值可不动（12G 上限仍大于实际 pin 量），
  或同步改成 8G 更干净。
- 验证流程（与上次固化相同，缺一不可）:
  1. `driver/build.sh` 重新出固化构建，冷启动加载；
  2. `f0_size_probe` 确认可用显存 ~67G → ~71G;
  3. **完整压测**:torture×20、verify×8、drip 全程、大区间单写。
     活动带边界是采样值，若 GSP 分配漂到带外会复现崩溃——不许只 probe 就收工。
  4. 不稳定则退回 32G–44G。
- 预期收益：可用 ~71G。

## 方案 2：GSP 固件根因修复（2026-08-08：owner 已定位，regkey 路径已证伪）

目标：让 GSP 内部 struct 页不走用户堆，从根上消除双重分配，不再需要挖洞。

### 阶段一定位结论（owner 报告）

- 固件已解包：`gsp_ga10x.bin` 是容器（`.fwimage` 段 + 各芯片签名段），主 ELF
  在其内偏移 `0x19f040`(RISC-V ET_EXEC,section header 被抹，字符串在）。
  解包产物在 `gsp_analysis/`（未入库）。
- 幻影归属（源码级，GSP 固件与开源树同源）:
  - `gmmu_walk.c`：页表层分配走 `pPageTableMemPool`(`VASPACE_FLAGS_PTETABLE_PMA_MANAGED`
    默认开，由 `bClientPageTablesPmaManaged=NV_TRUE` 决定）;
  - `pool_alloc.c allocUpstreamTopPool`：池顶用 `pmaAllocatePages(PMA_ALLOCATE_PINNED
    |PMA_ALLOCATE_PERSISTENT)` 拿 64K/2M 大块——**过的是 GSP 侧 PMA 视图，主机侧
    PMA 无记录** → 主机把同一段物理页发给用户 → 写入即踩死 GSP。双账本实锤。
- regkey 实验（新增运行时开关 `RMCmpPhantomReserve`，已固化进 build，默认开）:
  - E3 `RmGspFirmwareHeapSizeMB=1024`（实际被 HAL 钳到 256MB)+ pin 关：
    **74G 分配即撞死**(Xid 154)。假设 C 证伪。
  - E4 `RMEnablePmaManagedPtables=0` + heap 1024 + pin 关：**同样 74G 撞死**。
    B+C 组合证伪。（此前 B 单独：死亡点 39G→23G，仍死。）
  - 结论：**没有任何 regkey 能消除幻影**，只是移动它。免挖洞根修只剩两条路：
    GSP blob 二进制 patch（工程量大、成功率不确定）、fbRegionInfo 保留属性标记
    （此前拆图尝试在 GSP boot 期即崩，风险高）。
- 决策：投入产出上不推荐继续硬刚固件。**当前最优 = 8G 挖洞（70G 可用）**。
  剩余可选项只有"干净扫描 → 收窄到 5G 洞（~74G)"（见下节）。

### 进一步收窄（2026-08-08：5G 洞已行为验证通过）

- 干净扫描路线放弃：冷启动后 VRAM 内容仍不刷净，扫描到的"结构"实为
  启动残留（GSP 启动组件的 ELF 映像），活/死无法区分。
- 改为直接行为验证：洞收窄到 **`[0x900000000, 0xA3FFFFFFF)` = [36G,41G) 5G**
  （致命结构在 40G+64K，上沿留 ~0.94G 余量）。
- 验证（热加载态，2026-08-08）：单笔 72G 分配+写 OK（74G OOM，可用上限 ~72-73G);
  drip 72G 全程无幻影命中；torture×10、verify×10 PASS;BF16 169.6 TFLOPS 正常。
- **当前生效：5G 洞，可用 ~72G**。若日后出现不稳定，回退 8G 洞（改
  `apply_phantom_reserve.py` 两处常量：pin `0x140000000→0x200000000`、
  上沿 `0xA3FFFFFFF→0xAFFFFFFF`)。
- 运行时开关：`NVreg_RegistryDwords="RMCmpPhantomReserve=0"` 可整洞关闭
  （仅供实验，关闭后 74G 分配必现 GSP 死）。

## 顺序

1（半天内有结果）→ vLLM 等实际负载验证 → 2（视定位报告决定投入）。
