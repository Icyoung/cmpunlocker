# R1_WINDOW_RECON — "验签后、消费前"补丁窗口侦察(判定性)

> 2026-08-11。纯静态 RE。问题:FWSEC 验签完、devinit 表被消费之前,是否存在
> GSP-RM 可达的补丁窗口。
>
> **判定:窗口条件成立(conditional)。存在具体可实现的补丁方案,但挂三个
> 加密 ucode 内的硬未知,只有硬件探针能裁决。探针 1–2 次冷启动即可三分判别,
> 且每个失败模式各自对应一个确定结论。**

## Q1:devinit 表的物理路径

流向(与 v59 canary + 公开文档一致):

```
卡 ROM ──(FWSEC,HS,直读卡 ROM;v59 实锤 host 缓冲不被消费)──> 验签
      ──> 拷入 WPR2(FB)──> DEVINIT app(LS@PMU)消费
```

可见代码侧的核查结果:

- GSP-RM 的可见 VBIOS 缓冲路径 = 命名资源 `vbios000`(0x4e2f700 取资源、
  GSP heap 分配、vtable 填充;0x4e1f640 解析 PCIR/NPDS/SIG 记录)——
  **但这是 host 供应的副本,v59 已证不被 devinit 消费**。
- **被消费的表副本(WPR2 内)的地址,在可见 GSP-RM 代码中零引用**:
  全 disasm 无任何指向它的指针/descriptor。地址由加密 FWSEC app 计算。
  ⇒ 静态定位不可能;但**内容扫描不需要地址**(见 §判定)。
- WPR2 边界本身对 GSP-RM 可见:寄存器 0x1fa824/0x1fa828(WPR2 bounds,
  v35/v43/v45 硬件侧已读过),GSP-RM 经寄存器窗口(ld [0x438af28]+偏移,
  EXFIL_RE.md 已证)可读。

## Q2:DEVINIT 发射序列

- 编排器 0x4e2ddec 写的路径 flags:0x270(RMExecuteDevinitOnPmu)、0x27f
  (FwsecOnSec2)等;flag 消费方 = secureboot 派发 CU(0x4ea7xxx–0x4ea9xxx),
  经引擎对象 vtable+0x428 发射 app(0x4ea7fac 一带)。**派发函数全部
  NVOC 间接调用,无直接 caller**,精确到指令的"发射点"静态不可定。
- Boot 主序列(公开头导出函数 **0x5b2fb60** = RM app 主初始化):
  `0x5b2fdfc: call 0x5b2ded4`(取 vbios/解析/编排 = 准备段)→
  `0x5b2fe0c: call 0x5b2b724`(下一 stage,大量子调用)→ 0x5b1dac0、
  0x5b294a4 ……FWSEC/DEVINIT 发射嵌在 vtable 调用里,但它们**前后都有
  可见代码在跑** —— 这就是窗口的时间载体。

## Q3:read-back verify 威胁评估

`CERT_HULK_REG_(RMW_RBV_)READ_BACK_VERIFY_MISMATCH` 两个字符串的唯一引用方
= 错误码跳表 0x4e8f234(GSP-RM 仅把 FWSEC 返回的错误码翻译成日志)。
**检查本体在加密 ucode 内。** 证据强度评估:

- RBV 机制存在(否则不会有错误码)——证据强。
- 但 RBV 语义是 **cert(HULK)携带的 register-override 的写后回读比对**;
  我们改的是**脚本值表**,不是 cert override。表值被脚本引擎写出后是否有
  RBV/重验,静态不可知 —— 证据弱,只能靠探针。
- 同族还有 `DEVINIT_TABLE_AUTHENTICATE_*`:若"devinit 表"认证恰好覆盖
  明文值表,内容补丁在 DEVINIT 侧直接被验杀 —— 同样只能探针判。

## Q4:SEC2 通道关闭时点

硬件已证:SEC2 host 窗口在最终 BooterLoad(mbox 0x29)后永久关闭
(v44–v50);post-boot 的 SEC2 活动(v46 DMATRFCMD CTXDMA=6)是 GSP 自己的
secure-context DMA,host 不可触发。**⇒ FWSEC/DEVINIT 阶段没有任何
host 可触发的 SEC2 命令通道;SEC2 侧二次 payload 在当前原语下不可能。
唯一注入层 = arm-time 的 GSP-RM 代码补丁。**

## 判定:窗口条件成立

候选方案(布局无关,绕过"地址不可知"):

**arm-time 给 GSP-RM 打一个"WPR2 内容扫描改写"补丁**:在 FWSEC staging 之后、
DEVINIT consume 之前的可见代码点,用寄存器窗口读 WPR2 bounds(0x1fa824/28),
扫描 WPR2 内容找 `1b 84 d4 38`(0x38d4841b LE),就地改写为
`28 84 26 39`(0x39268428)。不需要知道表的地址/布局。

三个硬未知(全部在加密 ucode / 硬件行为内,静态不可判):

| 未知 | 若失败的表观 |
|---|---|
| U1:WPR2 对 LS(GSP-RM)可写/可被数据窗口覆盖 | 扫描写入 fault → GSP trap/hang |
| U2:表确有 FB/WPR2 副本(DEVINIT 不是直读 ROM) | 扫描零命中,boot 正常,0x9a0294 不变 |
| U3:DEVINIT 不重验表 / RBV 不杀 | boot 报 `DEVINIT_TABLE_AUTHENTICATE_*` / `*_READ_BACK_VERIFY_MISMATCH` / halt |

**加分项(为何"条件成立"而非"不存在")**:(a) FWSEC→WPR2→DEVINIT 的 FB 中转
有公开文档支持;(b) GSP-RM 对自己 WPR/heap 的高 FB 区域有可用的数据窗口
(phantom 带取证已证 GSP-RM 能直读 37-40G FB);(c) 触发点前后可见代码充足。

## 最小探针设计(交硬件侧)

补丁载体:已证管道(arm-time WPR 补丁;stub 放已证死的函数 0x5026c34)。
触发点候选(按 boot 序,幂等,先到先得,带 done-flag):

1. **0x5b2fe00**(0x5b2fb60 内,call 0x5b2ded4 返回处)——准备段之后;
2. **0x5b2b724 入口**(下一 stage 开头);
3. 0x4e2e0f0(编排器内 vtable 调用返回处,疑 FWSEC exec 之后)。

stub 逻辑:读 WPR2 bounds → 4 字节步进扫 `1b 84 d4 38` → 替换为
`28 84 26 39` → 命中计数写到 mailbox reg(bus 0x110440,EXFIL_RE.md 通道)
或 on-grid rodata 槽。扫描跨度约几十 MB,注意 boot 时延(可只扫 4K 对齐页头
或限一次)。

**判别矩阵(host 侧全可读,无需新通道)**:

| 结果 | 结论 |
|---|---|
| boot 在扫描点 trap/hang | U1 否:WPR2 不可达 → **窗口不存在,R1 关闭** |
| boot 正常,计数=0,0x9a0294=0x38d4841b | U2 否:无 FB 副本(或范围错;先扩范围重试一次) → 仍零命中则 **R1 关闭** |
| boot 报 DEVINIT_TABLE_AUTHENTICATE / RBV 类错误 | U3 否:重验/RBV 拦 → **R1 关闭** |
| 0x9a0294=**0x39268428** 且 boot 健康 | **窗口成立** → 跑 wall_reconfirm2 判墙(注意 v55b/v56 稀释:该寄存器单独可能不动墙) |
| 0x9a0294=A100 值但随后 RBV halt | RBV 杀 → R1 关闭 |

## 备注

- 即使窗口成立、值落地,修墙仍受 v55b/v56 阴性稀释(9 值组合写活寄存器
  不动墙)——R1 探针的真实价值是**判定"晚期覆盖被拦截后系统行为"**,
  墙动与否则看 wall 探针。
- 若 R1 关闭:剩余路线 = 接受 40G 生产配置,或 strap/MAC 级研究(天级,
   gist 的 DFA-on-secret(2) 方向)。
EOF
