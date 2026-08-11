# VBIOS_FLASH_RISK — 刷改 CMP-170HX 卡上 VBIOS 的风险评估与预案

> 2026-08-11。纯本地 RE + 公开资料。本地材料:`/tmp/cmp_vbios.rom`(CMP 卡 ROM-BAR
> 视图 dump,0x41800 字节)、`gsp_rm_tu10x.elf` 反汇编、A100 两个 ROM。
>
> **结论先行:表被认证覆盖的概率 = 高。公开记录与本地证据一致指向
> "GA100/Ampere 上改动认证区任何字节 → Falcon halt 不亮机"。不建议刷;
> 若刷,见 §2 预案(先备全片)。**

## 1. 表认证启用概率评估

### 判断:**高(≈8–9 成会被拦)**

### 证据

**(a) 公开记录(同一人/同一资料线,即项目已在用的 GA100 gist 作者):**

[falcon-security-architecture.md](https://github.com/amoghmunikote/170th-Street/blob/master/security-and-firmware-research/the-falcon-security-architecture.md) 明确陈述:

- Ampere 启动链:BROM → Booter(HS@SEC2) → GSP-RM(LS) → **FWSEC(HS@GSP) →
  DEVINIT(LS@PMU)** → SEC2-RTOS。**FWSEC 校验 VBIOS 各段后拷入 WPR2**;
- "**Any byte-level modification to any ucode blob, any DEVINIT script, or any
  thermal/voltage table referenced by WPR2 causes Falcon to halt**";
- Ampere(含 GA100)的签名校验**至今无公开绕过**(OMGVflash/NVflashk 只能在
  Turing 及以下改内容;Ampere 只能整刷别的已签名镜像 = cross-flash);
- 改过的卡在 Ampere 上的表观症状 = `Falcon In HALT or STOP state`。

**(b) 早前 GA100 gist 的字节级结论(本项目已采纳过)**:strap 字节 0x41D53 位于
"MAC-verified region (0x2200–0x43A00)"。CMP dump 里明文表在 0x3ba81,
**落在同一区间**(0x2200 ≤ 0x3ba81 < 0x43A00)——按该划分,表在 MAC 覆盖内。

**(c) 本地字节证据(CMP dump 熵图,0x400 粒度)**:

```
0x0000  MLLLL...           legacy PCI 头(0x6200,byte-sum=0 自洽)
0x5000–0x33400  HHHHH...   高熵区(加密/签名内容:ucode、cert、压缩脚本)
0x33400–0x39c00 M          结构化明文(其它 init 表)
0x3a000–0x3b400 几乎全零
0x3b400–0x40000  L         明文 init 表(0x3ba81 起,无局部签名块)
```

表区是**裸明文、邻近无签名块** ⇒ 若被 MAC 覆盖,MAC 值必在高熵区,
改表 = MAC 输入变化而签名(RSA-3072/SHA-256)无法重算 → halt。
注意:CMP dump 里 0x2200 是全零 —— gist 的 MAC 区间是按 A100 完整 flash 布局讲的,
与 ROM-BAR 视图不逐位对应;**精确的覆盖边界在 CMP 卡上从未被字节级验证**,
这是"高"而非"确定"的唯一原因。

**(d) GSP-RM ELF 侧**:DEVINIT_TABLE_AUTHENTICATE_* / VBIOS_VERIF_* /
CERT_T7_REG_OVERRIDE 等字符串全被一个函数 **0x4e8f234** 引用 —— 那是一个
**错误码跳表 + 日志分发**(a0=错误码 → 索引跳表 → case 里调 logger 打印对应字符串)。
即 GSP-RM 只**透传/报告** FWSEC 返回的错误码;**认证逻辑在 SEC2 加密 ucode 内,
本地不可见、不可 patch**。

### 不确定性声明

"表恰好在所有签名区之外"的可能性无法排除(概率低)。只有两类手段能定论:
刷卡实测(代价=可能变砖),或解出 FWSEC ucode(加密,解不了)。

## 2. 若用户决定刷:完整预案

### 2.1 改什么

**建议:Round-1 最小集 = 只改 0x9a0294 对应的 2 个 dword。** 理由:一次回答
"认证拦不拦 + 该寄存器管不管用"两个问题,爆炸半径最小。

⚠️ **CMP dump 的表偏移与 A100 ROM 不同!** CMP 表行基址 = **0x3ba81**(奇偏移,
步长 0x40,16 dword/行;V59_TABLE_PATCH.md 的 0x40034 基址是 A100 ROM 的,勿直接用):

| 寄存器 | CMP dump 偏移 | old | new |
|---|---|---|---|
| 0x9a0294 (idx2) | **0x3bac9** | 0x38d4841b | 0x39268428 |
| 0x9a0294 (idx4) | **0x3bad1** | 0x38d4841b | 0x39268428 |

(A100 值已在 CMP ROM 的 idx10 = 0x3bae9 处,核对无误。)

注意:CMP dump 是 ROM-BAR 视图,只有一份镜像;**flash 芯片上实际可能有双镜像
/容器布局**,刷卡写入的位置要在全片 dump 上重新定位(搜 `1b 84 d4 38` 字节序)。

**成功率稀释说明(必须知情)**:v55b/v56 已证"把 9 个寄存器的 A100 p10 值写进
活寄存器"对墙零效果;且 v56 后剩余嫌疑收窄到 7 个写锁寄存器(0x9a02c8/d8/e0/e4、
0x9a0164、0x9a03e8、0x9a016c)+ FBFLCN/HBM MRS 内部状态 —— **这些锁定寄存器的
A100 值不在表超集里,改表可能本来就不足以修墙**。即:刷表"生效"(0x9a0294 变成
A100 值)≠ 墙动。

### 2.2 ROM 里谁会拦 / 要同步修什么

| 校验 | 覆盖范围 | 改表后要修? |
|---|---|---|
| PCI legacy byte-sum | 仅 [0, 0x6200)(imglen=49×512) | **否**(表在 0x3ba81,区外) |
| 扩展镜像 (0x6900, NPDS@0x6920) | 声明长度为 0,无 PCI 式整像校验 | 无需 |
| **Falcon 签名/MAC(RSA-3072+SHA-256)** | gist:0x2200–0x43A00(含表区) | **修不了**(无私钥)——这就是拦截点 |
| FWSEC devinit 表认证 | 在加密 ucode 内执行 | 不可修、不可见 |

结论:**没有"同步修校验字段就能过"的路径**;唯一未知是表是否真在覆盖区内。

### 2.3 救砖路径

1. **/tmp/cmp_vbios.rom 不是全片**(0x41800 = 268KB,是 ROM-BAR 视图;GPU SPI
   flash 通常 512KB–2MB 且含双镜像/inforom 区)。**直接拿它回刷不能救砖。**
2. 刷前必须:CH341A + SOIC8 夹子**离线读全片两次、比对一致、存为 golden image**
   (若需重取 dump:服务器上 `~/f0/` 相关脚本或
   `nvflash --save`(需 patched nvflash)可取 ROM-BAR 视图,但救砖请只信夹子全读)。
3. flash 型号:以板上 8 脚 SOIC 丝印为准(拍板照核对;25Q 系列直接夹子读写;
   若是 1.8V 颗粒需带电平转换的夹子)。CMP 170HX 是 PCIe 卡形态,夹子可操作。
4. 若刷后 halt:断电 → 夹子刷回 golden image → 恢复。

### 2.4 实验判据

- 认证拦截签名(dmesg):`Falcon In HALT or STOP state`、
  `NV_UDE_ERR_VBIOS_VERIF_*`、`DEVINIT_TABLE_AUTHENTICATE_*`、
  或干脆无设备(掉卡)。
- 生效判据:正常 boot 且 post-boot 读 `0x9a0294 == 0x39268428`。
- 修墙判据:`wall_reconfirm2 single 60/72` 折叠点移动/消失。

## 3. FWSEC 运行时补丁路线初评(半页)

发射链:编排器 flag 0x27f → 派发 CU 0x4ea7xxx → SEC2 对象 vtable+0x428
(0x4ea7fac)把 FWSEC app 交给 SEC2 执行;app 在 SEC2 上 HS 运行,
自己读卡 ROM、验签、应用 devinit 表。

| 方案 | 机制 | 最大障碍 |
|---|---|---|
| **R1:SEC2 侧 post-auth/pre-exec 补丁** | 用 R3 已有 payload 原语(kgspSec2PostblTimingRefillPayload)在 FWSEC 镜像验签完成后、启动前改其 IMEM/表数据 | 时序窗口在"GSP 启动"附近,而 SEC2 窗口锁(0x840240/0x250)恰在此时落下(v44–v50 实锤);HS 启动后 IMEM 不可见。窗口窄,可能根本不存在 |
| **R2:GSP-RM 代码补丁重定向表指针** | arm-time 补丁(通道可用)改派发参数,把 devinit 表指针指到 host 构造的 A100 值缓冲 | FWSEC 在 HS 内**自己验签表**(gist:FWSEC 验证后拷 WPR2);重定向到未签名副本 = 内部验签失败。除非验签恰在指针传递之前完成——需更多 RE 才能证伪 |
| **R3:devinit 完成后 GSP-RM 侧补重初始化** | 补丁让 GSP-RM 在 devinit 返回后重写 FBPA 值 + 触发重初始化 | v55b/v56 已证单纯写值无效;“重初始化序列"未知,可能不存在于运行时路径 |

对比结论:三条都比"刷卡"工程量大,但 R1 有 R3 先例(SEC2 payload 注入已打通),
是唯一碰得到"验签后执行前"窗口的路线;R2/R3 大概率死在 ucode 内部验签或
派生状态缺失。刷卡的优势是便宜直接,代价是 halt 风险(§1 评估为高)+
必须夹子备片。

## 4. 给用户的决策摘要

- 刷表**被拦概率高**(公开记录 + gist MAC 区覆盖 + 本地无反证)。
- 即便不被拦,**修墙成功率也被 v55b/v56 稀释**(活寄存器写真值已证无效;
  深度可能在锁死寄存器/FBFLCN 内部)。
- 若仍要试:先 CH341A 全片备份;只改 2 个 dword(§2.1 CMP 偏移);备好夹子救砖。
- 我的建议:**先不刷**。把 v59 已建好的 host 改表管线留作"若能证明表不在
  MAC 区"的快速通道;同时推进 R1(SEC2 post-auth 补丁)这条虽有难度但
  不会变砖的路线。
