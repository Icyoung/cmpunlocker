# 32G 墙污染签名解码：实测 alias 公式

日期：2026-08-11
数据：`wall_reconfirm` cross48(48G 单对象，低 32G=L 模式、高 16G=H 模式）
解码工具：`tools/decode_wall_samples.py`（对 `pat(x)=x*K^(x>>3)` 做 LSB 优先逐位反解，枚举全部原像）

## 核心结论

**污染不是 ±32G / mod 4G，而是一条 256B 粒度、带分段尾巴的线性混叠：**

```text
低区逻辑地址 A(被污染) ← 高区写入 S(污染源)
S = A + 0x8C0000000 + tail          (主 delta = 35 GiB 整)
tail = 0x900    A ∈ [0.4G, 7.5G)
tail = 0xB800   A ∈ [7.5G, 13G)     （分段边界 0x1E0000000）
```

- 两轮独立 cross48（VA base 不同：0x7c5200000000 / 0x77a340000000）解出
  **完全相同的 delta**，所以与对象 VA 无关。
- 所有失配样本的 actual 值 bit63=1(HIGH_MARK)，且反解验证
  `pat(src)|MARK == actual` 逐位成立 —— 污染源 100% 是本对象 H 区写入。
- 低区在 H 写之前全绿 → 因果方向确定：高写 → 低坏。
- 高区自读全绿 + 低区被污染 ⇒ 写 S 的数据在 VA(S) 与 VA(A) 两处都可见
  （双可见，等价于两处 VA 落到同一物理位置）。

## 为什么这排除了 PTE/建表链 bug

tail(0x900 / 0xB800）不是 4K/2M 的整数倍，只是 256B 的整数倍
（0x900=9×256，0xB800=184×256）。

页表混叠（PTE(S) 指向 PTE(A) 的页）必然产生**页对齐**的 delta。
实测 delta 有子页尾巴 ⇒ **混叠不在 GMMU 页表层，而在 256B 粒度的
数据通路/地址路由层**。

这与两条旧证据完全自洽：

1. v52 绊线：vec_scan2 全程没有任何 vAddr bit35 的调用到达
   `dmaUpdateVASpace` —— 因为墙根本不在 map 路径。
2. Patch A(NOP dmaUpdateVASpace@chunkloop）零改善 —— 打错了地方。

**Patch A / Patch C(dmaOffset 32 位截断）这一类"建表链修复"可以放弃。**

## 机制假说（按可能性排序）

### H1:PA ≥ 阈值在内存通路被折回低区（最可能）

设对象物理布局（48G 对象，洞 [35G,40G)）为
逻辑 [0,35G)→PA[0,35G)、逻辑 [35G,48G)→PA[40G,53G)。

若硬件把 PA ≥ 40G 的访问折回 `PA − 40G`:

- 写逻辑 S(PA=S+5G ≥ 40G)→ 实际落在 PA S−35G = 逻辑 A 的页 → 低区坏 ✓
- 读逻辑 S 同样折回 → 读到刚写的数据 → 高区自洽全绿 ✓
- 污染范围 [0,13G) = PA[40G,53G) 折回区间 ✓
- "32G 墙"实为 **40G 墙**(0xA00000000 = bits 35+33)
- 分段尾巴 0x900/0xB800 ⇒ 折回不是干净减法，更像 **L2 slice/FBHUB
  的 256B 粒度 swizzle/hash**（地址位异或混合），这正符合 HBM
  交织哈希的实现方式

**这和我们的解锁方式直接相关**：我们只改了 FB geometry fuse-shadow
(10G→80G)，但决定 PA 交织/路由的配置（HBM die/partition straps、
L2 slice hash、FBHUB swizzle）可能仍按旧 geometry 或未覆盖新区域，
导致高位 PA 被错误折叠。

### H2:GSP/RM 把 >40G 物理段的 PTE 算错（变体）

若某处 PA 计算用了错误的区域基址，效果同 H1 但发生在建表时。
被子页尾巴排除（PTE 是页粒度）——除非错的是 big-page 覆盖关系，
可能性低。

### H3：拷引擎（CE）独有

被否：vec_scan2 用内核写（SM 路径）也复现同样的墙，
SM 与 CE 共享的只是 GMMU walk 之后的 PA 路由。

## 对修复路线的含义

1. **别再动 map 链**(dmaUpdateVASpace / dmaAllocMap / chunkloop)。
2. RE 目标转向：**FB 地址路由配置**——
   - GSP-RM/FBFLCN 初始化里按 FB geometry 计算 swizzle/交织参数的代码；
   - 对比同驱动下 A100-80G(原生 80G）会下发哪些 170HX 没有的
     寄存器序列（FBPA/FBHUB/L2 slice remap 类）;
   - 我们 HS exploit 已能开 PLM + BAR0 写受保护寄存器，补几个
     routing 寄存器比 patch GSP 代码便宜得多。
3. 探测重点改为直接观测 PA 折叠点：
   - 用更大对象（60G/72G）确认折叠周期（mod 40G? bits {35,33} mask?
     还是 swizzle 函数）;
   - 采样已增强：`tools/wall_reconfirm.c` 现在每 256MiB 窗口留一条样本
     （服务器上为 `~/f0/wall_reconfirm2.c` + 二进制）。

## wall_reconfirm2 复核结果（single 60G / 72G）

两组均在独立 FLR + 驱动重载后执行，并用
`tools/decode_wall_samples.py` 逐样本反解。

### single 60G

- `bad_qwords=3,301,703,680`；1G 桶覆盖到 `24G..25G`，`25G` 以上无坏点。
- 解码主 delta 为 `+35 GiB`，出现的尾差为：
  `0x900`、`0xB800`、`0xBC00`。

### single 72G

- `bad_qwords=4,697,620,480`；`0G` 桶从约 `0.4G` 开始，`1..34G` 满桶，
  `35..36G` 为部分坏，`36G` 以上无坏点（外包络约 `[0,36G)`，
  与 `[0,37G)` 这一预测侧一致，而不是 `[0,24G)`）。
- 解码主 delta 仍为 `+35 GiB`，尾差为：
  `0x900`、`0xB800`、`0xBC00`、`0x11400`。

所有 delta 都是 `+35 GiB + 256B 粒度 tail`。因此 72G 对象的污染范围支持
**mod-40G 折叠/地址路由折叠这一类机制**，排除 bits `{35,33}` 位掩码的
`[0,24G)` 预测。尾差说明它不是干净的整数减法，而带有 256B 粒度的
swizzle/hash。

原始日志：服务器 `/tmp/wall_reconfirm2_single60.log`、
`/tmp/wall_reconfirm2_single72.log`。

## 分段指纹分析（2026-08-11 晚，对 .decode 全量表的二次分析）

把 single60/72 的全部解码样本按 tail 分段，结构极其规则：

| 段 | A 区间 | tail | tail/256B | 对应源 die(PA_src≈A+40G) |
|---|---|---|---|---|
| 0 | [0.4, 7.5)G | 0x900 | 9 | die 5 |
| 1 | [7.5, 15.5)G | 0xB800 | 184 | die 6 |
| 2 | [15.5, 23.5)G | 0x900 | 9 | die 7 |
| 3 | [23.5, 31.5)G | 0xBC00 | 188 | die 8 |
| 4 | [31.5, ~36)G | 0x11400 | 276 | die 9 |

关键性质：

1. **段周期 = 8 GiB = 单颗 HBM die 容量**（段边界在 8G 整数倍
   −0.5G 处，0.5G 相位差与物理 chunk 基址有关）。tail 是
   **die 粒度的函数**，不是连续 hash——混叠发生在 die 路由层。
2. tail 值表（按折回目标 die k=0..4):`9, 184, 9, 188, 276`
   (×256B)。k=0 与 k=2 相同，疑为对称性或 swizzle hash 输出。
3. 三种对象尺寸（48/60/72G，各自独立 FLR+重载）指纹**完全一致**
   ⇒ 分配器确定性 + 折叠只依赖 PA，与 VA、对象尺寸无关。
4. 污染包络 = 逻辑 `[0.4G, size−35G)`；即逻辑地址 ≥35.4G
   (PA ≥ ~40.4G）的写全部折叠。0.4G 相位来源未定（chunk B 物理
   基址 = 40.4G? 或折叠阈值 = 40.4G?)，需要更细粒度实验区分。

**结论强化：这是 HBM die 路由/交织层的折叠**——80G geometry 解锁后，
高 5 颗 die(die 5–9,PA ≥ 40G）的访问被路由回低 die，
并带每 die 一个 256B 单位的偏移。修复目标 = 让 die 路由表
（很可能由 FBFLCN/RM 按 fuse geometry 编程）覆盖 80G 全量。

## 给测试 agent 的后续采样清单

1. ~~single 60 / 72~~：**已完成**，见上两节。
2. 每次跑完用 `tools/decode_wall_samples.py <log>` 解 delta;
   若 delta 随 S 变化出现更多尾巴分段，把分段边界全部记下——
   分段结构就是 swizzle 函数的指纹。
3. cross48 已复现两次，不需要再跑。
4. （可选，用于提取 swizzle 函数）细粒度实验：在单个 8G 段内以
   256B~4KB 步进密集采样 tail——若 tail 在段内还有子结构，
   说明是地址位异或 hash;若段内恒定，则是纯 die 路由表项。
   需要给 wall_reconfirm 加一个 `dense <startGiB> <endGiB>` 模式。
5. （可选，区分 0.4G 相位来源）小对象阶梯：single 36/38/40/42G,
   观察污染起点是否恒为逻辑 35.4G。

## 原始数据

- 本轮 cross48 日志：服务器 `/tmp/wall2_cross48.log`
  （VA base 0x77a340000000,bad_qwords=1,691,090,944，与首轮逐桶一致）
- 首轮结果：`docs/EXPERIMENT_32G_WALL_RESULTS.md`
- 解码脚本：`tools/decode_wall_samples.py`（已同步服务器 `~/f0/`)
