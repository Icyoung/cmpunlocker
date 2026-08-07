# 显存收复计划：从 67G 到 80G

> 状态（2026-08-07 更新）：方案 1 **已完成并验证**——挖洞收窄为 8G
> `[0x900000000, 0xAFFFFFFF)`(36G–44G；4G 对齐，故意不做 die 对齐：
> 活动带横跨 40G die 边界，die 对齐的 8G 窗口盖不住它）。可用显存
> ~66G → **70G**(70G 分配+写通过，71G OOM)。验证：70G 全程 drip 无幻影
> 命中、torture×10 PASS、verify×10 PASS、BF16 算力 152 TFLOPS 正常。
> 当前生效可用量 = 70G。

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

## 方案 2：GSP 固件根因修复，收复全部 12G（研究型，后做）

目标：让 GSP 内部 struct 页不走用户堆，从根上消除双重分配，不再需要挖洞。

- 第一步定位（产出 owner 报告再决定动不动手）:
  - 用 `tools/f0_phantom_scan.cu` / `f0_phantom_peek.cu` 取活动带内 struct 页的
    确切物理地址集合；
  - 结合 GSP 固件(`gsp_tu10x.bin` 提取）反查这些地址属于哪个分配池——
    大概率是 GMMU 页表页的 internal client sub-heap 未从 PMA 独立。
- 修复方向（假设 B 的深化）:
  - 若固件里存在控制 sub-heap 归属的字段/flag:patch GSP blob 或 RM 侧初始化参数，
    让页表页走 GSP 私有 reserved 区 → 用户堆完整 80G 可用，挖洞删除。
  - 若无现成开关：在 `memmgrCreateHeap_IMPL` 补丁点把 internal client 的
    heap base 重定向到 GSP reserved 区（phantom_carve 旧思路，当时未打通，
    需先确认 reserved 区位置与容量）。
- 风险与前置：
  - GSP blob 逆向工作量大，成功率不确定；
  - 需重新确认当前 GSP 固件免校验加载的前提仍然成立，否则 patch 无法生效。
- 决策点：只做有限时间的定位。有可行 patch 点 → 实施；工程量过大 →
  停在方案 1 的 71G,71G 已足够跑 27B 级模型。
- 预期收益：成功则 ~79–80G；失败无损失（回退方案 1)。

## 顺序

1（半天内有结果）→ vLLM 等实际负载验证 → 2（视定位报告决定投入）。
