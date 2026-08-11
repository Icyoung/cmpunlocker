# 方案：SEC2 DMA Post-Patch（绕过验签注入 Patch A）

> 日期：2026-08-10
> 状态：Step 0 v1 失败（根因已查明）→ v2 已重写待上机
> 前置：`docs/PROBLEM_32G_WALL.md`、`docs/HANDOFF_20260808_NIGHT.md`

---

## 0. 一句话

**让 Booter 正常验签加载 stock GSP-RM 进 WPR，然后再跑一次 SEC2，用 falcon DMA 引擎把 4 字节 Patch A 写进 WPR 里的 GSP-RM 镜像。不碰验签、不改固件文件、不经 host BAR。**

---

## 1. 为什么选这条路

### 1.1 已死路径

| 路径 | 死因 |
|------|------|
| 落盘改 `gsp_tu10x.bin` | Booter 验签 `0xb` |
| Host BAR0/BAR1/CE 写 WPR | 写不粘 / 只读都破坏 GSP init |
| Booter verify bypass（gadget 替换签名） | PLM `0x31` 风暴 + GspMsgQueue 挂死 |
| ForceMbox0 骗 mbox + Patch A | mbox=0 但 WPR 里无有效镜像（验签失败 → Booter 没搬） |
| Host region/tail-steer 迁 GSP 池 | GSP 不听 host 的 region 提示，池不动 |
| 应用层限 ≤30G / 双进程左右分立 | 放弃 80G 意义 / 连坐 |

### 1.2 为什么 SEC2 DMA 有戏

- **SEC2 就是这张卡解锁的核心**——PLM theater 已经证明我们能驱动 SEC2 执行**任意 falcon 微码**，戳**任意 MMIO 寄存器**。
- 现有 PLM gadget 用 `iowrs` 做 MMIO 写；falcon DMA 引擎可做 **FB 写**——是同一套武器的自然扩展，不是全新能力。
- SEC2 是 die 内**特权安全引擎**，是 WPR 的**创建者**。它的 DMA 走 die 内部总线，和 host BAR 路径完全不同。WPR 防的是 host，**不一定防 SEC2 自己的 DMA**。
- `extra-booter-run-p1c.patch` 已证明正常 BooterLoad 之后**还能再跑 SEC2**（`kgspSec2PostblTimingRefillPayload` + `kgspExecuteBooterLoad_HAL`），机制现成。
- **验证成本极低**：写一个 DMA payload，跑一次，读回来就知道能不能穿 WPR。最坏情况只是确认死路，不伤 GPU。

---

## 2. 整体流程

```
正常启动链：
  request_firmware("gsp_tu10x.bin")           ← stock，未改
  → _kgspPrepareGspRmBinaryImage              ← 版本检查通过
  → SEC2 PLM theater（22 轮 iowrs 戳寄存器）    ← 解锁算力/显存
  → kgspSec2PostblTimingRebuildStockSignature  ← 恢复 stock 签名
  → kgspPopulateWprMeta_HAL                   ← host 构建 WPR 元数据
  → normal BooterLoad                         ← 验签通过，GSP-RM 搬进 WPR，mbox=0
  ─────────────────────────────────────────────
  ★ 新增：SEC2 DMA post-patch                  ← 再跑一次 SEC2，用 DMA 写 4 字节到 WPR
  ─────────────────────────────────────────────
  → kgspInitRm_HAL                            ← GSP-RM 以补丁后的代码启动
```

时机窗口：`kgspExecuteBooterLoad_HAL` 返回（mbox=0）之后、`kgspInitRm_HAL` 之前。和 `extra-booter-run-p1c.patch` 插入点一致。

---

## 3. falcon DMA 引擎编程

### 3.1 现有 gadget 做的事（MMIO 写）

```
; 当前 PLM gadget 核心逻辑（简化）
mov rX, writeValue
mov rY, writeAddr
iowrs I[rY], rX          ; opcode 0x8e18 — 写 GPU MMIO 寄存器
exit
```

### 3.2 寄存器布局（2026-08-10 已用 NVIDIA 官方头文件校正）

**v1 文档此节有错**，以下为勘误后的事实：

- SEC2 falcon 的 BAR0 基址 = `NV_PSEC = 0x00840000`（GA100 `dev_sec_pri.h`）。
- DMA 寄存器 = base + falcon 标准偏移（GA100 `dev_falcon_v4.h`）：

| MMIO | 寄存器 | 位布局 |
|---|---|---|
| `0x00840110` | `DMATRFBASE` | 31:0 = 外部地址 >> 8 |
| `0x00840114` | `DMATRFMOFFS` | 23:0 = falcon DMEM/IMEM 偏移 |
| `0x00840118` | `DMATRFCMD` | bit0 FULL(RO), bit1 IDLE(RO), 3:2 SEC, bit4 IMEM, **bit5 WRITE(=DMEM→FB)**, 10:8 SIZE(6=256B), **14:12 CTXDMA**, bit16 SET_DMTAG |
| `0x0084011c` | `DMATRFFBOFFS` | 31:0 = 256B 页内偏移 |
| `0x00840128` | `DMATRFBASE1` | 8:0 = 外部地址高位（80G 卡 WPR 也用不到，写 0） |

- 写 `0x00840118` 即触发；`0x620` = WRITE|SIZE_256B|CTXDMA0（v1 的 0x620 蒙对了）。
- **CTXDMA（14:12）是唯一没把握的字段**——0 是否=物理 FB 上下文由 Step 0 实测回答。
- envytools 勘误：`iowr`/`iowrs` 不是「内部 vs 系统」两个空间，而是**同一 IO 空间的异步/同步写**（fuc5 编码 `0xF6`/`0xF7`，3 字节：op,(base<<4)|src,off>>2；falcon 视角 IO 地址 = MMIO << 6，如 mbox0 = 0x1000 = MMIO 0x40，XFER 寄存器 = 0x4400+）。这条留给将来「Booter OS IMEM stub」路线用。

### 3.3 Step 0 v1/v2 失败根因（2026-08-10 查明）

**v1**（往 DMEM 模板塞原始 falcon 指令）：模板是 **Booter 加密 app 解释执行的数据**——app 自己的字节码格式（0x10aa/0x815a/0x8e18…，见 `re/sig_dmem_template.gadget.md`），host 可参数化的只有 `0xf754`=writeValue、`0xf76c`=writeAddr，每次 Booter run 恰好做**一次绝对 MMIO 写**。塞 falcon 指令 → app 解释错乱 → `boot=0xffff`。

**v2**（gadget 写 SEC2 DMATRF 寄存器）：两个死因——

1. **正常 BooterLoad 成功后，app 拒绝再执行 gadget，mbox 返回 `0x29`**。5 轮全部空转（fb0=fb1=0）。0x29 的语义从日志可推：已解锁/已加载状态下 Booter 快速拒绝（重试 attempt 的 PLM 轮也是 0x29 快败 vs 干净状态 0x31/88ms）。**结论：gadget 传输只在最终 BooterLoad 之前可用（P1C 位置已实锤），之后是死的。**
2. v2 的 refill 把 `pWprMeta->sysmemAddrOfSignature` 指向 gadget 模板且不复原 → 后续 GSP-RM init 自报失败（RPC 0xffff）→ 重试死循环 → 整卡 wedge。

**⚠️ 流程事故（同日的更大教训）**：v1 调试时把 `RMCmpSec2DmaProbe=1` 写进了 `/etc/modprobe.d/cmp-pcie-gen2.conf` 且命令行同名校验**不能覆盖**它——之后所有"基线"启动其实都在跑探针，"基线挂了"的假象浪费了一整天。**探针 regkey 永远只走命令行，绝不落盘 modprobe.d。**（已修复并留 `.bak-probefix`。）

**v3 正解：Booter OS IMEM stub**。不碰签名 memdesc。正常 BooterLoad 成功后，在 host 内存里把 OS 镜像 0x74 处（theater 路径主体，每次运行必经，紧接 lcall 0x100 之前）替换成 51 字节 falcon stub：`iowr`（fuc5 opcode **0xF6**，与 iowrs 0xF7 同格式）直接编程 DMATRF 四寄存器（falcon IO 地址 = MMIO<<6，即 0x4400 系列）→ 触发 DMA → 清 mailbox0 → exit。app 不运行、mbox=0、WPR/签名零改动。跑完恢复 pImage。

勘误存档：envytools 证实 `iowr`/`iowrs` 是**同一 IO 空间**的异步/同步写（不是内部/系统两个空间）；v1 文档把 `0x110/0x114/0x118/0x11c`（host MMIO 偏移）当 falcon IO 地址也是错的（falcon 视角应为 0x4400+）。

### 3.4 目标地址计算

从 `apply_gsp_postboot_patch.py` 的探针和 `HANDOFF` §0.6 已知：

- `pWprMeta->gspFwOffset` = GSP-RM ELF 在 FB 中的起始地址
- Patch A 的 ELF 内偏移 = `0x1b54664`
- 目标 FB 地址 = `pWprMeta->gspFwOffset + 0x1b54664`
- 容器偏移 = ELF 内偏移 + `0x40`，但 WPR 里存的是从 `.fwimage` 解出的内容，具体偏移需在 Step 1 验证时确认（读回 ELF magic 位置校准）

`pWprMeta->gspFwOffset` 是 host 侧可读的值——在 `kgspPopulateWprMeta_HAL` 之后、BooterLoad 之前就已确定。驱动里可以直接拿来算。

---

## 4. 关键未知与风险

### 4.1 SEC2 DMA 能否穿透 WPR？

**这是整个方案的生死门。**

WPR 的设计目的是防止 **host** 篡改 GSP-RM 镜像。但 SEC2 是 WPR 的**创建者/管理者**——硬件可能给 SEC2 保留了持久写权限，也可能 WPR lock 后连 SEC2 自己都挡。

**我们不知道答案，但验证成本极低**（一次 FLR 周期即可确认）。

### 4.2 WPR 内 GSP-RM 的实际布局

`gsp_tu10x.bin` 的容器结构：`.fwimage` @ `0x40` 即 stripped ELF。但 Booter 搬进 WPR 时可能有展开（ELF 的 segment layout 和文件 layout 不同）。Patch A 偏移 `0x1b54664` 是**容器内的文件偏移**，WPR 里如果按 segment 展开，虚拟地址和文件偏移的映射需要确认。

缓解：Step 1 先写 `.fwversion`（文件偏移 `0x1bfae2c`，内容是 ASCII "610.43.02"），如果 GSP 起来后报的版本变了，说明文件偏移 = WPR 偏移（至少在那个区域）。

### 4.3 GSP-RM 有没有二次自校验？

Booter 验签后，GSP-RM 自己启动时可能再做一次 integrity check（哈希自己的 .text）。如果有，4 字节改动会被发现 → GSP 自杀。

缓解：如果 Step 1（改 .fwversion）成功且 GSP 正常工作，说明至少 .fwversion 区域没有二次校验。Step 2 改 .text 可能不同——但 GA100 的 GSP-RM 是 2021 年的 610.43.02，运行时自校验不太可能覆盖全部 .text。先试了再说。

### 4.4 DMA 最小粒度

falcon DMA 引擎的最小传输单元通常是 **256 字节**（一个 DMEM block）。我们只改 4 字节，但 DMA 会写 256 字节。需要确保：
- DMEM 里对应的 256 字节 block，除了 patch 的 4 字节外，其余字节是 stock 值
- 或者：先 DMA 读 256 字节到 DMEM，改 4 字节，再 DMA 写回

前者更简单——我们知道 stock 文件内容，可以在 host 侧预填好整个 256 字节到 signature buffer（→ DMEM）。

---

## 5. 验证计划

### Step 0：falcon DMA 写非 WPR FB（验证 DMA 指令编码）—— v2 设计

**目标**：确认 SEC2 falcon DMA 引擎能被我们的 gadget 驱动，做 FB 写。

**v2 方法**（`driver/apply_sec2_dma_probe.py`，regkey `RMCmpSec2DmaProbe=1`）：

1. 正常 BooterLoad（stock）完成后，post-BooterLoad hook 跑 **5 轮** proven 单写 gadget（每轮 = refill 模板 + 再跑 Booter）：

   | 轮 | writeAddr | writeValue | 作用 |
   |---|---|---|---|
   | 1 | `0x00840128` | `0` | DMATRFBASE1 清零 |
   | 2 | `0x00840110` | `0x1000` | DMATRFBASE = FB+1MiB>>8 |
   | 3 | `0x00840114` | `0xf754` | DMATRFMOFFS = 模板尾部 |
   | 4 | `0x0084011c` | `0` | DMATRFFBOFFS |
   | 5 | `0x00840118` | `0x620` | DMATRFCMD → 触发 |

2. Host 经 BAR0 PRAMIN 读 `FB+0x100000` 共 8 字节。
3. 附带诊断：host 直接读/写 `0x00840110` 做 stick 测试——若 host 能写动 SEC2 XFER 寄存器（PLM 没锁），说明可以连 Booter 都不用，host 直接驱动 DMA。

**成功标志**：`fb0=00000620 fb1=c0deca7e`（第 5 轮时模板 `0xf754`=0x620、`0xf758`=sentinel，DMA 把它们搬到 FB——这对组合只能来自 DMEM DMA）。

**判读**：
- 全对 → DMA 编码 + CTXDMA0 + 引擎权限全通，进 Step 1。
- `boot` 状态非 0x31 → gadget 本身出问题（不该发生，和 PLM theater 同路径）。
- magic 不对但 hostWrStick 显示 host 可写 → 改 host 直写路径重试。
- magic 不对且 host 也写不动 → 怀疑 CTXDMA≠0 或 BASE1 语义，下一轮实验调 CTXDMA。

**产出**：`driver/apply_sec2_dma_probe.py`（已重写为 v2）。

<details>
<summary>Step 0 v1 存档（失败，2026-08-10 早些时候）</summary>

v1 往 DMEM 模板塞原始 falcon 指令（一条指令一个 slot），结果 `refill=0x0 boot=0xffff magic=0`。根因见 §3.3：模板是 app 字节码不是 falcon 代码。另外 v1 还有两个次级错误：把 `0x110/0x114/0x118/0x11c`（host MMIO 偏移）当成 falcon IO 地址（falcon 视角应为 0x4400+），以及误以为 `iowrs` 写「系统空间」、DMA 需要 `iowr`——实际两者是同一 IO 空间的同步/异步变体。

</details>

### Step 1：falcon DMA 写 WPR 内 `.fwversion`（验证 WPR 穿透）

**目标**：确认 SEC2 DMA 能穿透 WPR。

**方法**：
1. 正常 BooterLoad（stock 镜像）→ mbox=0，GSP-RM 在 WPR 里
2. 再跑 SEC2：DMA 写 `WPR_base + gspFwOffset + 0x1bfae2c` 处的版本字串，"610.43.02" → "610.43.99"（等长，无害）
3. GSP-RM 启动后读 `/sys` 或 `nvidia-smi` 报的 GSP 固件版本

**成功标志**：版本号变成 "610.43.99"。
**失败**：版本号仍 "610.43.02" → SEC2 DMA 被 WPR 挡 → **这条路死，结论明确**。

注意：host 不能直接读 WPR 来验证写入，所以用 GSP-RM 自己启动后暴露的版本号作为间接验证。如果 GSP-RM 根本起不来（改 .fwversion 导致 GSP 自检失败），那就知道有二次校验——但 .fwversion 不太可能被校验，因为它是元数据不是代码。

### Step 2：Patch A 真正落地

**目标**：消灭 PTE 双写。

**方法**：
1. 预填 256 字节 DMEM block：stock `gsp_tu10x.bin` 在文件偏移 `0x1b54600..0x1b546ff` 的 256 字节，其中 `[0x64..0x67]` 从 `e7 80 40 4f` 改为 `13 05 00 00`
2. SEC2 DMA 写到 `WPR_base + gspFwOffset + 0x1b54600`
3. FLR 冷启动 → GSP-RM 以 patched code 运行

**验收**（缺一不可）：
1. `vec_scan2 48` 全绿（`total_bad_units=0`）
2. 双进程 llama（Q8 + Q6），洞下 + 洞上同时推理，**互不破坏**
3. llama `-c 262144` 越过 40G 输出连贯
4. gpu-burn 默认大显存不再 FAULTY

### Fallback

如果 Step 1 证明 SEC2 DMA 被 WPR 挡：
- 回到 host 侧 PMA 单侧分配约束（思路 B），作为不完美但可用的 workaround
- 或探索 SEC2 在 WPR lock 之前的更早注入窗口（需要逆向 Booter 加密 app 的内部时序）

---

## 6. 实现要件

| 组件 | 说明 | 状态 |
|------|------|------|
| falcon DMA 寄存器布局 | GA100 `dev_falcon_v4.h` + SEC2 base `0x840000` | ✅ 已实锤（2026-08-10） |
| falcon `iowr` 编码 | `0xF6`（fuc5，备用路线用） | ✅ 已查明（envydis falcon.c） |
| DMA payload 传输机制 | 复用 proven 单写 gadget，5 轮编程 DMATRF | ✅ v2 已实现 |
| Post-BooterLoad SEC2 re-run hook | 在 `kgspBootstrap_TU102` 正常 BooterLoad 后插入 | 复用 P1C 机制 ✅ |
| 目标地址计算 | `pWprMeta->gspFwOffset + patch_offset` | host 侧可读 |
| 256 字节 DMEM block 预填 | stock 字节 + 4 字节改动 | 从 `gsp_tu10x.bin` 提取 |
| `apply_sec2_dma_probe.py` | Step 0 探针脚本 | ✅ v2 已重写 |
| `apply_sec2_wpr_patch.py` | Step 1/2 正式补丁脚本 | 待写 |

---

## 7. 和已有机制的关系

```
已有:
  _kgspSec2PostblTimingFillPayload    → iowrs gadget（MMIO 寄存器写）
  kgspSec2PostblTimingRefillPayload   → 重填 signature buffer + flush
  kgspExecuteBooterLoad_HAL           → 触发 SEC2 执行
  extra-booter-run-p1c.patch          → 正常 BooterLoad 后再跑一次的先例

新增:
  _kgspSec2DmaPostPatchFillPayload   → DMA gadget（falcon DMA 写 FB）
  s_cmpSec2DmaPostPatch              → post-BooterLoad hook，算地址 + refill + 再跑
```

新增代码不改动 PLM theater 的任何逻辑，只在正常启动完成后追加一步 SEC2 run。regkey 控制（如 `RMCmpSec2DmaPatch=1`），默认关闭。

---

## 8. 一句话总结

验签防的是「host 改了再让 Booter 验」；我们反过来——**先让 Booter 验完搬好，再用 SEC2（验签引擎自己）从内部 DMA 改 WPR**。如果硬件允许 SEC2 写 WPR，Patch A 就落地了。如果不允许，一次实验就能确认，损失只是一个 FLR 周期。

---

## 9. 2026-08-10 下午:大反转与最终路线(v16-v29)

### 9.1 实锤的事实

1. **run-2(第二次 BooterLoad)能执行 host patch 的 OS 镜像**(v4 E1/E2:mbox0 清零、DMATRFBASE 写读回 0x1f00)。注入点 = OS 镜像 `0x76`(gate 两条路径汇聚点,每次必经)。
2. **v6-v15 全部失败的根因**:DMEM `0xf700` 页 post-boot 不可写,`st D[0xf700]` 直接异常。换 `0x1700` 后一切编码(imm16/24/32 mov、iowr、st D)全部可用(v16 逐指令梯子实锤)。0xf700 是当初为靠近签名区(0xf754)选的,一个地址坑了两天。
3. **host 对 SEC2 的一切写(DMATRF/TRANSCFG/DMEMC)在 boot 完成后被 PLM 静默丢弃**;host 读 DMEM 返回 `0xDEAD5EC2` 故障填充("dead sec2")。
4. **kflcnReset 后、falcon 启动前,host DMA 通路是开的**(驱动每次 execute 都靠它 host-DMA 装载镜像)。v23:A1 sysmem 回环 ✅、A2 FB 写入链 ✅(DMEM→FB→DMEM→sysmem 魔法数完好)——**FB 写入机制完全打通**。但 WPR 读挂引擎(A3),且遗留 wedge 会毁掉后续启动+内核内存(血的教训:探针必须清理现场)。
5. **四条 OS iowrs(0x48400←0x200、0x46700←1、0x46600←3、0x46000←1)是 DMA/WPR 通路使能**:NOP 掉→ app 自己的 DMA 挂(v21,boot 失败);post-boot 重放→ falcon 异常(v22, 0x1f)。WPR 只在这四条写执行后的窗口内开放。
6. **forgive 路线证伪**(v27 null patch):改一个无害字符串字节 → 验签 0xb → forgive 放行 → GSP 照样挂。**app 验签失败后不会留下可用镜像;先拷后验不成立。** Patch A 的语义问题从未被真正测试过,投递才是卡点。
7. **app 不经任何 OS 明文终局点**(v27b):exit@0xd9、halt 循环@0xf9、trap@0xeb 全部 mbox1=0——app 在自己内部 halt falcon。**不存在 post-app 注入点。**

### 9.2 当前路线:run-1 内联注入

patch `0x27`(gate 调用)→ `lcall 0x30`,我们的代码跑在 gate 区(0x30 起,app 之前):内联 0xdf 存储 + 四条 enable + 我们的 DMA + `lcall 0x100`(app 照常跑)。v28 首测超时(0x65)——用了 ctx2,但 TRANSCFG2 是 app 才配的;**v29 改用 ctx0**(驱动装载镜像时已把 TRANSCFG0 配成 sysmem,我们 stub 时点仍然有效)。

### 9.3 若 v29 通:replace-app 全量拷贝

内联 DMA 一旦验证,app 就可以被整体替换:我们的 stub 做 enables + sysmem→WPR 全量拷贝(host patch 过的镜像,~28MB,256B×114k 次 DMA 循环)+ mbox0=0 + exit。地址全部来自 `pWprMeta`(sysmemAddrOfRadix3Elf / gspFwOffset / sizeOfRadix3Elf),host 运行时注入。falcon 循环所需编码全部已有实弹出处(add/sub=B0/B8 系、bra=B3 (reg<<4)|4 00 off8、iowr/iowrs、ld/st D)。

Patch A 目标 FB 地址 = `gspFwOffset + 0x1b54664`(auipc 数学证明 file off == vaddr;块基址 gspFwOffset+0x1b54600,块内偏移 0x64;stock `e7 80 40 4f` → `13 05 00 00`)。

### 9.4 v28-v32:WPR 直连全部证伪(2026-08-10 下午收尾)

- v28/v29(run-1 内联 falcon DMATRF iowr):超时挂,ctx2/ctx0 都一样。
- v31(run-1 内联 xdst,编码经 envydis 验证):同样超时挂。falcon 侧两条 DMA 路径在我们的上下文里都不可用;app 用的是 HS 上下文(加密区),其 DMA 携带的权限我们无法复制。
- v30:post-run-1 host 写 DMATRF 完全不粘(R0 读回 0),此前 v10/v13 的"完成"全是假象。
- v32(直写 Patch A):reset 窗口内 host 写 enable 寄存器,读回发现 **0x84111c 写 1 读回 0x300——该寄存器拒绝我们的值**;WPR 写 DMA 超时挂引擎。**WPR 由 FBHUB ACL 守护,只有 run-1 的 app(HS 上下文)能写。**

### 9.5 结论与剩余路线

SEC2 DMA 写 WPR 的所有非 HS 路径已系统性证伪。剩余路线:

1. **论文的 HS exploit**(SEC2 booter DMA length 溢出 → canary/返回地址覆盖 → LEVEL2 任意 PC → 重编程 PLM → host 直写)。这是论文在同一硬件上证明过的原语,也是唯一能开 WPR/PLM 的路。工程量大,但我们已具备全部前置:booter 镜像布局、签名 refill 通道(v2 时代就能控制签名缓冲指针)、PLM 位置。
2. 收窄的驱动侧绕过(不动 WPR):RMEnablePmaManagedPtables、GSP heap 扩容、洞尺寸调整——当年提过但未充分验证的方向。
