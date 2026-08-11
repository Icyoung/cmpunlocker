# R1_VA_WINDOW — RM app 上下文是否存在覆盖 WPR2 staging 的可读 VA 窗口

> 2026-08-11。纯本地 RE。上游判定书:R1_WINDOW_RECON.md;硬件事实:v60/v60-retry
> (app 对窗口 CSR 无权限;现有窗口只覆盖镜像自身 ~28MB;WPR2 = FB 顶 148MB,
> [0x13f7200000, 0x13ffee0000),fwBase=0x13fe300000;staging 在 WPR2 低 ~132MB
> = heap/FRTS 区)。
>
> **裁决:窗口存在性 = 确定有(heap 窗口必须存在,否则 RM 无法运行);
> 覆盖 staging 的概率 = 中高(staging 就在 heap/FRTS 区);
> 精确 VA 基址静态定不了(运行时算进 libos 全局)。
> 给最小只读探针(§4),一次 boot 裁决。**

## 1. 窗口机制还原(libos 段,PH0)

CSR 布局(全部窗口写点枚举自 disasm_libos.txt):

| CSR | 角色 |
|---|---|
| 0x7c8 (1992) | 窗口索引 |
| 0x7c9 (1993) | **VA 基址 \| enable(bit0)** |
| 0x7ca (1994) | 物理基址 |
| 0x7cb (1995) | 大小 |
| 0x7cc (1996) | 属性(0xc0600/0xc0200/0xc0e00/0x600…,疑含目标空间+权限) |

设施:直接写点(0x4005000 自举序列)+ helper 0x40057f4(set_window)+ 通用窗口管理器
0x400b45c(及其 +0x4000 副本 0x400f45c,另一份在 0x4007040 起 —— 镜像里有两套 libos
代码副本)。app 无 CSR 权限(v60 实锤,illegal instruction 而非 access fault ——
指令本身被 privilege 拦,与"窗口归 libos 管"一致)。

libos 自举窗口(静态值):idx3 = [0x4000000, +0x13000) 恒等(libos 自身);
idx7 = 0xdead 标记的禁用槽;idx8 = **catch-all [0, 4G), attr 0xc0e00**。

## 2. app 窗口与全局 0x400d420/0x400d638

app 的窗口由 loader 经管理器安装,运行时计算。其中 **idx4 = 大窗口**:
- VA 基址 = **[0x400d638]**;物理基址 = VA + [0x4002080];大小 =
  (**[0x400d640]** + 1) << 5(32 字节单位);attr = **[0x400d420]** | 0x40400。
- 写入方:0x400562c/0x400563c(init 默认:base=0x4001078, size_field=0x7f);
  **0x4006344/0x4006354**(per-app loader:base = arg0 − [0x4002080],
  size_field = arg1 − 1 → size = arg1×32)。arg1×32 的量级与 GSP heap
  (112MB = 0x7000000 → arg1 = 0x380000)吻合 ⇒ **idx4 ≈ heap 窗口**。
- 这些全局在 libos 数据段(va 0x400dxxx)——app 是否可读取决于 app 上下文有没有
  覆盖该 VA 的窗口(catch-all 是否随 app 上下文安装 = 未知,见 §4 探针)。

## 3. RM 正常业务的 FB 访问形态

- **heap:直读直写,走 idx4 类 VA 窗口**(确定 —— allocator 0x58bb668 的 pool
  描述符在 app 数据 va 0x438ab28,返回的指针被直接 ld/sd)。
- **FB 内容(页表/FRTS 等):走 DMA 引擎**(memmgrMemBeginTransfer/EndTransfer
  影子缓冲 + DMA 提交),RM app 对任意 FB 的 CPU 直读**从未被证明**
  (v60 复核正确:phantom 带取证是 host 侧行为)。
- 绝对地址直方图:app 代码的 lui 绝对寻址 99.9% 落在镜像 VA 区
  ([0x4000000,0x6000000)),heap 访问全部经指针(运行时值,静态不可见)。

## 4. 裁决与最小只读探针

**静态能定**:heap 窗口存在(必然);staging 区物理上就在 heap/FRTS 所在的
WPR2 低区 —— 若 FWSEC 的拷贝落在 heap 窗口覆盖的物理页内,则经 heap 窗口可读。
**静态定不了**:heap 窗口的 VA 基址(运行时算进 [0x400d638])与其确切物理覆盖。

**探针设计(boot 一次,纯只读 + 一次命中才写):**

1. 触发点:沿用 R1_WINDOW_RECON 的 boot 候选点(0x5b2fe00 / 0x5b2b724 入口 /
   0x4e2e0f0;staging 之后、heap 覆写之前)。
2. stub(放已证死函数 0x5026c34,全部 4 字节指令):
   a. **安全锚点先读**:app 数据全局 —— [0x43c9d18](logger 缓冲指针 = 一个活
      heap VA;若 logging 未启用为 0 则退到 heap 描述符 0x438ab28 逐字段扫
      非零指针)。这些在 app 数据窗口内,不会 fault。
   b. **次安全**:[0x438af28](寄存器窗口基址,EXFIL_RE 已用)——顺便再次自证。
   c. **风险读(放最后)**:[0x400d638]/[0x400d640](libos 窗口描述符)——
      若 fault 即知 app 上下文读不到 libos 段(本身是个结论)。
   d. 从 heap VA 基址扫 `1b 84 d4 38`(CMP 表值 LE);命中 → 就地改
      `28 84 26 39` 并计数。
3. **回传通道排序**:
   - 首选 **PGSP mailbox(bus 0x110440/0x110444,经 [0x438af28]+0x110000)**:
     host BAR0 随时可读,不依赖 post-boot dump(已死)也不依赖 dmesg。
     ⚠️ 该通道出自 EXFIL_RE.md 设计,可能未单独实测过 —— 探针第一轮应先只写
     固定 magic 验证 mailbox 回传本身,再做扫描。
   - 备选:**trap-PC 编码**(找到/没找到让 stub 跳到不同地址,Xid 1 的 mepc 携带
     结果;代价 = GSP 死,所以只作终局手段)。
4. 结果判读:mailbox 有值 = heap VA 已知 + 窗口可读;命中数>0 = staging 在窗内
   → 换正式补丁;0 命中且 boot 正常 = staging 不在 heap 窗口覆盖 → R1 关闭;
   扫描中 hang = 窗口边界越界(stub 需按 [0x400d640] 限长,或分段试)。

## 5. 与 R1 判定书的关系

本侦察回答的是判定书里的 U1(WPR2 是否可达)的可达层:heap 窗口**大概率**
覆盖 staging 区(同处 WPR2-low heap/FRTS 区),所以探针直接赌"在窗内"。
若探针 0 命中,再排除"范围算错"(用 [0x400d640] 算出的窗长复核扫描范围)后,
R1 正式关闭,转接受 40G 收尾。
