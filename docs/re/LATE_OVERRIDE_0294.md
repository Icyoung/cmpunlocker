# LATE_OVERRIDE_0294 — 0x9a0294 晚期覆盖者定位（GSP-RM 610.43.02, tu10x）

> 2026-08-11。纯静态 RE。结论:**覆盖写入不在 GSP-RM 代码里——GSP-RM 对 0x9a0294
> 零访问(已穷尽证明)。覆盖者是 GSP boot 期的 falcon-ucode devinit 路径
> (FBFLCN devinit boot / FWSEC-HULK cert override),GSP-RM 只做编排。
> 最佳切入点不是固件补丁,是 host regkey `RmDisableFbflcnDevinitBoot` 或
> host 侧 ROM 表补丁。**

## 1. 否定证据(GSP-RM 不写 0x9a0294)

- 值常量:`0x38d4841b`(CMP-10G 目标值)在 ELF 的 data 与 code 中**均不存在**;
  同为 ROM 表的 0x1255b93c / 0x88130b11 / 0x24002b4a / 0x0801900c 也全部不存在。
- 地址常量:`0x9a0294` 全形式扫描为零 —— 绝对 dword、`lui 0x9a02`+`addi 0x294`、
  `lui 0x9a03`+负偏移、regbase+0x294、以及全部 `sw …, 0x294(reg)` 站点
  (0x53baab8/0x53bb928/0x5491f84/0x55a57c8…)逐一核查均为软件结构体字段写。
- 同族寄存器 0x9a0290/298/29c/250/254/248/224/3e4 同样零访问。
  ⇒ 该写由**数据驱动的表解释器**完成,解释器不在本 ELF 内。

## 2. 值来源:ROM 明文表(超集) + SKU 选择

两 ROM(283224=A100-80G、282161=GRID-32G)在**相同偏移**含两个值:

| ROM 文件偏移 | 值 | 身份 |
|---|---|---|
| 0x4007c, 0x40084 | `0x38d4841b` | CMP-10G 行值(=晚期覆盖写入值) |
| 0x4009c | `0x39268428` | A100-80G 行值(=早期 DevInit 留下值) |

邻近同族值(同一寄存器族的不同 SKU 行):`0x39560299`(0x40074/78/80/98/a0…)、
`0x3957c52a`(0x40088/90)、`0x391705a6`(0x4008c/94)。
**表是全 SKU 共享超集;选择由 devinit 引擎按 device-id/fuse/cert 完成。**
佐证字符串:`NV_UDE_ERR_DEVICE_ID_WITH_DEVINIT_TABLES_MISMATCH`、
`NV_FWSECLIC_ERR_CODE_CERT_T7_REG_OVERRIDE_TYPE_UNKNOWN`、
`CERT_HULK_REG_(RMW_RBV_)READ_BACK_VERIFY_MISMATCH` —— devinit 表按设备 ID 匹配,
且 cert 可携带 register-override/RMW+readback-verify 负载。
(注意:表区 0x40000–0x40600 落在早前认定的 MAC 校验区 0x2200–0x43A00 内 ——
host 侧改表的可行性取决于 GSP 期 devinit 是否对 host 上传镜像重验,见 §4-C。)

时间线自洽:power-on DevInit 先把"默认/A100 行"写进 0x9a0294(我们 hook 时刻读到的
0x39268428),GSP 启动阶段的 secureboot/fbflcn devinit 再按 SKU 认证结果改写为
CMP-10G 行值 —— **"SKU 决定落地"就是这一步**。

## 3. GSP-RM 侧的编排链(补丁考查对象)

- **0x4e2ddec** = devinit 路径编排函数:一次性 guard(`lbu 0x28b(a1); bnez → skip
  整段`),读取全部路径 regkey:`RMDevinitBySecureBoot`(×4 处)、
  `RMExecuteDevinitOnPmu`、`RMExecuteFwsecOnSec2`、`RmDevinitDmaUseSPA`,
  并调用 **0x50e75c4**;
- **0x50e75c4** 读 **`RmDisableFbflcnDevinitBoot`** → 写标志字节 obj+0x1b0/0x1b1
  (0x50e767c/0x50e7680),gating FBFLCN 的 boot 期 devinit;
- 编排器的调用者:**0x5b2ded4**(调用点 0x5b2e404),受 obj+0x70d 标志 gating,
  处于 GSP boot 主流程中(同一函数内做 WPR/boot 簿记)。
- 编排函数本身**不做任何 MMIO 写**(全 span 核查)——它只是配置/派发;
  真正的寄存器写在 falcon ucode(SEC2/PMU/FBFLCN 上跑的已签名 devinit/FWSEC app,
  加密 bindata,不在本 ELF)里。

## 4. 切入点(按风险从低到高)

### A. host regkey:`RmDisableFbflcnDevinitBoot=1`(首选)

零固件风险。若晚期覆盖来自 FBFLCN devinit boot,此 key 直接跳过它。
**判别实验(一次冷启动)**:加 key 启动 → 读 0x9a0294:
- 保持 0x39268428(A100 值)⇒ 覆盖者就是 FBFLCN devinit boot,路径实锤;
  随即跑 wall_reconfirm2 single 60/72 看折叠是否移动。
- 仍是 0x38d4841b ⇒ 覆盖者在 FWSEC/HULK cert 路径,转 B。

注意与 v55b 的区别:v55b 是 boot **后**回写 A100 值(全阴)——那改变不了
FBPA 已按 10G 值完成的派生初始化;本路线是让 A100 配置**全程不被覆盖**。

### B. host regkey:路径切换 `RMDevinitBySecureBoot=0` / `RMExecuteDevinitOnPmu=1`

强制换 devinit 执行路径。风险高:整个 secureboot devinit 是 GSP 启动必需环节,
可能直接开不了机;只在 A 阴性后试。

### C. host 侧 ROM 表补丁(若 GSP 期 devinit 读 host 上传镜像)

`RmDevinitDmaUseSPA` 的存在暗示 devinit 可经 DMA 从 **sysmem** 取 VBIOS ——
那镜像由 host 驱动上传,**host 可在上传前把 0x4007c/0x40084 的
0x38d4841b 改成 0x39268428**(连同镜像副本 0xa007c/0xa0084)。
零固件风险。风险点:devinit 表认证(DEVINIT_TABLE_AUTHENTICATE_* 错误族存在),
且表区落在早前认定的 MAC 区(0x2200–0x43A00)内 —— 是否重验只有试了知道;
若认证拦,报 `NV_UDE_ERR_VBIOS_VERIF_COMPLETED_AND_FAILED` 类错误即知。

### D. 固件补丁点(仅当 A–C 都不可行)

- 0x4e2de24:把 `lbu s3, 0x28b(a1)` 后的 `bnez s3, +0x340` 改成无条件跳过
  (改成立即返回)——跳过整段编排。**范围太大**(整段还负责别的 boot 配置),不推荐。
- 0x50e767c/0x50e7680:把 `sb zero, 0x1b1(s1)` 等 flag 写改成恒置位 ——
  等效于 regkey A,只在 regkey 被编译裁掉时才需要。

## 5. 结果解释预案

- A 实验后 0x9a0294 保持 A100 值 + 墙移动 ⇒ 命中,后续做固化。
- 保持 A100 值但**墙不动** ⇒ 0x9a0294 不是深度寄存器(或深度在
  FBFLCN/HBM MRS 内部状态,MMIO 不可见)—— 回 VBIOS/FBFLCN 线决策
  (docs/WALL_ALIAS_DECODE.md 的 H1 细化)。
- A/B/C 全阴 ⇒ 覆盖者是 cert override 且认证绕不开;剩余路线 = 在 GSP boot 后、
  首次大映射前用 host BAR0 写值 + 触发 FBPA 部分重初始化(若有这样的寄存器/
  序列 —— 需要进一步从 A100-80G 真机 dump 反推)。

## 6. v57 消歧:flag 消费方追踪(任务 1)

**字符串实证**:ELF 中是 `RmDisableFbflcnDevinitBoot`(file 0xabdc8,逐字一致)。
邻居键:`RmEnableFbflcn`(0xabdb8)、`RmMClkSwitchOnFbflcn`(0xabde8)——三者都在
0x50e75c4 被读。host→GSP registry 链路已对(自建 RMCmp* 已证),所以"设上了但没
gate 住"成立。

**flag 精确映射**(0x50e75c4 全量还原):

| regkey | 落点 | 语义 |
|---|---|---|
| RmEnableFbflcn | obj+**0x1b0** | FBFLCN 总使能(0x50e7724/0x50e7734 写 1/0) |
| RmDisableFbflcnDevinitBoot | obj+**0x1b9** = !key(==1→0x1b9=0 @0x50e771c;==0→0x1b9=1 @0x50e772c) | "fbflcn devinit boot 使能" |
| RmMClkSwitchOnFbflcn | obj+**0x1b3**(0x50e7704) | MClk 切换相关 |

**0x1b9 的全部消费方**(fbflcn 管理 CU 0x50e7xxx–0x50e9xxx,同对象双 flag 交叉确认):

- **0x50e8d8c**:配置 fbflcn 子对象标志(0x26a/0x26b/0x26d ← 0x1b5/0x1b4/0x1b6)后
  **无条件调用 0x5193d00**;0x1b9 的检查在调用**之后**(0x50e8e28),只 gate 一个
  事后分支(0x1b7 相关)。⇒ 该 regkey 根本不 gate 这个调用。
- **0x50e7798**:注册 fbflcn 命令回调(0x50e7bb4/0x50e7dd4,经 0x51f1990);
  0x1b9==0 时走 0x50e791c 路径,对 0x566c930 发一个 arg=0x22 的替代命令——
  是"换一个动作",不是"跳过 FBPA 配置写"。
- **0x50e9af8**:状态/dump 路径,无关。

**结论 = 情形 (a)**:这条路径上没有任何 FBPA 配置写被 0x1b9 gate;v57 实测
(regkey=1 后 0x9a0294 仍被覆盖为 0x38d4841b)与之完全自洽 ⇒
**覆盖者不在 fbflcn-devinit-boot 路径,在 SEC2 上的 FWSEC devinit app**
(与 nouveau "GA100 devinit 全走 FWSEC" 一致)。FBFLCN 线放弃。

## 7. FWSEC 触达面评估(任务 2)

### 编排器统一调度,但粒度太粗

0x4e2ddec 一个函数统一写下两条路径的全部 flag:obj+0x270(RMExecuteDevinitOnPmu)、
obj+**0x27f**(RMExecuteFwsecOnSec2)、0x280/0x281(RMDevinitBySecureBoot 与 vtable
能力查询组合)、0x282(RMSbProgressProfiling)、obj+0x1678(RmDisableFwseclic)、
obj+0x16e8(RmDevinitDmaUseSPA)。

flag 消费方 = secureboot 派发 CU(0x4ea7xxx):0x27f 置位时,经
`ld a1, 0x1c8(pGpu+0x2000)`(SEC2 引擎对象)→ `vtable+0x428` 发射 app
(0x4ea7fac、0x4ea8194 一带;派发器带 phase 检查 `0x290(obj)==3`)。

### 补丁点存活性评估

| 点 | 效果 | 存活性 |
|---|---|---|
| guard 0x4e2de24 / call site 0x5b2e404 NOP | 跳过整个编排器 | **差**——编排器同时配置一大堆子系统 feature flag(不止 devinit),跳过≈半个 GSP init 缺失,大概率起不来 |
| 派发点 jalr(0x4ea7fac 等)NOP | 禁 FWSEC app 发射 | **差/不可分**——RM 固件启动与 devinit 表应用都走 FWSEC 发射;静态无法区分哪个 jalr 是"devinit 表应用"、哪个是"RM start"。NOP 错 = GSP 死 |
| cert/override 应用点 | — | **不可达**:在 SEC2 加密 ucode 内 |

没有存活性高的固件补丁点。如果一定要固件级实验,唯一可考虑的顺序是:
先 NOP 0x5b2e404 看 GSP 是否活着(若活,说明编排器可被整体旁路,再细分);
预期会死,只是死法有信息量。

### Route C:VBIOS/表镜像来源路径

- `RmDevinitDmaUseSPA` → obj+0x16e8。该 key 的存在本身暗示**默认路径不是 sysmem
  (SPA)**——默认应是 GPU 侧自读(ROM BAR/缓存副本)。key=1 时 devinit 经 DMA 从
  host 提供的 sysmem 物理地址取镜像。
- 若走默认(GPU 自读 ROM):host 改不了内容,route C 死。
- 若走 SPA(key=1):host 可在上传前改表(0x4007c/0x40084 0x38d4841b→0x39268428,
  镜像副本 0xa007c/0xa0084 同改)。**认证风险**:明文表区(镜像内 0x40000)落在
  早前认定的 MAC 区 0x2200–0x43A00 内;且 FWSECLIC 对 devinit 表有独立认证链
  (DEVINIT_TABLE_AUTHENTICATE_*)。是否对 SPA 镜像重验无法静态判定——
  实验判据:boot 报 `NV_UDE_ERR_VBIOS_VERIF_*`/DEVINIT_TABLE_AUTHENTICATE 类
  错误即被拦。
- 旁证:紧邻的 `RMHulkCertSize`(0x928a8 区域)说明 HULK cert 的尺寸也可由
  regkey 给出 → cert 可能同样来自 host 上传;但 cert 有签名,改内容必死。

### 建议的下一个硬件实验(按信息量/成本)

1. `RMExecuteFwsecOnSec2=0`(把 FWSEC 赶到别的引擎)——若覆盖消失,FWSEC 路径
   实锤(值可能变成 A100 行或 boot 失败,两者都有信息量)。零固件风险。
2. `RmDevinitDmaUseSPA=1` + host 侧改表(仅当 1 指向 FWSEC 且 devinit 数据确为
   host 上传)。看 UDE/VERIF 错误有无,一步判定认证拦不拦。
