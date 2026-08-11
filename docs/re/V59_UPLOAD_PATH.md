# V59 上传路径:host 侧 VBIOS 镜像从哪里来到 GSP

> 2026-08-11。本地无 610.43.02 源码树(repo 的 driver/ 只有补丁脚本);
> 以下 = 本地固件证据 + 公开源码认知。**标注 [服务器验证] 的点需要 agent-3 在
> 源码树上 grep 确认。**

## 1. 固件侧证据(GSP-RM 怎么拿 VBIOS)

- GSP-RM 里有命名资源拉取函数 **0x4e2f700**,引用的资源名是 **`vbios` /
  `vbios000`**(file 0x92920/0x92928;该函数把 0x4092928 当描述符读,
  +0x00=qword 名、+0x08=属性字节,然后构建 0x270 字节的参数块)。
  ⇒ GSP-RM 把 VBIOS 当作**命名资源**获取,不是自己去 ROM BAR 读。
- GSP 的命名资源由 **host 侧的 bin-archive 服务**供应 —— 本 repo 已有直接证据:
  `gsp_analysis/g_bindata_kgspGetBinArchiveBooterLoadUcode_TU102.c`
  (从服务器构建树抽出的 `kgspGetBinArchiveBooterLoadUcode_TU102`)。
  同一服务族应存在 `kgspGetBinArchive*Vbios*` 或等价物。
- regkey `RmDevinitDmaUseSPA`(→ obj+0x16e8,编排器 0x4e2ddec 读)表明 devinit
  取表可以走 **sysmem 物理地址 DMA**;该 key 的存在暗示默认不是 SPA
  (默认大概是 host 提供缓冲由 FWSEC 读入 FB,或 GSP 直读 ROM 缓存)。

## 2. Host 侧最可能的路径(公开树知识,需核实)

610.43.02(open-gpu-kernel-modules)GSP 启动序列,本 repo 补丁已证实的符号:
`kgspBootstrap_TU102`、`kgspExecuteBooterLoad_HAL`、`kgspExecuteHsFalcon_HAL`、
**`kgspExecuteFwsec_HAL`**、**`kgspGetFrtsSize_HAL`**。

据此推断的标准流程:

1. host 读卡 ROM:`src/nvidia/src/kernel/gpu/rom/kern_rom.c`(objROM,
   经 PRAMIN/ROM BAR)进 sysmem 缓冲。[服务器验证:函数名,可能在
   `kern_rom.c` 的 `romLoadImage`/`romSanityCheck` 一带]
2. GSP 启动时 host 解析该缓冲:`kgspGetFrtsSize_HAL`(从镜像里算 FRTS 区大小)
   —— **证明 host 侧缓冲确实存在且被解析**。[服务器验证:调用点]
3. `kgspExecuteFwsec_HAL` 构建 FWSEC 命令,内含 VBIOS/FRTS 缓冲的 DMA 地址
   (memdesc),FWSEC app 在 SEC2 上把镜像 DMA 进去执行 devinit。
   [服务器验证:`grep -rn "ExecuteFwsec\|FrtsSize" src/`]

nouveau 侧对照(同流程的公开实现):`nvkm/subdev/gsp/rm/r535/fwsec.c`
(GSP-FWSEC 启动,sysmem 里放 VBIOS 镜像,把 GPU 物理地址写进 FWSEC 命令)。

## 3. 建议的补丁注入点

**首选**:host 侧填充/供应 VBIOS 镜像缓冲的函数里、提供给 GSP 之前
(ROM 读出后、bin-archive/FWSEC 供应前),按 `V59_TABLE_PATCH.md` 的表
**先校验后改**(断言 `buf[0x4007c..]==0x38d4841b` 再写;不符则拒绝并打印,
防止表布局漂移写坏镜像)。

候选注入位置(按命中概率排序,均需服务器核实):

1. `kgspGetBinArchive*Vbios*`(若存在)——按名供应 "vbios000" 的主机回调;
   改它返回的缓冲。
2. `kgspExecuteFwsec_HAL` 的调用方/参数构建处(镜像 memdesc 已就位,
   启动前最后一站)。
3. `kern_rom.c` 的 ROM 读取完成点(最早,覆盖所有下游消费者;
   副作用:host 自己看到的 ROM 也被改 —— 对 v59 反而无害)。

**服务器验证 grep 清单**:
```
grep -rn "GetBinArchive" src/                 # 找 VBIOS 资源供应函数
grep -rn "ExecuteFwsec\|FrtsSize" src/        # FWSEC 启动与 FRTS 解析
grep -rn "vbios000\|\"vbios\"" src/           # 资源名注册点
grep -rn "DevinitDmaUseSPA" src/              # SPA flag 的 host 侧消费(若有)
```

## 4. 关键一问的答案(SPA=1 时 host 缓冲在哪)

若默认路径并非 host 上传,`RmDevinitDmaUseSPA=1` 时的 sysmem 缓冲**仍然由
host 填充** —— 因为 `kgspGetFrtsSize_HAL` 在 host 侧解析过该镜像
(FRTS 尺寸只能从 host 内存里的镜像算出),所以镜像缓冲必然存在;
SPA flag 只是切换"FWSEC 从哪个地址 DMA"。缓冲的分配/填充点在 §3 的
grep 结果里确认。

## 5. 风险提示(给 v59 执行者)

- 表区在 MAC 校验区内(见 V59_TABLE_PATCH.md):若 FWSEC 对上传镜像重验,
  预期报 `NV_UDE_ERR_VBIOS_VERIF_*` / `DEVINIT_TABLE_AUTHENTICATE_*` → 路线死。
- 若认证只盖"devinit 表"子结构而明文表不在其内,则可能通过 —— 只有实验能判。
- 第一阶梯只改 0x9a0294 的 4 个 dword,最小变量。
