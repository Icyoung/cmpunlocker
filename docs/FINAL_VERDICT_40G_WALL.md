# 40G 墙最终判词(2026-08-11)

> 本文是 80G 解锁显存线的**终审结论**。实验细节见 `EXPERIMENTS_20260810.md`(v44–v61),逆向细节见 `re/` 各文档。一句话:**物理 80G 存在且已可见;40G 折叠墙的决策点在 SEC2 加密 FWSEC devinit ucode 内部,所有可达层的干预路径已被穷尽排除;按既定决策树,接受 40G 有效墙收尾。**

## 1. 战果(已落袋)

- 显存容量:10G SKU 识别为 81920 MiB(80G 物理存在,几何解锁成功)。
- 算力:SM math rate 全速(SS0/SS1 修复,最终验收以干净 build 复核为准)。
- PCIe:Gen1→Gen2(fuse-shadow 路线,生产 regkey 生效)。
- 墙的完整刻画:折叠点 40G = 20 FBPA × 2G/FBPA;折叠签名 = +35 GiB + 256B 粒度 tail,tail ∈ {0x900, 0xB800, 0xBC00, 0x11400},分段周期 8G(HBM die 粒度)。见 `WALL_ALIAS_DECODE.md`。

## 2. 根因(精确定位)

- CMP 与 A100 的 FBPA 配置差异**不在 BCAST 列(列 4,三 ROM 全同)**,而在 devinit 明文表的**列 7/8 = per-partition 值,共 19 个 dword**(`re/A100_40G_80G_COLUMN_DIFF.md`)。
- SKU 选择逻辑:FWSEC devinit(SEC2 上,已签名加密 ucode)按 fuse/device-id 从卡上 ROM 表选 CMP-10G 行——这是"SKU 落地"的确切位置(`re/LATE_OVERRIDE_0294.md`)。
- 投递路径封闭:host PL0 写 FBPA 配置全 REJECTED;SEC2 HS 可写 8 个可写寄存器但**它们不是杠杆**(列 4 原生=A100);7 个写锁寄存器的 A100 值不在表超集内。

## 3. 排除清单(每条都有实证,非"没试对")

| 路线 | 实验 | 结局 |
|---|---|---|
| 可写 FBPA 寄存器单写(9 个,A100 目标值) | v55b | 墙逐字节不动;持久性后经 v56 复核有效 |
| 组合写(8 个同 boot,采样时刻全量读回确认值活着) | v56 | 墙不动,面排除;后证所取"列 10"值本非杠杆(列 4 原生即 A100) |
| RmDisableFbflcnDevinitBoot=1 | v57 | flag 落 obj+0x1b9,不 gate 任何 FBPA 写;覆盖者非 FBFLCN |
| RMExecuteFwsecOnSec2=0 / RMExecuteDevinitOnPmu=1 换引擎 | v58 | 均无效;决策点在加密 ucode 内部终锤 |
| host 改表(RmDevinitDmaUseSPA=1 + 上传镜像补丁) | v59 | 认证不拦(赌赢),但 FWSEC **自己读卡 ROM**,表根本不经 host 缓冲 |
| 刷改卡上 VBIOS(改 CMP 行) | 风险评估 | 表区在 MAC 区内(RSA-3072+SHA-256),8–9 成被拦;且锁寄存器目标值未知,生效≠墙动 |
| 整刷 A100/ES VBIOS | 推断 | SKU 选择 fuse 驱动;A100 表内自带 CMP 行,刷了仍选 CMP 行,零收益或 MISMATCH halt |
| R1:WPR2 staging 副本运行时补丁 | v60/v60-retry/v61 | RM app 无窗口 CSR 权限(第一条 csrw 即 Xid 1);libos 全局不可读;堆 VA 锚点全断——app 层物理够不到 staging 区 |
| PCIe Gen3/Gen4 | RE | 明文表无 XP 行;能力值来自 fuse 驱动寄存器;无表可补、无分支可打;PHY 校准层与显存同墙 |

## 4. 理论上剩余但未走的出口(存档,不建议)

1. stub 内自建 SEC2 DMATRF 读回通道(寄存器窗口编程 DMA + DMEM 端口读)——工程量翻倍,新失败面多;
2. 挖 GSP-RM secureboot 派发 CU(0x4ea7xxx)找 staging 指针结构——找到也大概率是物理地址,app 依然无窗口解引用;
3. VBIOS strap-4(0x41D53)+ MAC 伪造(论文 DFA 路线)——天级;
4. FBFLCN/HBM MRS 内部状态——论文实证该侧存在 source-ID 隔离,SEC2 HS 都不够。

## 5. 生产配置(最终态)

- 驱动:610.43.02 干净 build(无探针日志),`CMPUNLOCKER_STRIP_POST0808=1 CMPUNLOCKER_CARD_PROFILE=10gb80`。
- 显存:80G 可见 + phantom reserve 挖洞(GSP 元数据带 PMA pin [36G,41G) 5G,`driver/apply_phantom_reserve.py`)——洞的作用是防止用户分配踩进折叠区/元数据带,**从来没修过数据折叠**。
- 有效可用显存:~75G 可见、~40G 连续安全区(40G 以上单对象不可用,洞下分配安全)。
- regkey(仅 RegistryDwords,不落盘):`RmForceEnableGen2=1;RMPcieLinkSpeed=0x1;RMDisableScrubOnFree=1`。
- 验收标准:v62-acceptance(nvidia-smi / SS0/SS1 / wall_reconfirm2 / vec_scan2 / gpu burn 5min / llama -c 262144)。

## 6. 已知注意事项

- 历史"60/67G PASS"记录全部是折叠自洽假象,勿引用。
- 0x9a0210 是挥发状态寄存器,分析时剔除。
- 换卡/断电后 GSP 状态必须冷启动才干净;FLR 不持久化 FBPA 配置(v55 实证)。
- 若未来 NVIDIA 更新 FWSEC 或社区出现 GA100 签名绕过,第 4 节出口可重估。

## 7. 附录:为什么除"签名固件"与"fuse 烧写权"之外原理上不可解锁

**SKU 配置在信任链内部一次性完成,且输入全部经过认证。** GA100 的启动是一条硬件信任链:片上 Boot ROM(不可变)→ 验签并解密 FWSEC ucode(RSA-3072 签名)→ FWSEC 在 HS/LEVEL2 域读取 eFuse(device-id/SKU 标识)与卡上 VBIOS(MAC 保护区域),按 `config = FWSEC_select(fuses, ROM_table)` 选定 devinit 配置并应用到 FBPA 路由、每分区几何、PCIe 能力等寄存器,随后落下 PLM(Privilege Level Mask)与 WPR 保护。从这一刻起,host(PL0)、驱动、乃至 GSP-RM 全部位于这组资源的 TCB 之外。配置函数的三个要素——执行体(签名加密 ucode)、选择输入(fuse)、数据输入(卡 ROM)——没有一个经过任何外部可达层。

**所有可达层都被各自隔离机制钉死——这不是工程难度问题,是架构问题:**

- **host PL0**:对 FBPA 配置域的写被 PLM 直接 REJECTED(含自锁位 0x9a014c);
- **SEC2 HS(LEVEL2,论文级原语)**:能写部分 shadow 寄存器,但 BCAST 可见寄存器不编码 SKU 差异(三 ROM 列 4 全同);真正承载差异的 per-partition 状态与写锁寄存器由 fuse/source-ID 门控,LEVEL2 不可达;FB/HBM 侧存在 source-ID 检查(仅 FBFLCN 身份被接受),SEC2 的 LEVEL2 也不满足;
- **GSP-RM 运行时补丁(post-auth WPR 注入)**:FWSEC 在 WPR2 staging 的表副本对 GSP-RM app 层无任何 VA 映射(无窗口 CSR 权限、libos 全局不可读、无活堆指针),TOCTOU 窗口在可达层不存在;
- **host 侧数据通道**:FWSEC 直读卡上 ROM,host 上传镜像只用于提取 ucode 后即释放,数据不过 host;
- **驱动 regkey**:仅在 fuse 驱动的能力值之下做钳制,选择逻辑无编排层开关。

**配置生效后锁闭,无残留攻击面。** FWSEC 应用配置与 PLM/窗口锁闭发生在同一启动阶段;boot 完成后不存在"配置尚可写"的时间窗,boot 期间该窗口只存在于认证域内部。

**PCIe Gen3/4 同理且更深。** 链路能力寄存器在复位时由 fuse 直接驱动(LNKCAP 只读反映);Gen2 可解是因为其 PHY 配置仍存在于 LEVEL2 可写的 shadow 寄存器;Gen3+ 需要独立的 TX/RX 均衡校准数据,该路径在此 SKU 被 fuse 裁掉——明文表中无对应行,固件中无可补丁分支,能力值无软件来源。

**因此剩余的充分条件只有两个,且都等价于"成为权威机构":**

1. **一份签名有效的固件/VBIOS,使其认证输入天然选择目标配置**——需要 NVIDIA 签名私钥(表区在 MAC 覆盖内,改动即验签失败);
2. **fuse 烧写权限(eFuse/HULK cert 签发)**——直接改变 device-id/SKU 选择本身。

换言之:这张卡的 SKU 边界不是软件策略,而是**由硅片信任根锚定的认证启动链的一个输出**。任何不掌握签名权或 fuse 权的路径,都只能在链外观察结果,无法参与决策。
