# 2026-08-08 PM 排查报告:大映射 VA 翻译损坏(32G 墙)

> 触发点:llama.cpp 27B 模型 `-c 262144` 输出乱码。一路排查发现一个**驱动级 bug**,
> 与算力解锁、显存硬件、CUDA 工具链均无关。

## 现象与规律(全部实测)

当**单进程 GPU VA 映射总量 T 超过 ~35G** 时,VA 区间
`[首个 cudaMalloc 基址 + 3.4G, 基址 + (T-32G))` 的翻译损坏:

- 对该区间的写(SM kernel / CE memset 均试)**全部落空**(黑洞,疑似落到 WPR 物理页);
- 读返回**陈旧 DRAM 内容**(VRAM 不刷净,读到的是上一个进程的数据);
- 坏带终点恒为 `T-32G`,坏带大小随 T 线性增长;T=32G/34G 完全正常;
- 与分配粒度无关(单块 48G 与 40×1G 多 buffer 同样触发);
- 与访问宽度(4B/16B)、kernel 并发度(grid 64~65535)无关;
- 确定性复现,跨进程、跨冷启动稳定。

## 排除项(都有实验证据)

| 假设 | 实验 | 结果 |
|---|---|---|
| FP16/BF16 tensor core 算错 | 精确整数 matmul(K=256/4096,多显存高度) | 0 错,算力 152/155 TFLOPS 正常 |
| 显存物理损坏 | CE 读回 + SM 字节校验全量扫 | 干净 |
| 幻影腐蚀(5G/8G 洞) | 洞改回 8G [36,44) 重建冷启动后复测 | 坏点依旧,且物理地址不变 → 与洞无关 |
| CUDA 13 工具链 | 裸 CUDA(nvcc 13.0)大 buffer 读写/归约/原子 | 全干净;但 torch 大张量归约会踩中本 bug |
| llama.cpp 长 ctx kernel bug | CPU 256k 连贯;GPU ≤144K 连贯;KV 放 CPU 256k 连贯 | 非 kernel bug |
| host 侧 32G 常量 | 全树 grep `0x800000000`/`<<35`/NvU32 截断,逐路径排查 | 无命中(见下) |

## 关键实验记录

- `vec_scan2`(全量 pattern 写+读):T=48G 时坏带 [3.4G,16G) 约 95% 单元错误;T=34G 全好。
- `vec_probe3`:坏带内容 = 上一进程的 pattern 残留(加盐区分),证明**写被丢弃而非写错位置**。
- `multi_buf_test`:40×1G,buffer 3–8(累积 3G–9G)坏,其余好 → 与"总映射量"相关,非单分配。
- `ladder_test`:0.5G 间隔小量探针写全好 → 只有大批量映射+访问才触发(页表页数量随 T 增长)。
- `alias_probe2`:坏带内容与正常位置存在 ~2G/32G 粒度的错位关系。
- llama 阈值实验:131072/147456 连贯,163840/262144 乱码 —— 恰好在总映射 ~35.4G 两侧。

## 根因判断

host 侧开源代码无任何 2^35 假设(已排查 GMMU fmt、PDB 分配、TLB invalidate、
partial page tables、PMA client addr space 等,见子任务排查记录)。症状几何
("后映射的 32G 永远好,早映射的被追溯破坏")指向:**页表自身的物理页在 T 增大后
被覆盖/别名**,肇事者最可能是 GSP 侧页表池(`pool_alloc.c allocUpstreamTopPool`
→ `pmaAllocatePages(PINNED|PERSISTENT)`,即原幻影机制)——其内部可能残留 40G
时代的尺寸推导(40G 既是稳定版 profile 的 fb 长度,又是 80G>>1),host 无法校验。

次要嫌疑:
- `kernel_gsp.c` static-info 修补只改了 `fb_length` 和最后一个 region 的 limit,
  `reserved=limit-base+1` 把整段标 reserved;`fbRegion[].reserved` 的消费方可能有尺寸推导不一致。
- comptag:`supportCompressed=NV_TRUE` 是写死的,80G 的 comptag backing 从未验证过。

## 缓解措施(当前生效)

**单进程 GPU 映射总量 ≤ ~32G 即安全。**

- llama-server:`-c 131072`(总映射 ~31G)已验证连贯;262144 必坏。
- vLLM:`--gpu-memory-utilization` 按总映射 ≤30G 折算,长 ctx 的 KV 要算进去。
- 当前驱动 = 8G 洞 [36,44) 构建(与本 bug 无关,但保留了余量;5G/8G 对本 bug 无差异)。

## 下一步(判定性实验,需驱动仪表化 + 冷启动)

1. 用 `NV0080_CTRL_CMD_DMA_PTE_INFO`(`kern_gmmu.c:2478 kgmmuExtractPteInfo_IMPL`)
   查询坏带 VA 的 PTE:invalid / sparse / 指向 WPR / 指向其他用户页,四种结果对应四种 bug。
2. 物理读坏带的 PD1/PD0 表项(f0_phantom_peek 思路),与好带对比;
   若坏带 PDE 与好带 PDE 指向同一 PD0 表 → 实锤页表分配器别名。
3. 记录坏带各级页表物理地址,看是否落在 37–40G 幻影带或 [36,44) 洞内。

## 测试工具(服务器 ~/f0/)

`vec_scan2`(分桶扫描)、`vec_probe3`(加盐坏点 dump)、`multi_buf_test`(多 buffer)、
`ladder_test`(探针)、`write_fate`(写落点判定)、`flood_bisect`、`alias_probe2`、
`atomic_test`、`red_repro`、`fill_repro`、`sm_write_test`。

---

# 2026-08-08 晚:根因实锤 —— 32G 周期 PTE 混叠,触发器是"洞之上的物理页"

## 决定性实验链(全部实测)

1. **宽过滤 PMA 日志**(apply_pma_alloc_log.py)+ 48G 洪水测试:
   buffer 物理布局完全线性 `PA = 0x26600000 + VA偏移`,跳过洞 [36G,44G)。
   坏带 VA [3.4G,16G) 对应物理 [3.6G,16.6G),**全部低于 32G** →
   否决"PTE 35-bit 截断"假设。

2. **PTE 写入日志**(apply_pte_map_log.py,CMP_PTE_VA/CMP_PTE_MAP):
   48G cudaMalloc 的映射**完全不经过 host `mmuWalkMap`**——没有叶级页表分配、
   没有 PTE 填充,而 GPU 翻译照常工作。小映射(CUDA heap 等)都走 host。
   → **客户端大映射的页表由 GSP 固件内部构建**(rmapi 转发,alloc_free.c:773
   "RPC to GSP"),host 开源代码不参与。这就是 host 侧一直找不到 bug 的原因。

3. **NVreg_EnableGpuFirmware=0 实验失败**:610 在 GA102 上强制 GSP
   (param 被接受但 GSP 照常加载),无法绕开固件拿 host 路径。

4. **alias_read(模式反解码)**:坏带内容可精确解码为 **VA+32G 处的模式**
   ——是**翻译级混叠**(VA x 与 VA x+32G 共用翻译,高者胜),不是写丢/WPR。

5. **alias_read2(8G filler 移位实验)**:buffer 物理基址从 0.6G 抬到 8.6G 后,
   坏带起点从 3.4G 移到 0。**起点 = buffer 物理地址跨过 4G(2^32)的位置**。

6. **alias_read3(cuMemAlloc 经典路径)**:同样混叠,与 API/工具链无关。

## 最终经验定律

> **物理地址位于幻影洞 [36G,44G) 之上(PA ≥ 44G)的页,其 GSP 构建的 PTE
> 会落到"VA 低 32G"的槽位**,把该槽位原有翻译覆盖掉。物理在洞以下的页完全正常。

- 验证 run1(基址 0.6G):洞上页 = VA 偏移 [35.4G,48G) → 受害者 [3.4G,16G) ✓
- 验证 run2(基址 8.6G):洞上页 = 偏移 [27.4G,48G) → 受害者 [0,16G) ∩ buffer ✓
- 受害者起点 = 物理 4G 线纯粹是"洞起点 36G − 32G"的几何结果。
- 混叠偏移恒定 +32G;洞 5G/8G 无差异(起点都是 36G)与"偏移=洞起点−4G"或
  "恒定 32G"均相容,未区分。
- T ≤ 34G 干净的原因:洞以下物理只有 ~35.4G,且 32G 伙伴需同时在映射内。

## 机理推断

GSP 固件为跨洞的大映射建表时,洞把它切成两段物理 page array;
固件对**第二段(洞上)**的槽位计算错了恒定 −32G(疑似段起始 VA/页索引推导
含 32-bit 或 32G 周期假设)。host 只负责 PMA 分页(日志正常),页表构建全在固件。

## 推论与修复方向

- **若没有中位洞,映射物理线性 → 预计完全不混叠**。洞是触发器。
- 方向 A(首选):把幻影保护洞挪到堆顶(如 [72G,80G)),让 ≤70G 的映射物理
  全部在洞以下 → 干净。前提是幻影 GSP 元数据的位置能随 geometry 补丁挪到顶
  (查 apply_phantom_reserve/carve 与 profile 常量的关系)。
- 方向 B:逆 GSP 固件(gsp_analysis/gsp_rm.elf,RISC-V stripped,字符串少),
  找段处理代码二进制修补 —— 有签名强制风险,固件认证失败会变砖(可冷启动恢复)。
- 方向 C:不同 hole 位置做判别实验(洞挪到 [44,52) 看混叠偏移变 40G 还是仍 32G),
  进一步钉死固件里的推导式。
- 当前安全约束不变:单进程总映射 ≤30G(洞以下 + 无 32G 伙伴)。

## 新工具(服务器 ~/f0/)

`alias_read`(模式反解码混叠探测)、`alias_read2`(filler 移位)、
`alias_read3`(driver API 对照)、`va_base`(打印 cudaMalloc VA 基址)。
驱动补丁:`driver/apply_pte_map_log.py`(CMP_PTE_VA/CMP_PTE_MAP)。

---

# 2026-08-08 深夜:GSP 固件加载机制 + RE 进展

## 固件加载机制(多次探针实验结论)

- **GSP-RM 在卡上跨驱动卸载(rmmod/modprobe)和暖重启存活**:模块重载只重连,
  不重启 GSP。"伪冷启动"测试固件补丁全部无效的原因就在于此。
- **真断电(拔卡/断扩展坞电 60s+)后 GSP 必死**,重启时镜像从
  `/lib/firmware/nvidia/610.43.02/gsp_ga10x.bin` 加载(已排除:nvidia.ko 内无
  RM text(3735 块 0 匹配)、卡上无 expansion ROM)。
- **nvidia-smi 的 "GSP Firmware Version" 不是有效观察通道**:它来自二进制字段,
  把文件里全部 3 处 ASCII 版本串改成 .03 后依然报 .02。验证固件补丁只能靠
  行为测试(alias_read)或崩溃探针。
- 外置雷电扩展坞有独立供电:只断服务器 AC ≠ 断卡的电。
- 固件容器 = ELF:.fwimage(0x40,84MB 主镜像,内含 gsp_rm RISC-V ELF @0x19f040)、
  .fwversion、各芯片 .fwsignature_*。booter 签名路径在本驱动构建中被故意打穿
  (booter load 0x31 失败是 PLM 注入剧场的一部分),实际经 kflcnResetIntoRiscv 直启。

## RE 进展(子代理,RE_FINDINGS.md)

- 543 万条反汇编全量扫描:**固件内无 40G/32G/80G 立即数** → 几何是运行时数据,
  bug 是位宽/推导缺陷,不是忘改的常量。
- 混叠算术特征 = **bit 35 (2^35=32G) 丢失**;恒定 −32G 排除了 NvU32 截断
  (mod-4G 会产生多种跳变偏移)。
- 结合主线新数据(受害带可位于叶级 PT 页内部、rogue 是额外多写、高区自身正常):
  **模型 = 对 PA ≥ TH(TH≈fb_length/2=40G)的页,额外把其 PTE 多写一份到
  VA−32G 的槽位**(2MB 页索引丢 bit 14 / VA 丢 bit 35)。疑似"按 fb 一半分两趟"
  的上趟基址推导错误。
- 固件地标已建(dmaMapMemory 入口 0x12e1914 等),正在下钻到具体函数。

## 判别实验备忘(若需进一步钉 TH)

- TH=36G(洞起点/分段)vs 40G(fb/2)目前被洞 [36,44) 遮蔽,无法区分;
  如需要,缩洞到 [36,40) 或挪洞起始点做判别。

## 附:客户端映射的完整数据流(源码考证)

1. UMD cudaMalloc → host RM 物理分配(可观测:`pmaAllocatePages` 日志);
2. host RM 作为 **GSP client**(裸金属 610 上 IS_GSP_CLIENT 也为真)把整个 alloc/map
   通过 **vGPU 协议 RPC**(`NV_RM_RPC_ALLOC_MEMORY` / `NV_RM_RPC_MAP_MEMORY_DMA`,
   virtual_mem.c:1508)转发给 GSP;MAP RPC 只带**句柄**(hMemory + dmaOffset +
   length),**不带页数组**——页数组由 GSP 自己持有;
3. GSP 在固件内完成 VA 分配 + 页表构建(host 全程无感知,与日志观测一致)。

推论:32G 墙完全位于 GSP 固件的 MAP_MEMORY_DMA 处理链内
(句柄 → 页数组遍历 → walk → PTE 写);host 无法经参数面干预页数组格式。
拆分配(多个小 cudaMalloc)无法规避:触发条件是**页面自身 PA ≥ ~40G**,与分配粒度无关。

## E1 判别实验(VMM 布局控制,零风险)——模型再次收窄

构造:W(8G,PA [0.6G,8.6G))映射 VA base;44G filler(不映射)占住 [8.6G,60.6G);
B(8G,PA [60.6G,68.6G),**全在洞上**)映射 VA base+32G。
结果:**W 零污染、B 零错误**。

- **逐页"PA≥TH 即 rogue"模型(A1)否决**;真触发条件 = **内存对象的物理页数组被
  洞切成 ≥2 段**:48G buffer(seg1 [0.6G,36G)+seg2 [44G,56.6G))的 rogue 恰好覆盖
  seg2 全部页,重复写入位置 = 对象内页索引**清掉 bit 14**(−2^14 页 = −32G)。
- 单段对象免疫(无论 PA 多高)。40G profile 无墙的原因随之明朗:**无洞 → 永远单段**。
- walk 回调(RE 已验证)每条目恰好写一次 → 肇事点在 walk 上层的**段迭代**:
  seg2 被建表两次,第二次 VA/索引推导丢 bit 35。
- 幻影带(37-40G)本身高度疑似"GSP 内部某视图的尺寸 = 40G = fb/2"的产物;
  若属实,宿主侧可能存在"让 GSP 内部尺寸正确 → 洞可挪顶/消失 → 单段化"的修法,
  与固件字节补丁并行候选。

## 待办实验池(按子代理 RE 结果选用)

- E2:host rpcMapMemoryDma printk(预期:48G 一次 RPC,分段在 GSP 内)——基本已被 E1 取代。
- E4(暂搁置):洞以下人为碎片化多段对象,判"多段即坏"还是"高段才坏"。
