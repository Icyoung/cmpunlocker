# SEC2 DMA / WPR 注入实验全记录（2026-08-10)

> 目标：把 Patch A(NOP `dmaUpdateVASpace` @ fwimage+0x1b54664）写入 WPR 中已验签的 GSP-RM 镜像。
> 结论先行：**所有非 HS 路径已系统性证伪；WPR 只认 run-1 的 app(HS 上下文）**。剩余可行路线见文末。
> 环境：CMP 170HX (GA100, devId 0x2082)，驱动 610.43.02，服务器 p3-server。

---

## 1. 最终结论

1. **WPR 由 FBHUB ACL 守护**，只在 BooterLoad 的 app(Heavy-Secure 上下文）执行期间开放。host、falcon stub(run-2)、reset 窗口内的 host DMA 全部无法读写 WPR。
2. **forgive 路线死亡**：改 1 个无害字节 → 验签 0xb → host forgive 放行 → GSP 依然挂。app 验签失败后**不会**留下可用镜像（不是"先拷后验")。
3. **Patch A 的语义从未被真正测试**——所有失败都是投递失败。它本身是否有问题（全局 NOP 误伤 boot 路径）仍是开放问题。
4. **post-reset 窗口（kflcnReset 后、falcon 启动前）host 驱动 SEC2 DMA 完全可用**：sysmem 回环、FB 写入链都验证过。这是本项目新获得的基础设施能力，只是够不到 WPR。

## 2. 基础设施事实（别再重新推导）

- **modprobe.d 大坑**：探针 regkey 写进 `/etc/modprobe.d/` 后命令行同名参数盖不住，曾浪费一天。探针 regkey 只走命令行。
- **FLR 足够做干净实验**(`echo 1 > /sys/bus/pci/devices/0000:3d:00.0/reset`)；主机 reboot 不清 GPU 状态。判 GPU 干净：`CMP_MEM_EARLY_WRITE: cfg1 before=0x02449000` = stock。
- **RM 初始化延迟到第一次 open 设备**;nvidia-smi 触发。抓日志用 `journalctl -k -b`(dmesg 被 swap 刷屏）。
- **gen2.service 已于今天 disable**（早期 PCIe Gen2 retrain 服务，与手动实验冲突）。
- 驱动构建：`driver/build.sh`，约 20 分钟；`CMPUNLOCKER_STRIP_POST0808=1` 跳过 08-08 后加的 9 个脚本（注意:forgive/force_mbox 也在跳过之列，需要时去掉 STRIP)。
- PCI 地址目前稳定 `3d:00.0`（见过 09:00.0)。

### Booter OS (GA100 prod）明文布局（`gsp_analysis/booter_load_ga100_prod.bin`)

| 偏移 | 内容 |
|---|---|
| 0x00 | init：读 mbox0/1 存 DMEM[0x1850/0x1858]，设 sp,lcall 0x2f |
| 0x27 | `lcall 0x2f`(gate 子程序） |
| 0x2f-0x75 | gate：检查 DMEM[0x200..0x20c]，两条路径都汇聚到 0x76 |
| **0x76** | **主路径起点（注入点）**:lcall 0xdf;mbox0=0x31;清 gate;4 条 enable 写；xdst 配置入 s10($cauth) |
| 0xd5 | `lcall 0x100`（加密 app：验签+搬运 GSP-RM 进 WPR) |
| 0xd9 | `exit`——**app 不会返回到这里**(v26 实锤） |
| 0xdf | 子程序：DMEM[0x6340]=0x2d706 |
| 0xeb | 异常处理（st 现场 + halt loop) |
| 0xf9 | halt 循环 `lbra 0xf9` |

### 四条 enable 写（0x76 块内，falcon IO 地址）

| IO | 值 | host MMIO | 备注 |
|---|---|---|---|
| 0x48400 | 0x200 | 0x841210 | |
| 0x46700 | 1 | 0x84111c | **host 写 1 读回 0x300——拒绝我们的值，疑似 WPR 门禁** |
| 0x46600 | 3 | 0x841198 | |
| 0x46000 | 1 | 0x841180 | |

### falcon fuc5 编码（全部真机/envydis 验证）

- `mov r,imm8/16/24/32` = `0r XX` / `4r LL HH` / `8r LL MM HH` / `Dr + 4B LE`
- `mov $srN, rX`(xdbase=sr7, xtargets=sr11)= `FE (X<<4|N) 00`
- `iowr I[r9],rX` = `F6 (9<<4|X) 00`;`iowrs` = `F7 ...`(同空间，异步/同步）
- `st D[r1],r2` = `A0 12`;`ld b32 r14,D[r13]` = `BF DE`（形式 `BF (src<<4)|dst`)
- `sub b32 r9,r9,imm16` = `B8 99 LL HH 02`;`bra ne rX,0,off8` = `B3 (X<<4)|4 00 off8`(off8 相对指令起点）
- `xdst Ra,Rb` = `FA (a<<4|b) 06`(DMEM[lo16(Rb)] → ext $xdbase+Ra,size=Rb>>16,6=256B);xdld = `FA .. 05`
- `xdwait` = `F8 03`;`exit` = `F8 02`;`lcall` = `7E LL HH 00`;`lbra` = `3E LL HH`
- **大偏移形式 `I[r9+0x200]` 未验证/疑似坏**——OS 原文永远全地址入寄存器、offset 恒 0

### DMEM

- **`0xf700` 页 post-boot 不可写**（一写就异常，v16 梯子实锤）——v6-v15 两天失败全因这个地址
- `0x1700`/`0x1800` 可用；`0x1850/0x1858` = OS init 存的 mbox0/1
- host 直读 SEC2 DMEM 返回 `0xDEAD5EC2`("dead sec2"故障填充，PLM 拦截）

### DMATRF(host MMIO,SEC2 base 0x840000)

+0x110 BASE（外部地址>>8)/+0x114 MOFFS(DMEM 偏移）/+0x118 CMD/+0x11c FBOFFS/+0x128 BASE1。CMD:bit0=FULL、bit1=IDLE（读）、bit5=WRITE、bit4=IMEM、bits10:8=SIZE(6=256B)、bits14:12=CTXDMA、bits3:2=SEC。TRANSCFG(i) @ 0x840600+i*4:boot 后 0/1=0x114(LOCAL_FB|PHYS)、**2=0x115(sysmem|PHYS)**、其余 0x110。FBIF_CTL @0x840624 bit7=ALLOW_PHYS_NO_CTX;DMACTL @0x84010c bit0=REQUIRE_CTX。

### 关键地址

- `pWprMeta->gspFwOffset` = GSP-RM 镜像在 FB 的**绝对地址**（约 0x13fe300000，每次 boot 变）
- Patch A 目标 FB 地址 = `gspFwOffset + 0x1b54664`(auipc 数学证明 file off==vaddr;stock `e7 80 40 4f` → patch `13 05 00 00`)
- 已备好的 256B patch 块：`gsp_analysis/patch_a_block.bin`
- WPR meta 页（sysmem):`pWprMeta` CPU 映射 + `memdescGetPhysAddr(pWprMetaDescriptor)`；驱动里它 4KB,+0x800/+0xC00 可做观测/暂存

## 3. 实验流水（v1-v32)

### v1/v2（前情，详见 §3.3)
v1:DMEM 模板塞 falcon 指令——模板是 app 的字节码，boot=0xffff。v2:BooterLoad 成功后 app 拒执 gadget(0x29);refill 换签名指针搞挂 GSP init。

### v3-v9:IMEM stub 路线 + 编码排雷
| 版本 | 结果 | 教训 |
|---|---|---|
| v3 | 自检拦截：0x74 处是 0xd9 非 0x7e | 交接笔记偏移记错 2 字节，lcall 在 **0x76**；运行时镜像与本地 bin 完全一致 |
| v3b | boot=0xffff,stub 没跑完 | — |
| **v4** | **E1 ✅ boot=0x0（框架成立）;E2 ✅ base=0x1f00(iowr 粘住，XFER 没被 PLM 锁）;E3 ❌ xdwait 挂** | 首次实弹验证 run-2 stub 执行 + DMATRF iowr |
| v5 | 6 变体全死于 CMD 写入前（rdCmd 旧值） | — |
| v6/v7 | boot=0x1f mbox0=0(tail 跑了）但 CMD 未落地、marker 地址算错 | mbox1 IO=0x1100 非 0x1040；大偏移 iowr 不可靠 |
| v8 | sysmem 目标 6 变体全挂 | 事后定性：V0 把 sysmem 地址当 FB 用，DMA 落 FB 物理空洞 → 队列中毒，后面全陪葬 |
| v9 | 12 变体，未跑（被取代） | — |

### v10-v15:host 驱动 DMA 幻想破灭
| 版本 | 结果 | 教训 |
|---|---|---|
| v10 | host 写 TRANSCFG 不粘（锁 0x114);H2/H3"成功" | st=0x0 全是假象（写根本没生效） |
| v11 | TRANSCFG dump(ctx2=sysmem 白嫖到）;host 写 DMEM 被挡（0xdead5ec2);fwOff 是绝对地址 | — |
| v12/v13 | 各跳"完成"但 meta=0 | host 写被静默丢弃，无观察通道 |
| v14/v15 | falcon stub 全挂，连控制 stub 都挂 | 引出 v16 梯子 |

### v16：逐指令梯子（本周最大单点突破）
T0 tail ✅ / T1 mov r8 ✅ / **T2 st D[0xf700] ❌ 致命** / T3 st D[0x1700] ✅ / T4 imm24+iowr+BASE 读回 ✅ / T5 ❌（同 T2 死因）。**0xf700 毒地址坑了两天。**

### v17-v22:DMA 传输层排查
| 版本 | 结果 | 教训 |
|---|---|---|
| v17 | 3 stub 全跑完（boot=0x0)，数据零移动 | 编码/执行没问题，传输层被禁 |
| v18 | +xdwait:S0 跑完 S1 挂 57s | xdwait 等的是 XD 单元，不是 DMATRF |
| v19 | 阳性对照（OS init 存的 wprPhys 回显）也没到 sysmem | 传输从未发生 |
| v20 | run-2 补写 DMACTL/FBIF_CTL 无效 | 锁定发生在 run 1 |
| **v21** | **run 1 NOP 四条 enable → app 自己 DMA 挂（boot 失败）** | 四条写是 DMA/WPR 使能 |
| v22 | run-2 重放 enable → 0x1f fault | post-boot 连 falcon 都被锁 |

### v23：窗口期实锤（本项目新基础设施）
kflcnReset 后 falcon 未启动时：**host 写 SEC2 寄存器全部粘住；A1 sysmem 回环 ✅;A2 FB 写入链 ✅(DMEM→FB→DMEM→sysmem 魔法数完好）**。A3 WPR 读挂引擎。教训：探针必须清理现场，否则遗留 wedge 毁掉后续启动 + 内核内存（kmem_cache_free 警告）。

### v24:host 补写 enable + WPR 读 → 整机冻结（冷启动恢复）

### v25-v27b：注入点排查
| 版本 | 结果 | 结论 |
|---|---|---|
| v25 | 0xd9 exit 改 lcall stub:boot 正常但 meta=0 | stub 没跑 |
| v26 | +mbox1 marker:mbox1=0 | app 不返回 0xd9 |
| **v27** | **null patch(1 字节字符串）+forgive:0xb→放行→GSP 照挂** | **先拷后验证伪；forgive 路线死** |
| v27b | 三个终局点（0xd9/0xf9/0xeb）全 0 | **app 内部 halt，无 post-app 注入点** |

### v28-v32：最后冲刺
| 版本 | 结果 | 结论 |
|---|---|---|
| v28/v29 | run-1 内联（0x27→lcall 0x30)+DMATRF DMA(ctx2/ctx0) | 0x65 超时挂 |
| v30 | post-run-1 host 写 DMATRFBASE 读回=0 | host 写 post-run-1 完全不粘 |
| v31 | run-1 内联 xdst(envydis 验证编码） | 0x65 超时挂 ×3 |
| **v32** | **reset 窗口 host 补 enable:3/4 粘住，0x84111c 拒绝（写 1 读 0x300);stage ✅;WPR 写 0x65 挂** | **WPR 只认 run-1 app 的 HS 上下文** |
| **v33** | **probe==4:DMATRFCMD.SEC=0..3 全扫（同 v32 窗口，control=FB+1MiB v23-A2 目标）。sec=1:control ✅(cmd=0x626) WPR 0x65 挂;sec=2/3:control 即 0x65(cmd=0x628/0x62c，干净拒绝，GSP 照活);sec=0:control ✅ WPR 0x65（复现 v32)** | **SEC 字段 host 可设但帮不了 WPR:SEC=1 能写普通 FB 仍被 WPR ACL 拒;SEC=2/3 连普通 FB 都被拒。host 触发 DMATRF 进 WPR 彻底证伪，剩 HS 触发一条路** |
| **v34** | **probe==5:HS 扳机——step1 RefillPayload(0x84111c,1)+reset-ful run（读回仍 0x300);step3 reset 后 host 预编程 DMATRF**;step4 免复位 run 走 NoReset 变体扳机 DMATRFCMD=0x620** | **决定性失败：hook 在最终 BooterLoad 之后，continuation 已死——step4 run mbox=0x29（快速拒绝，PLAN 文档 §95 早已记录此语义：gadget 只在最终 BooterLoad 之前可用）。0.29 状态下连 reset-ful run 也全部 0x29 快败，且 step3 的 host DMATRF/TRANSCFG 写被静默丢弃（en 寄存器照粘）——0x29 拒绝路径拉起的 PLM 跨 reset 存活。idle_cmd=0x2，扳机 DMA 从未发生。GSP 死，sysrq 重启恢复。推论：HS 扳机必须搭在最终 BooterLoad 那一次 run 上（multi-write continuation），post-hook 无计可施** |
| **v35** | **probe==6:塌缩 WPR2(0x1fa824/28:13f72000/13ffee00→0/0，粘住且跨 kflcnReset 存活)→ probe4 前导 → host SEC2 DMATRF → 恢复 bounds。read1 读回 stock 块 **match=1**（首次从 WPR 内读回 GSP-RM 字节，门开+地址数学双重实锤）;write st=0x0;read2 patched 块 **match=1**（写落盘实锤）;restore 干净;GSP 活（610.43.02, 81920 MiB)** | **Patch A 投递成功但 vec_scan2 48 total_bad_units=845545477 = stock 32G 墙原值，零效果。两个假设：(a) Patch A 语义不对/不充分（计划文档早标注"语义从未被真正测试过，投递才是卡点"——投递现已解决，语义首测即败）;(b) RISC-V 启动后执行的 .text 另有副本（boot 流程重载/重定位把 sysmem 原镜像又盖进 FB)，我们补的是 booter 的驻留副本。判据实验：同路径补 .fwversion 版本串（610.43.02→.99，无害），若 nvidia-smi 显示 .99 则 in-place 执行实锤→(a) 成立** |
| **v36** | **probe==7:同 v36 塌缩路径补 .fwversion。第一轮 7 处全 stock=0——用了 .bin container 偏移；修正为 ELF 偏移（-0x40，驻留布局=ELF 文件布局，probe6 已实锤）后：6/6 驻留副本 stock=1 wrote=1 verified=1（第 7 处在 ELF EOF 之外的 trailer，不自驻留，自动跳过）** | **`/proc/driver/nvidia/gpus/*/information: GPU Firmware: 610.43.99`——运行中的 GSP-RM 读的就是我们补的这份 FB 副本，in-place 执行实锤，假设 (b) 死。结论：Patch A（0x1b54664 jalr→li a0,0）语义不对/不充分，32G 墙不由它（单独）把守，需重新推导 Patch A。vec_scan2 基线不变（845545477)，版本串补丁无副作用。关键地址事实：驻留 FB 偏移 = gsp_rm_tu10x.elf 文件偏移 = gsp_tu10x.bin 偏移 - 0x40** |
| **v37** | **probe==8:PATCH_B_RE.md 的 C1(dmaUpdateVASpace)+C2(dmaAllocMap）参数记录桩，经 v35 通道投递。10/10 site 全部 stock/zero 校验通过+写入 readback 实锤（c1=1 c2=1 ringzero=1);ring dump 用 os-interface.c 里的 delayed_work(30s×8)。坑：nv-kernel.o 链接是 --gc-sections，跨层符号被回收——加 probe==9 一次性 dump 分支保住引用;stash 的 pKernelGsp 在 GSP 启动失败后被 teardown 释放→delayed dump UAF 页错（已修：dump 时 GPU_GET_KERNEL_GSP 重新获取）** | **GSP 启动挂死:Xid 119(GSP_INIT_DONE 22s 超时）,RISC-V TRACE 在 0x400a35c↔0x5c27c68 两地址间乒乓——0x5c27c68 不在任何 PT_LOAD 段内（未映射）,trap 死循环。最可能机制:ring 所在段（va 0x41f7000 起）对 GSP 是只读映射，stub 第一条 sd 写计数器就 fault。下一步(RE/家长决策）:ring 搬进已知可写全局页（同一页内有被写的 global)；或先单测 stub 本体（stub 只跳回不写 ring）来区分"stub 编码错"vs"ring 页只读"。UAF 修复后第二轮：dump 安全跳过，host 无恙** |
| **v38** | **probe==8 round 2:ring 迁到静态 store 普查证明可写的页（C1 file 0x38a500, C2 file 0x38b7c0,PATCH_C_VERIFY.md §5),stub 字节按新 ring 重新生成（与 gen_probe.py 逐字节核对）;11 site 全部 arm OK,ring 窗口零校验通过** | **GSP 照样挂，且 trap PC 与 v37 逐位相同（0x400a35c↔0x5c27c68,Xid 119)——ring 地址变了、故障地址没变 → "ring 页只读"理论死亡。故障在 hook/stub 的执行路径本身，与 ring 无关。留给 RE 的线索:0x5c27c68 = stub va 0x5be7c68 + 0x40000（恰好 +256K，两轮一致）;0x400a35c=file 0xa35c(libos 段，疑似 trap handler)。建议下一步:stub 本体单测（只跳回、不写 ring，即把 stub 换成 prologue 重放+jump-back）区分"跳转/编码错"vs"任何 stub 执行都死";或先只装 C2 hook（不装 C1）二分。probe10（并行 agent 的 mbox1 exfil smoke）已并入同一 generator，其 static 前向声明错已修** |
| **v39** | **probe==8 round 3:stub 迁进 dma CU 内死函数（file 0x1026c34/0x1026c8c，与 hook 同映射域，PATCH_C_VERIFY.md §6);stock 校验改为真代码字节（从 ELF 提取）;hook 跳转目标运行时解码验证（0x5027b54→0x5026c34, 0x502ccdc→0x5026c8c)。10/10 site arm OK** | **GSP 活了！死函数 stub 位修复了挂死（v37/v38 的 cave 页确实未映射）。但计数器全 0——boot 期间和 vec_scan2 repro 期间（8 dump × 30s，全窗口）C1/C2 都从未触发。加扫 ±0x40000 候选位（0x386500/0x3877c0/0x38e500/0x38f7c0）也全 0。结论：这两个函数按当前识别从未被执行，或 .text 的执行副本不是我们 patch 的这份物理页（v36 只证明了 DATA 读本副本；TEXT 执行路径可能另有映射——+0x40000 VA 偏移说明运行时确实重映射）。host 侧 delayed-work dump 通道全程稳定（8/8 dump,collapse/restore 干净，live 系统上无 wedge)。RE 下一步：确认这两个函数是否真的被调用（可在 hook 位放一个只写 magic 到已证实可写页的极简 stub 来区分"没调用"vs"写丢")** |
| **v40** | **probe==8 round 4:magic-qword 判别桩——同 hook 位同死函数 home,stub 只做 lui/sd 写 magic(C1=0xC1C1C1C1C1C1C1C1→va 0x438a500, C2=0xC2C2...→0x438b7c0)+prologue 重放+跳回，共 48B;generator 内置迷你汇编器生成，capstone 逐条解码验证（抓到并修了一个真 bug:_rv_sd 漏 rs2 字段会写成 sd zero)。arm 10/10 OK** | **magic 从未出现：boot 后（dump #0）和 vec_scan2 后（dump #1-7）五个读取位（0x38a500/0x38b7c0 及 ±0x40000 四个候选）全 0,GSP 全程健康。但 v37/v38 的 trap 证据（跳到 stub_va+0x40000 才挂）说明 hook 当时确实 fired——所以最一致的解释是：stub 执行了，但其绝对 VA 的 sd 落到了运行时映射的另一物理页（不在 ELF 布局 ±0x40000 网格上）。下一步需要运行时 VA→PA 锚点：PATCH_C_VERIFY.md 末尾的 backup exfil 通道——读 libos logger buffer 指针（va 0x43c9d18/file 0x3c9d18，logging 开启时运行时填充）拿到一对已知 VA↔PA，据此放 ring。备选：hook fired 的 v37/38 vs 不留痕的 v39/40 差异本身也值得 RE 再核对（或许 trap 另有来源）** |
| **v41** | **probe==8 round 5:§7.4 三路 magic 三角定位（magicA→PH27 ring 0x438a508、magicB→logger buffer ptr+0x40、magicC→PH26 0x4033360),bytes 与 gen_magic41.py 逐字节核对；dump 改为定点读（C1/C2 区、magicC 点、logger struct、buffer 两种物理猜测）。arm:4 patch site OK;logger struct 零校验 SKIP(idx=6——logging 在 arm 时已活）** | **三路 magic 全无，GSP 健康。但 dump 带回关键情报:(1) "file 内为零的干净窗口"在运行时全是活结构——file 0x38a500 处有 {6, 0x2000, 0x5c2b000, 3} 的描述符，file 0x33360 也是活数据 → RE 的静态零窗口选型法无效，ring 放哪都会踩活数据;(2) logger 活跃：idx=6 cap=8192 buf=0x5c2b000（该 VA 在 text 截断区之外，两种物理猜测读回相同值，疑似 stale 或 alias);(3) v37/38 挂死的另一种解释浮出：那两轮唯一特有的破坏是 cave 覆写——logger 函数(va 0x5be7b50, 活跃）尾部 0x5be7c64 紧邻 cave，若其执行流入 cave 即踩中我们的 stub。→ "hook 从未 fire" 与 "v37/38 挂死" 可以自洽：hook 可能从未 fire,v37/38 是 cave 覆写误伤 logger。判别实验（建议）:stub 往 fwversion 串（已证 on-grid 可读）写一个字符——/proc 显示即 hook fires，不变即函数识别/执行副本有误** |
| **v42** | **probe==8 round 6:fwversion 字符判别 + 大范围物理扫描 + 通道自检。stub1 = 计数器+magicA+record+magicB（同 v41)，末尾追加 6 处版本串 "02"→"77" 的 sh 写（版本串=已证 on-grid)。第一轮换载即挂:Xid 1 store address misaligned @ stub+0x48——sh 写在奇地址（0x4025e6f)——**这本身就是 hook fire 的铁证**（挂在版本串写上说明前面的 magicA/B 写已执行且没 fault)。修对齐（+6，我写错了立即数 0x737≠0x3737，结果写成 '7'+BEL）后:GSP 活** | **决定性突破:/proc/driver/nvidia/gpus/*/information 显示 `610.437\\a2`——版本串被 stub 改写成功（值错是我立即数笔误，位置全对）→ hook fires、stub 跑到底、对 PH26 rodata 的绝对 VA 写落在 on-grid 物理页，全部实锤。但 magicA/计数器依旧不在 grid 0x38a500/0x38b7c0，两范围 sweep(0x380000-0x3e0000 + 0x1c00000-0x1d00000）零命中 → PH27 写落点不在 ELF grid 也不在扫描范围；最可能解释：ring 选点是活结构（v41 已见），stub 写入后被 GSP 运行时覆写。下一步（RE/家长决策）:exfil 通道改用"只读不被覆写"的 on-grid 数据页——6 个版本串副本里只有 3 个有代码 xref，拿一个无引用的副本当 mailbox(9B 太少就占一串连续 rodata),dump 走 grid 读回；或先扫整个下 WPR 区 [0x13ee400000..0x13fe300000) 找 magicA 定 PH27 物理基址** |
| **v43** | **probe==8 round 7:PH27 物理基址实测。stub 写 8 个 magic(..ee51..ee58）到 PH27 各 VA(0x438a508/0x438b7c8 已证安全 + 0x4200000..0x4700000 六点）;dump #1/#5 全扫 WPR2 span(0x1fa824/28<<4 = 0x13f720000..0x13ffee000,36064 块/sweep 约 1s)。8 store 全执行（GSP 活、无 fault)** | **零命中：magic 不在 WPR2 span 内（或被运行时覆写）。指纹分析彩蛋：v43 的 grid 读回内容能在 ELF file 0xc09164 处对上 5 个连续 qword(delta 0x87ec64)，随后发散——同一 grid 页三次 boot 三种内容（v41 描述符/v43 PH27 尾部内容/v42 又一描述符）→ grid 数据页是动态复用的堆/竞技场，不存在固定 PH27→grid 映射可测。PH27 物理落点只剩两个候选区：未扫的堆区 [0x13ee400000..0x13f720000]（下一次 sweep 范围）或已被覆写。另一个怪象待 RE:v43 的 C2 读出了 hook2 stock 字节（13 01 01 f2...）而该页 arm 时已验证被 patch——疑有第二份 text 物理副本** |
| **v44** | **probe==8 round 8:rodata 邮箱（3 个无引用版本串副本：file 0x1897f0/0x1aea08/0x1d77a8——全物质化扫描实锤无 xref;slot1=counter, slot2/3=bit35 条件 ra/a5)+ magicA->0x438a508 + 低 WPR 区 sweep。stub 76B+16B passthrough,capstone 验证全指令（beqz 立即数坑已修：+16 会落在第二条 sd 上，应为 +20)。arm 4/4 OK,GSP 活** | **两个坑全爆:(1) sweep 地址量级错——reg<<4=[0x13f720000..0x13ffee000] 是 5.36G 低 FB，不是 85G 的 WPR2;v43/v44 扫的都是无关低 FB（所以零命中但能读）;v44 低区 span 起点>终点空转。正确单位未解（reg hi<<8≈image end，疑似 addr>>8)。(2) dump 通道 post-boot 不可信：SC 自检从未读到过版本串——v42b 里 /proc 证明串已被改成 610.437\\a2，同时刻 dump 读同物理地址却是别的内容 → post-boot 的 collapse/TRANSCFG0 写可能被 PLM 丢弃，或读落点漂移。arm 时读全对、post-boot 读全错。邮箱实验因此无结论。下一步：dump 里先自证（读 image file-0 比对已知字节 + 打 0x1fa824/28 与 TRANSCFG0 的写后读回），修好通道再重跑邮箱** |
| **v45** | **probe==8 round 9:通道自证 + /proc 活性测试。WPR2 单位定死：dev_fb.h(tu102) VAL=31:4 ALIGNMENT=0xc → reg=addr>>8,lo<<8=0x13f7200000 正确括住镜像。dump 加自检（collapse 读回+TRANSCFG0 读回+image file-0 比对 16B);RMCmpSec2DumpNoReset 变体；stub 加 ASCII 计数器写引用版版本串** | **机制定位完成：wpr2_rb=0/0(collapse post-boot 照粘）,tc 写也粘，但 file-0 自检拿到的是带 ecall 的代码残留（6396070073001000）且三轮 boot 逐位相同 → DMATRFCMD 触发写 post-boot 被 PLM 丢弃，引擎从未启动，idle 轮询读到 IDLE=1 假成功，全部 dump 读的是 DMEM 0x1700 的 boot 残留。noreset 变体同结果。/proc 版本串冻结（610.43.02 不变）——但该值多半 init 时缓存，测试结构性无效。结论：post-boot 读回通道的死因=SEC2 host 写窗口在 GSP 启动后永久关闭（reset 不再重开）。下一步（RE)：找出关闭 host 写的那个 SEC2 PLM/lock 寄存器，在 arm 窗口（post-BooterLoad+reset,host 写还粘）把它写到常开** |
| **v46** | **probe==8 round 10:寄存器差分猎锁。arm 末尾快照 0x840000..0x841200(1152 dword)+4 个 PLM;dump 开头（先于任何写）逐寄存器 diff，只打变化的** | **60 个变化寄存器到手。关键:(1) 0x104(BOOTVEC)/0x184/0x1c4/0x258 post-boot 读回 `0xbadf51xx` ——NV 的 badfalcon 故障填充=这些寄存器被 PLM 从 host 读都封了，PLM 确实升了;(2) 0x240(疑似 SCTL) 0x3000→0x7021（新置位 bit0/5/14)——首要嫌疑;(3) 0x250 0xf→0;(4) DMATRFCMD(0x118) arm=0x622→now=0x6602(CTXDMA 字段 0→6，说明引擎 post-boot 被别人用过/最后命令是 ctx6);(5) CPUCTL(0x100) 0x10→0、RM(0x84) 0x170000a1→0。下一步：dump 里把 0x240（±0x250）写回 arm 值再自测——若 file-0 读变真，锁就是它** |
| **v47** | **probe==8 round 11:dump 里写回锁寄存器（0x250←0xf 先，0x240←0x3000 后，读回记录）再自测。首个 cycle 遇上 delayed_work 未触发（同 v39b 的调度 flaky，重跑即恢复）** | **干净的否定：rb250=00000000 rb240=00007021——两个写回都被静默丢弃，post-boot host 对整个 SEC2 锁/触发面都不可写。自检照旧 match=0（同样的 DMEM 残留）。结论：post-boot 读回通道无法从 host 侧撬开；备选路线（RE/家长）:(a) 在固件里 patch 掉升锁代码（RE 在 booter/GSP-RM 里找写 0x240/0x250 的点）;(b) 放弃 post-boot 通道，把邮箱读回挪到 NEXT boot 的 arm 窗口（FB 内容活过 FLR——本轮存的值下一轮 arm 时读！)——(b) 零新机制，只差一个跨 boot 的读回约定** |
| **v48** | **probe==12:跨 boot 读回——在 kgspPopulateWprMeta 之后、首个 BooterLoad 之前的早钩子读 3 个邮箱槽+版本串对照** | **第一次读全零——因为早钩子的 fwBase=0x27e300000 是 10G 布局值，而运行时镜像在 80G 布局的 0x13fe300000，读错了地方。v48b 加了 80G 基址读；但在两轮 boot 流程里 boot N 的 modprobe -r 撞上了 dump delayed_work 的 4 分钟挂起窗（v37 就标注过无 cancel):work 在模块卸载后触发→整机冻结。ssh 死透 >5 分钟，需物理冷启动。教训：用 probe==8 的 cycle 卸载前必须等满 4 分钟 dump 窗，或先 cancel_delayed_work_sync** |
| **v48-final** | **冷启动后重跑（sequencing 已修：等 dump #7 done 再卸载，unload rc=0 干净）** | **两个硬结论:(1) 早钩子读 10G 基址全零（10G 布局区在 init 时被清/不含镜像）;(2) 早钩子读 80G 基址 st=0x65 引擎 wedge——80G 镜像区在 BooterLoad 前就被（WPR1 或 PLM）保护，该窗口读不了；wedge 还毁掉后续 PLM 循环的 BooterLoad→GSP 起不来。→ 跨 boot 读回路线死：早窗口读不到 80G 区，arm 窗口镜像已被重载覆盖。若还要追：邮箱需放在镜像加载区之外且 FLR 后仍存活的 FB 里（logger 写 VA 需落在那里——PH27 物理落点之谜的前置问题）。所有 restore 完成，机器健康（610.43.02, 81920 MiB)** |
| **v49** | **gap 窗口重试:probe==12 挪到 PLM 循环+几何 host 写（SS0/SS1/CFG1/LMR）之后、RebuildStockSignature 之前（RebuildStockSignature 调用行为锚点）。两轮 boot 序列干净（dump 列车等完再卸载）** | **80G 读在 gap 窗口照样 wedge(st=0x65)，且 wedge 后连 BAR0 寄存器读都卡（soft lockup 实锤在 osDevReadReg032 读 0x840118 的轮询里打转 26s)——80G 地址要到最终 BooterLoad/booter 配置内存解码后才存在。残留 wedge 杀死后续 BooterLoad(Xid 45),GSP 死；restore 过程中整机冻结（ssh 死透 >4 分钟），需再次物理冷启动。最终结论：SEC2 通道只能在 post-BooterLoad 窗口读 80G 镜像区，而那里镜像已被重载——跨 boot 邮箱读回在所有可行窗口都不可行。若继续：只能走"运行时持久 on-grid 槽位 + 本 boot 内读回"（如 v44 邮箱）但需先修 post-boot 通道（v47 已证 host 不可撬）——或接受纯静态 RE 路线** |
| **v50** | **猎锁登记：0x840240/0x840250 在所有公开头文件（裁剪版 dev_falcon_v4.h、envytools rnndb、nova regs.rs）中都无名——envytools 明确标 "0x240: ??? v5+ units"。host 侧全树 grep:sec2/falcon/gsp 无任何代码写 SEC2 窗口 0x240/0x250（除我们自己的探针）;SCTL/PLM-raise 类函数在开源树里不存在（安全模块闭源）;booter 排除（其写在 arm 快照里可见，当时还是 0x3000);GSP-RM 反汇编里无 0x840240 绝对地址物化、无 0x7021 常量（唯一的 lui 0x7021 @0x4ead0e8 是乘法缩放系数 0x7021039)** | **结论：升锁者是 GSP-RM 固件（RISC-V 侧），在 GSP 初始化窗口（最终 BooterLoad 之后、首个 post-boot dump 之前）通过 map 相对寻址写 SEC2+0x240(0x3000→0x7021）与 +0x250(0xf→0)，或该变化是硬件在 RISC-V 启动/secure 转换时自动置位。留给 RE 的路标:(1) 毒化读偏移 0x104/0x184/0x1c4/0x258(BOOTVEC/CODE/DATA 窗口被 PLM);(2) 变化位=0x240 的 bit0/5/14 新增;(3) 写者形态=引擎描述符基址+0x240 的 sd（数据驱动，常量搜不到——建议从 kgspInitRm→GSP 启动 RPC 区间里的 sec2/falcon 管理函数下手，或找 falcon IO 0x9000(=host 0x240）的 iowr);(4) 若证实是硬件自动置位，则软件无解，需换思路（如阻止 RISC-V 启动前的某步，或接受 arm 窗口单发）** |
| **v51** | **probe==13:PGSP 邮箱直写 exfil（绕开封死的 SEC2)。stub(76B,gen_exfil45.py 逐字节核对）每次 dmaUpdateVASpace 调用经寄存器窗口基址（va 0x438af28）写 mbox0=vAddr.lo/mbox1=vAddr.hi16|ra.lo16<<16;probe=13 只 arm 不开 dump 列车（卸载即安全）。host watcher:mmap resource0 紧轮询记录变化。第一轮零变化——host 探针普查发现 RE 选的 0x110440/44 主机写不入（dropped)；真邮箱在窗口+0x40/0x44(0x110040/44 STICK;0x111300 有固件活值但部分位被 PLM 掩；0x840040 SEC2 窗口读直接 bus-error segfault=封死铁证）** | **v51b（重定向到真邮箱）依旧零变化（watcher 起点读到 boot 残留 m0=04070000 m1=9c800000，证明寄存器活；vec_scan2 全程 75s 无任何变化）。可能原因:(a) stub 在调用时读到的窗口基址指针不对/为空（RE 的窗口证据来自 doorbell 路径，不代表 map 路径执行时该全局已初始化）;(b) 运行时 VA 窗口不把 +0x110040 映射到 PGSP 邮箱（写被丢）;(c) dmaUpdateVASpace 在 vec_scan2 路径上根本不被调（boot 时 fire 过=v42 Xid 实锤，但 client map 可能走别处）。下一步建议（v52 候选）:stub 把读到的窗口基址指针的字节用版本串（已证 /proc 可见的持久通道）编码出来——一次 boot 读出运行时基址真值，直接区分 (a)/(b)。机器已 restore(610.43.02, 81920 MiB)** |
| **v52** | **bit-35 绊线（崩溃预言机，无需回读）:stub 在 dmaUpdateVASpace 入口检查 a5 bit35——置位则死循环（GSP 挂=答案可见）,x00 否则重放 prologue 跳回。32B stub,capstone 逐条验证（beqz+8 精确跳过 hang 指令）;probe=13 无 dump 列车** | **boot 健康，vec_scan2 48 跑完，total_bad_units=845545477(stock 墙）,GSP 存活，无 Xid——没有任何 bit35 置位的 vAddr 到达这个函数。两种读法:(a) 函数被调但无 >32G 映射 → rogue -32G 写在它之下（walk/transfer 层，RE 的 0x5027fd0-0x5028254 方向）;(b) 函数在 repro 路径根本不被调 → 测试无效。区分实验（一次 build):bit32 绊线（任何 ≥4G vAddr 都挂）——vec_scan2 必然映射 >4G，若挂则函数在路径上、(a) 成立；若活则 (b),RE 重新瞄准函数识别** |
| **v53** | **probe==14:SEC2 HS(LEVEL2)payload 写 FBPA 配置寄存器判别。3 次 refill+BooterLoad(PLM 循环同款成熟原语）,host 即时读回（DevInit 在其后，immediate≠final 可区分"被还原"vs"没写进")。锚点=PLM 循环后、几何写之前（与 probe10 同位）** | **判别成立：0x9a0250 0x0bb800a1→0x0bb800b1 写入成功且终值保持（imm_rb==target==final);0x9a0294 0x38d4841b→0x3926c525 同样成功且保持；0x9a016c(0x14→0x10）即使 HS 也写不入（fuse/source-ID 锁）。⇒ LEVEL2 能写（部分）FBPA 配置寄存器，修复路径打通。代价：最终 BooterLoad 4/4 次 0x29 快拒（这 3 个值改变了 FBPA 路由，镜像装载/校验在其之后）→GSP 没起来；下次真写入要么选对 boot 无害的顺序/时机，要么接受当次 boot 死（写完即弃）+FLR 再干净 boot 验证。机器已 restore(610.43.02, 81920 MiB)** |
| **v54** | **probe==15:regkey 驱动通用 HS 写工具（RMCmpFbpaAddr/RMCmpFbpaVal，一次一寄存器，换候选零重编）。writability 阶梯：12 候选 × canary(orig^1)× 一次 FLR boot** | **可写：0x9a0224, 0x9a0248, 0x9a03e4, 0x9a0290, 0x9a029c(+v53 的 0x9a0250, 0x9a0294)= 7 个；拒绝：0x9a02c8, 0x9a02d8, 0x9a02e0, 0x9a02e4, 0x9a0164, 0x9a03e8(+v53 的 0x9a016c)= 7 个；0x9a0298 可写但 canary 即杀 boot(0x29 路线——路由关键寄存器实锤）。0x9a0298 死 boot 后首次 modprobe -r 卡死过一次（unload-stuck,FLR 后第二次成功）——阶梯脚本已带该恢复逻辑。第二轮（写 8G 值+wall 探针）未跑，等指令** |
| **v55/v55b** | **真值写入 + wall 探针。v55(8G 卡值，被叫停于 6/8):0x9a0298/0x9a0224/0x9a0248/0x9a029c/0x9a0290 全部写入成功、boot 活、wall actuals 逐位不变。v55b(A100-80G VBIOS 明文表 p10 目标值，9 寄存器全跑）:0x9a0298(0x881b0b11，真值下 boot 活了——canary 死真值活）、0x9a0254（新证可写，真值 0x01cab04a 写入存活）、0x9a029c/0x9a0290/0x9a0224/0x9a0248/0x9a0250/0x9a03e4 全部写入+存活** | **全阴性：所有 9 个寄存器的 wall actuals 与基线逐位一致（sample[20..22] 完全相同）——没有一个单独移动折叠点。0x9a0294 特殊：hook 时 orig 已是 A100 表值 0x39268428(early DevInit 从表写入，post-boot 才变成 10G 值 0x38d4841b——10G 覆盖发生在更晚阶段）,hook 点写它（哪怕同值）必杀最终 BooterLoad(0x29)——它是镜像装载路径关键。FLR 持久性（v55 Phase0)：不持久（纯净 boot 后读回原值，DevInit 重写）→ 杀启动寄存器没有"写入即弃"捷径。下一步方向：组合写入（折叠大概率是多个字段的函数，单寄存器不动）或 FBFLCN/VBIOS 线** |
| **v56** | **组合写入：probe==16 一次 boot 写全部 8 个 A100-p10 值（跳过 0x9a0294;hook=PLM 循环位，HS gadget 唯一可用窗口）。post-boot 采样时刻全寄存器读回 + wall single 40** | **8/8 写入成功且全部活到采样时刻（无晚期覆盖——晚期覆盖只碰 0x9a0294 这类）。但 wall 精确等于基线：bad_qwords=617349120、坏桶 [0,5G) 逐位不变 → 这 8 个寄存器的组合也不编码 20-vs-16 FBPA 折叠。附带两个结论:(a) v55b 的单寄存器阴性追溯有效（值确实活到了采样时刻）;(b) 剩余嫌疑收窄到：写锁的 7 个（0x9a02c8/d8/e0/e4/0x9a0164/0x9a03e8/0x9a016c)、0x9a0294 的晚期覆盖者、或 FBFLCN/VBIOS strap 层。机器已 restore(610.43.02, 81920 MiB)** |
| **v57** | **Step 1 判别：RmDisableFbflcnDevinitBoot=1(regkey 命令行注入）单独一次 boot，读 0x9a0294** | **阴性：boot 健康（610.43.02),0x9a0294 仍是 0x38d4841b(10G 值）——要么该 regkey 没到达/没被 GSP-RM 采纳（host 侧无法直接验证转发）,要么覆盖者不是 FBFLCN devinit（嫌疑转向 SEC2 上的 FWSEC devinit/cert override，或硬件 fuse 直写）。Step 2 按预定决策未跑。剩余路线：host 侧 ROM 表补丁（改 host 上传的 devinit 表镜像，风险=GSP 期重验）或确认 regkey 转发链。机器已 restore(610.43.02, 81920 MiB)** |
| **v58** | **regkey 换引擎判别 FWSEC 路径：boot A = RMExecuteFwsecOnSec2=0;boot B = RMExecuteDevinitOnPmu=1（各自单独 boot，变量不合并）** | **两个 boot 都健康，但 0x9a0294 均为 0x38d4841b 不变——regkey 换路无效，表选择不随引擎变（同一份 cert/fuse 输入喂同一张表）。结论坐实：FWSEC 的 SKU 决策点在 SEC2 上的已签名加密 devinit ucode 内部，host/编排层无任何开关够得着。剩余路线只剩：host 侧 ROM 表补丁（赌 GSP 期对 host 上传表镜像不重验；表区在 MAC 校验区 0x2200-0x43A00 内）或接受 40G。机器已 restore(610.43.02, 81920 MiB)** |
| **v59** | **probe==17:host 侧 VBIOS 表补丁（route C)。注入点=`kgspExtractVbiosFromRom_TU102` 把 ROM 拷进 sysmem 缓冲后、`pVbiosImg->pImage` 赋值前（kernel_gsp_vbios_tu102.c)。CMP ROM(92.00.66.00.02, 0x41800 字节，hook 内 filp_open dump 取回）布局与 A100 不同：0x9a0294 行值在非对齐 0x3bac9/0x3bad1,A100 p10 值本就在 0x3bae9，无 +0x60000 镜像副本。三锚点 verify-then-write。R1:改 0x9a0294 行 2 dword;R2:+RmDevinitDmaUseSPA=1;R3:+canary 行 0x9a0290(0x3ba91)/0x9a0298(0x3bb09/0x3bb11）改 A100 值** | **三轮全部：补丁 100% 打上（patched=2/2、canary 3/3)、boot 健康、GSP 起、全程零 UDE/VERIF/AUTHENTICATE——host 上传镜像不被重验（赌赢了，但没用）。三个寄存器全不变：0x9a0290=0x1255b93c / 0x9a0294=0x38d4841b / 0x9a0298=0x88130b11。canary 全阴性 ⇒ 不是 294 行被选择性覆盖，而是**这张表根本不走我们补的缓冲**。源码实锤（kernel_gsp.c:5549-5558):host 侧这份镜像的唯一用途是 `kgspParseFwsecUcodeFromVbiosImg` 抽出 FWSEC ucode，随即 `kgspFreeVbiosImg` 释放——devinit 表从不离开卡：FWSEC 在 SEC2 上自己读卡 ROM 取表。⇒ route C(host 表补丁）对整个 0x9a02xx 家族死刑：要改表只能改卡上 ROM 本身或拦 FWSEC 的 ROM 读，host 侧无解。附带排雷：v59 交接笔记里的"标准 keys"(RmCmpSecretsOff/RmCmpEnableNsShift 等直传模块参数）是幻觉——全树无任何文件声明它们，正确通道只有 NVreg_RegistryDwords(§5);RmDevinitDmaUseSPA 只存在于 GSP 固件（obj+0x16e8)，host 源码树 grep 不到属正常。机器已 restore(610.43.02, 81920 MiB)** |
| **v60** | **probe==18:R1 窗口判别探针（R1_WINDOW_RECON.md)。arm-time GSP-RM 代码补丁：hook 编排器 vtable 调用返回处 0x4e2e0f0(file 0xe2e0f0，无静态分支落入，疑 FWSEC exec 后/DEVINIT consume 前）,stub 在已证死函数 cave(file 0x1026c34,412B)。stub(gen_probe60.py 生成，capstone 逐条验证）:偷 libos 翻译窗口 idx7(0xdead 毒槽）映射 VA 0x70000000→WPR2 物理基址（arm 时 host 把 WPR2 bounds<<8、gspFwOffset 打进 stub 尾部数据槽）,ELF magic 自检后 4B 步进扫 0x38d4841b→就地改写 0x39268428，命中数/首命中偏移写 PGSP 邮箱 0x110040/44，分阶段进度码定位挂点。Round 1(含压缩指令版）:arm 3/3 OK(stub 204+204B + hook 8B，全 verify),bounds=13f72000/13ffee00(span 0x8ce0000≈148MB),fwBase=0x13fe300000** | **R1:Xid 1 illegal instruction @ pc=0x5026c34=stub 入口第一条——压缩指令（c.addi16sp）在 RM app 上下文即非法指令；trap PC=link va 无 +0x40000 偏置（与 v37/38 的 +0x40000 报告模型不一致，注意）。重建为全 4B 指令版。R2（全 4B 版）:modprobe 后未归，随后整机冻结（ssh 死透 >4 分钟，3 次 ConnectTimeout)，无任何本轮数据取回（arm 行/mailbox/Xid 全丢）——按纪律停机待冷启动。R2 无数据，不能计为矩阵任何一行；冻结机制未明（该 cycle 的 FLR 因 sudo 抽风实际未执行，叠加嫌疑：无 FLR 加载 + 148MB 窗口扫描）。待冷启动后：可选一次带逐 1MB 进度上报 + 旁观轮询的重试，若同点再冻 = 扫描挂 = U1 否 = R1 关闭。模块已装（无 regkey 时惰性），机器待冷启动恢复** |
| **v60-retry** | **冷启动后重试（stub v3：全 4B 指令 + 逐 1MB 扫描进度滚写 mbox1 + cycle 修 sudo/FLR):arm 3/3 OK,bounds/span/fwBase 同前，后台 1s 轮询 mailbox** | **决定性环境结论（非矩阵行）:Xid 1 illegal instruction @ pc=0x5026c88 = stub 的第一条 `csrw 0x7c8`（窗口选择 CSR)——**RM app 上下文对 libos 翻译窗口 CSR(0x7c8-0x7cc）无访问权限**，stub 编程窗口路线死。进度码未及写出（trap 在 entry 报告之后、首个 1MB 进度之前；轮询只见硬件噪声）。推论链：(a) 无 CSR 权限 ⇒ stub 既不能自建窗口也不能枚举现有窗口；(b) existing-window 备选只覆盖镜像自身段（text/rodata/data ~28MB),FWSEC staging 区（heap/FRTS,WPR2 低 132MB）无任何已知 VA 映射覆盖——部分扫描的零命中不可判读，不值一次 boot;(c) agent-4 的加分项 (b)("GSP-RM 直读 37-40G FB 已证"）复核为 host 侧 phantom 带取证，**GSP-RM app 上下文直读任意 FB 从未被证明**。R1 悬挂在最后一个未证前提上：需要 RE 找到一个 app 可读、覆盖 WPR2 staging 区的现有 VA 窗口（或证明不存在 ⇒ R1 关闭）；或改走 stub 内 SEC2 DMATRF 读回通道（自编程 DMA + DMEM 端口读，大工程）。**agent-6 背景（列裁决）:v60 canary(0x38d4841b→0x39268428）仍是有效窗口判据；但若窗口成立，真修复载荷 = 列 7/8 的 19 dword(A100_40G_80G_COLUMN_DIFF.md)，不是列 10 值；v55b/v56 的"写 A100 值墙不动"因取错列（列 10 而非 BCAST 列 4）需重新解读——BCAST 列 4 三 ROM 全同，那 9 个寄存器本来就不是杠杆**。机器已 restore(610.43.02, 81920 MiB，标准 keys)** |
| **v61** | **R1 终判探针（R1_VA_WINDOW.md),probe==19，触发点同 v60(0x4e2e0f0 编排器 vtable 返回）。回传通道实测：R1:mailbox magic 写 0x110440/444/040——host BAR0 全程读不到（GSP 健康、无 fault、写入执行了但不可见）⇒ **mailbox 通道死**。R5（合并通道验证+安全锚点）:fwversion 通道实锤可用——stub 把（值+0x01010101 防 NUL）写进 6 个 610.43.02 副本（PH26 rodata,app 可写）,/proc Firmware 行携带出站（1×32bit/boot);[0x43c9d18] logger 缓冲指针 = **0x5c2b000**（与 v41 dump 吻合，双确认）。R6：风险读 [0x400d638](heap 窗口描述符）→ **load access fault @ ld** ⇒ app 读不到 libos 全局。R4(heap 指针回退扫描）：以 0x5c2b000 为基址扫 64MB → **首条 lwu 即 load access fault** ⇒ 该 VA 在触发点未映射（v41 就疑其 stale/alias)。R8：池描述符 [0x438ab28,+0x200) 扫描无可用作堆基址的指针（全字段无 ≥0x5c00000 的值，marker "...." 如期出现=通道自证）** | **R1 终判：四个锚点全部断线——无窗口 CSR 权限（v60-retry)、libos 全局不可读（R6)、logger 堆 VA 在编排器时刻未映射（R4)、池描述符无堆 VA(R8)。app 上下文找不到任何覆盖 FWSEC staging 区的可读 VA 窗口，"staging 在 heap 窗内"的赌注连下注的桌面都够不到。按矩阵精神计为**探针设计层面死**(Round 2 本身没 trap，但其取到的指针在扫描时刻未映射，等价效果）。R1 实际关闭，除非 RE 能找到存放 staging FB 指针的确切 GSP-RM 结构（则无需扫描，直取）或接受 stub 内 SEC2 DMATRF 读回大工程。通道资产（后续探针可直接用）:fwversion 回传通道（1×32bit/boot,GSP 健康前提）+ trap-PC 定位（Xid 报 link va)+ probe==19 通用 arm 机构（gen_probe61.py 迷你汇编器，I 型立即数越界已加断言）。机器已 restore(610.43.02, 81920 MiB，标准 keys)** |
| **v62-acceptance** | **生产收尾 build + 全项验收。build.sh 新增 `CMPUNLOCKER_PRODUCTION=1` 门（跳过 apply_pt_log/pma_alloc_log/pte_map_log/wpr_rmw_probe/sec2_dma_probe 五个纯探针/日志生成器；保留 profile、phantom reserve、tail-steer、radix3 hook;flag 计入 BUILD_STAMP)。构建：`CMPUNLOCKER_PRODUCTION=1 CMPUNLOCKER_STRIP_POST0808=1 CMPUNLOCKER_DRIVER_VERSION=610.43.02 CMPUNLOCKER_CARD_PROFILE=10gb80`。洞 = 5G phantom reserve [36G,41G)(08-09 最终版，dmesg 实证 pinned)。加载 = 裸 modprobe(modprobe.d 的 cmp-pcie-gen2.conf 供标准 key；零 RMCmp* 探针 key)** | **全绿:**(1) nvidia-smi 81920 MiB/610.43.02/Firmware 正常；裸 modprobe 即生产态（modprobe.d 供 key 机制验证）。(2) SS0/SS1=0x88888888/0x00000008、CFG1=0x02779000、LMR=0x28b 全部读回达标；bench_matmul(gpuenv/torch):BF16 166.5 / FP16 168.9 / FP32 12.26 / FP64 12.02 TFLOPS = 满血（对照 nerfed 基线 6.15/6.26/0.39/0.20)。**勘误记录：SS0/SS1 的 0x88888888/0x8 就是生产正确值；A100 真值 0x00112011/0x2 是 B4 的错路（CMP 硅上会重新点燃节流）——别再来回翻**(ARCHITECTURE.md)。(3) wall_reconfirm2:single 40 bad_qwords=617349120（坏桶[0,5G))、single 60=3301703680、single 72=4697620480——与文档基线逐值一致（预期行为）。(4) vec_scan2:32G total_bad_units=0（用户分配安全）;48G=845545477(>40G 单对象，墙固有，判词 §5 已记）。(5) gpu_burn 300s:0 errors,12249 Gflop/s 持续，实占 68GB。(6) llama:小模型 32K 连贯；Q8-27B+32K 连贯；**Qwopus Q6_K-27B + `-c 262144 -np 1 --cache-type-k q8_0 --cache-type-v q8_0`(35.7GB）连贯输出**——256K 长上下文生产可行配置落地；反例机制钉死：bf16-51G 模型对象跨洞 / Q8+256K-f16-KV(~15G）跨洞 → 胡言（确定性同错串），洞下 [5G,36G) 全装下即连贯。(7) 全程零 Xid。**机器留在生产解锁态（驱动已载，81920 MiB)** |

## 4. 剩余路线（按可行性排序）


1. **论文的 HS exploit**:SEC2 booter DMA length 溢出 → canary/返回地址覆盖 → LEVEL2 任意 PC → 重编程 PLM → host 直写。同一硬件上被论文证明过；前置（booter 布局、签名 refill 通道、PLM 位置）已齐。
2. **驱动侧绕过（不动 WPR)**:`RMEnablePmaManagedPtables=0`、GSP heap 扩容（RMGspFirmwareHeapSizeMB)、洞尺寸/位置调整——让页表分配避开 32G 墙区域，可能根本不需要 Patch A。
3. **隔离 v28/v31 挂点**（小实验）:enables-only 内联 stub（无 DMA）从未单测——如果 enable 写本身才是挂点，xdst 路线还有救。

## 5. 复现实验命令

```bash
# 标准探针启动(FLIR + 命令行 regkey,绝不落盘 modprobe.d)
modprobe -r nvidia_drm nvidia_modeset nvidia_uvm nvidia
echo 1 > /sys/bus/pci/devices/0000:3d:00.0/reset; sleep 2
modprobe nvidia NVreg_RegistryDwords="RmForceEnableGen2=1;RMPcieLinkSpeed=0x1;RMDisableScrubOnFree=1;RMCmpSec2DmaProbe=1"
modprobe nvidia-modeset; modprobe nvidia-uvm
# 看结果
journalctl -k -b | grep -E "CMP_SEC2|CMP_WPR|CMP_INLINE"
```

构建：`cd driver && CMPUNLOCKER_STRIP_POST0808=1 CMPUNLOCKER_DRIVER_VERSION=610.43.02 CMPUNLOCKER_CARD_PROFILE=10gb80 sudo ./build.sh`（需要 forgive 时去掉 STRIP)。

v33 流程教训：WPR DMA 超时（0x65）后 GSP 起不来时**不要跑 nvidia-smi**——它会 D 住并持有 fd，导致 `modprobe -r` EBUSY、unbind 也 D 住，`systemctl reboot --force[-force]` 都会被 systemd-shutdown 卡死，只能 `echo 1 > /proc/sys/kernel/sysrq; echo b > /proc/sysrq-trigger` 紧急重启。判 GSP 死活用 `cat /proc/driver/nvidia/gpus/0000:3d:00.0/information | grep Firmware`（N/A=死）;wpr_st!=0 的 cycle 直接 modprobe -r + FLR 进下一个，不碰 nvidia-smi。另：probe hook 在 modprobe 返回后 ~3-12s 才打日志，dmesg 要延迟再抓。
