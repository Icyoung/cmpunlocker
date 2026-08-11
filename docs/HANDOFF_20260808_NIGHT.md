# HANDOFF — 2026-08-08 深夜（80G 根修 · GSP 固件修补通道验证阶段）

> 本文档是当前工作的完整交接：bug 定性、固件加载机制考古、当前卡点、机器状态、下一步操作序列。
> 早期完整研究过程见 `docs/RESEARCH_REPORT_20260808.md`；问题陈述与已否路径见 `docs/PROBLEM_32G_WALL.md`。
> **进度看 §8（Done / Blocked / Next）**——§5 的旧 TODO 已作废，勿按旧清单重做。

---

## 0. 一句话现状（2026-08-08 夜末更新）

32G 墙 bug **已完全定性**（GSP 对跨洞 ≥2 段对象的 seg2 多写一份 PTE 到 VA−32G 槽）。
**探索阶段基本收尾**；**修复未落地**——落盘改 `gsp_tu10x.bin` 内 RM 代码被 Booter 验签拦（烟雾 `0xb`），WPR PRAMIN 改写亦否定。
当前唯一阻塞：**验签通过后的注入通道**（或 Booter 验签绕过）+ tu10x 地标重定位出补丁字节。

---

## 0.5 ⚠️ 重大勘误（2026-08-08 深夜，GPT 提出、已在源码实锤）

**我们一直改错了固件文件。** 170HX 是 GA100，而 610.43.02 的固件名映射（`src/nvidia/arch/nvalloc/common/inc/nv-firmware.h:111-122`）里 **GA100 fall through 到 TU10X 分支 → 加载 `gsp_tu10x.bin`**；`gsp_ga10x.bin` 只服务 GA10X（GA102 等）。依据：

- `nv-firmware-chip-family-select.h:47`：`GPU_IMPLEMENTATION_GA100 → NV_FIRMWARE_CHIP_FAMILY_GA100`
- `nv-firmware.h:100-104`：GA10X → `gsp_ga10x`；`nv-firmware.h:111-118`：**GA100/TU11X/TU10X → `gsp_tu10x`**
- 旁证：linux-firmware 公开事实——gsp_tu10x.bin 覆盖 Turing 全系 + GA100

**由此作废的结论：**
- ❌ "补丁 A 阴性有效"——补丁打在从不被加载的 `gsp_ga10x.bin` 上，坏单元数不变**什么都不能说明**。"肇事点在段记录创建处"降级为未验证假设。
- ❌ "文件挪走 GPU 照样起来 = GSP 热存活的证据"——挪的是错误文件，这些测试是无效操作，不构成任何证据。
- ❌ `gsp_analysis/gsp_rm.elf`（从 gsp_ga10x.bin 提取）上所有 RE 地标的**偏移量**不可直接复用——但函数符号名可复用（同一套源码，不同 build，符号表还在），按符号名在 tu10x ELF 里重定位即可。

**仍然成立的结论：**
- ✅ 文件是镜像源（nvidia-smi 报 GSP 610.43.02 → 只能来自 610.43.02 目录下的 gsp_tu10x.bin，2021 年的卡闪存没有这个版本）
- ✅ bug 定性本身（§1 全部是 CUDA 层实测，与固件文件名无关）
- ✅ 冷热启动判据（vram_mark/vram_resid）
- ✅ 冷启动：FLR（PCI reset）已等价真冷启动（§0.6）；扩展坞断电仍可作为最后手段

**GPT 建议的两步验证（采纳，作为新基线）：**
1. 改 `gsp_tu10x.bin` 的 `.fwversion`（等长修改，如 610.43.02→610.43.03）→ 真冷启动 → 驱动应报版本不匹配/GPU 死 → 实锤"这个文件被读取"。
2. 之后第一次代码补丁若遇 Booter 验签失败（`.fwsignature_*`），则说明不能简单落盘改代码，需绕过验签或改内存镜像——这是新风险项，此前"验签不存在"的考古是在错误文件上做的，需对 tu10x 路径重做。

---

## 0.6 ✅ 金标准测试结果（2026-08-08，全部实锤）

**`.fwversion` litmus：完美命中。** 等长改 `gsp_tu10x.bin` 的 `.fwversion`（容器偏移 0x1bfae2c，"610.43.02"→"610.43.03"）后真冷启动：

```
NVRM: GPU0 _kgspFwContainerVerifyVersion: GSP firmware image version mismatch:
got version 610.43.03, expected version 610.43.02   ← kernel_gsp.c:6706
```

- ✅ `gsp_tu10x.bin` 就是被加载的文件，再无悬念
- ✅ 版本检查在上传镜像之前执行
- ✅ 版本检查失败后卡上无 GSP 存活 → 恢复文件 + `modprobe -r/modprobe` 即可复活 GPU（不用断电）

**🔑 FLR 软件冷启动（重大流程突破）：** `echo 1 > /sys/bus/pci/devices/0000:3d:00.0/reset`（先卸载 nvidia 模块，reset 后再加载）等价于对 GSP 的真冷启动——实测 marker (0x5A) 丢失、完整 GSP 启动剧场重跑。**后续所有固件补丁测试不再需要拔扩展坞电**，测试循环从"求用户断电"变成纯软件操作：
```bash
sudo bash -c "modprobe -r nvidia_drm nvidia_modeset nvidia_uvm nvidia && \
  echo 1 > /sys/bus/pci/devices/0000:3d:00.0/reset && sleep 2 && \
  modprobe nvidia && modprobe nvidia-modeset"
```
（注意 PCI 地址会变，先 `lspci | grep -i nvidia` 确认。）

**tu10x 容器结构（与 ga10x 不同！）：**
- 外层 ELF：`.fwimage` @ 0x40（size 0x1bfadc8）**本身就是** RISC-V gsp_rm ELF（ga10x 是在 0x19f040 处另嵌一个带符号的 ELF）
- `.fwversion` @ 0x1bfae2c（0xa 字节）；三个签名节：`.fwsignature_ga100` / `_tu11x` / `_tu10x`（各 0x1000，驱动按芯片选 ga100）
- 内嵌 ELF **stripped 无符号表**；RM text = `.section_task_rm_elf_text_instance`（ELF 内偏移 0xbf7000，0xff1000 字节），rodata = `.section_task_rm_elf_rodata_instance`（0x1d000，0x1da000）
- 本地文件：`gsp_analysis/gsp_tu10x.bin`（md5 f6a6491e2f5fa671b3f4171f63844737）、`gsp_analysis/gsp_rm_tu10x.elf`
- 容器偏移换算：**容器偏移 = ELF 内偏移 + 0x40**
- RE 重定位方法：从 ga10x 带符号 ELF 提取地标函数指令字节（mask 重定位立即数）→ 在 tu10x RM text 段模式匹配；字符串 xref 交叉验证

## 0.7 ✅ 签名烟雾 + WPR 探针（2026-08-08 晚，全部实锤）

**`.fwimage` 改 1 字节 + FLR**（容器偏移 `0x189852`，`0x58`→`0x59`）：

- `normal BooterLoad` → **`0xb`** / `status=0xffff`（非剧场故意的 `0x31`）
- 随后主机卡在 GSP init 路径；恢复 stock `gsp_tu10x.bin` + FLR 后 `BooterLoad status=0x0`，GPU 正常

**结论**：host 侧 `_kgspPrepareGspRmBinaryImage` 只做版本字符串检查；**RM 代码完整性由 SEC2 Booter 验签**。落盘改 `.fwimage` 内代码不可行。

**WPR PRAMIN RMW 探针**（`driver/apply_wpr_rmw_probe.py`，实验已结论）：

- BAR0 PRAMIN 可读 ctrl 区，WPR 内 GSP-RM 镜像读回非 ELF magic，写不粘；只读访问也破坏后续 GSP init
- **结论**：验签后不能经 PRAMIN 改 WPR 驻留镜像

**Booter 资产**：`gsp_analysis/booter_load_ga100_prod.bin`（60KB，已抽出，验签分支 RE 未完工）

---

## 1. bug 定性（全部实测证据，勿重复验证）

**现象**：单进程 CUDA 映射 >~35G 时，VA x 与 VA x+32G 翻译混叠——读 x 得到 x+32G 的内容，精确偏移 +32G（physical address bit 35 丢失）。

**触发条件**：内存对象的物理页数组被幻影洞 [36G,44G) 切成 ≥2 段。洞上部分（seg2）的 PTE 被额外写到"对象内页索引清 bit 14（−32G）"的槽位。
- 单段对象**免疫**——哪怕整体位于 60G+（E1 实验证明）。所以"32G 墙"不是地址截断，是分段触发的 PTE 错位。
- 40G profile 无洞 → 永远单段 → 无墙（这解释了为什么原版 10G→40G 解锁完全正常）。
- 三种硬件 alias 假设均被排除：T≤34G 干净；洞几何下 VA 混叠精确 +32G 而非洞大小对应的 +24G。

**肇事者定位**：
- 客户端页表**完全由 GSP 固件构建**（host 日志零参与；MAP RPC 只带句柄，物理页数组在 GSP 侧）。
- host 开源代码已逐条精读排除（walk 回调干净）。
- 幻影洞 [36G,44G) 必须保留（保护 GSP 页表池所在的 37-40G 幻影带）；挪洞不能解决混叠——触发与洞位置无关，只与分段有关。

**修复判据**（最终验收）：
1. 真冷启动后 `~/f0/vec_scan2 48` 全绿（total_bad_units=0）。
2. llama-server `-c 262144` 输出连贯（256K 上下文越过 40G 界限时不再胡言）。

---

## 2. 固件加载机制（现行事实 + 历史考古）

### 2.1 现行事实（GA100 / CMP 170HX，以 tu10x 为准）

**实载文件**：`gsp_tu10x.bin`（见 §0.5、§0.6）。容器结构见 §0.6（`.fwimage` @ 0x40 即 stripped gsp_rm ELF；偏移 **ELF 内 + 0x40**）。

**启动链（简化）**：
```
request_firmware(".../gsp_tu10x.bin")
  → _kgspPrepareGspRmBinaryImage     ← 仅 .fwversion 字符串比对
  → SEC2 PLM 剧场（故意 0x31 失败 → 戳寄存器解锁算力/显存）
  → normal BooterLoad                ← 验签 RM 镜像；改码 → 0xb
  → GSP-RM 驻 WPR / kflcnResetIntoRiscv
```

**GSP 重载**：`rmmod` 不重载镜像；**FLR 或真断电**才从文件重载（§0.6）。

**冷热启动判据**：
- ❌ 剧场日志（39× "Booter failed"）不是冷启动标志
- ✅ FLR 后 marker 丢失 / `vram_mark`→`vram_resid` 无残留

### 2.2 历史考古（ga10x 路径，仅供 RE 地标参考，勿当加载事实）

早期误用 `gsp_ga10x.bin`：内嵌带符号 ELF @ 0x19f040；`容器偏移 = gsp_rm file off + 0x19f040`。
当时以为 host 路径无验签——**已被 §0.7 推翻**（验签在 Booter，不在 `_kgspPrepareGspRmBinaryImage`）。

### 2.3 已证/未证清单（更新至 2026-08-08 夜末）

| 命题 | 状态 |
|---|---|
| GA100 加载 `gsp_tu10x.bin` | ✅ `.fwversion` litmus |
| host 路径无 RM 验签 | ✅ 仅版本串；**Booter 有验签** |
| 落盘改 `.fwimage` 内 RM 代码 | ❌ 烟雾 `0xb`，不可行 |
| WPR PRAMIN 改写 GSP-RM | ❌ 探针否定 |
| nvidia.ko 内嵌 RM text | ❌ 已排除 |
| ga10x 补丁 A 阴性 | ⚠️ 在错误文件上测的，结论作废；tu10x 上未重测 |
| host region / tail-steer 消墙 | ❌ 不移动 GSP 池，不消双写 |
| 双进程左右分立 workaround | ❌ 洞上进程连坐洞下（见 `PROBLEM_32G_WALL.md` §4） |
| tu10x 地标重定位表 | ❌ 未产出（`RE_FINDINGS.md` 仍全是 ga10x 偏移） |
| 根修验收（vec_scan2 48 / llama 256K） | ❌ 未通过 |

---

## 3. 服务器环境速查

- 连接：`ssh icy@p3-server`（凭据在本地密码管理器，勿入库；sudo 同密码；偶发 Permission denied，`sleep 20` 重试）
- 卡在雷电扩展坞，PCI 热插拔；PCI 地址会变（见过 09:00.0 和 3d:00.0）
- 驱动构建（固件补丁测试用 **FLR**；大改驱动后若 GSP 状态异常再真断电）：
  ```bash
  cd ~/cmpunlocker/driver && CMPUNLOCKER_DRIVER_VERSION=610.43.02 CMPUNLOCKER_CARD_PROFILE=10gb80 nohup ./build.sh > ~/build_x.log 2>&1 &
  ```
- 测试工具（服务器 `~/f0/`）：
  - `vec_scan2` — 48G 洪水测试，分桶报坏点
  - `alias_read`/`alias_read2` — 模式反解码混叠探测
  - `vram_mark`/`vram_resid` — 冷热启动判据
  - `e1_rogue`、`va_base`
- nvcc 两套：
  - 运行时 API：`~/vllm-env/.venv/lib/python3.12/site-packages/nvidia/cu13/bin/nvcc`，链接 `-L$CU13/lib -lcudart -Xlinker -rpath -Xlinker $CU13/lib`
  - driver API：`~/cuda-12.8/bin/nvcc`，`-lcuda`
- llama-server 恢复命令（修复验证用）：
  ```bash
  cd ~/llama.cpp && LD_LIBRARY_PATH=...cu13/lib nohup build/bin/llama-server -m ~/Models/Qwen3.6-27B-official-Q8_0.gguf -ngl 99 --host 0.0.0.0 --port 8001 -c 131072 > ~/llama_server.log 2>&1 &
  ```

---

## 4. 当前机器状态（2026-08-08 夜末）

- **GPU**：CMP 170HX，81920 MiB；`gsp_tu10x.bin` **stock**（md5 `f6a6491e2f5fa671b3f4171f63844737`）
- **驱动**：干净基线；`RMCmpTailSteer=0`（tail-steer 已实现但关闭）
- **固件烟雾**：已恢复 stock；勿留 smoketest 字节在服务器固件目录
- **本地**：`gsp_analysis/gsp_tu10x.bin`、`gsp_rm_tu10x.elf`、`booter_load_ga100_prod.bin`、`RE_FINDINGS.md`（ga10x 偏移）；`fw_patch.py` **仍指向 ga10x**（小活，未改）

---

## 5. 补丁测试纪律（仍有效）

1. 固件实验：**FLR 冷重载**（§0.6）；`vram_mark`→`vram_resid` 或 dmesg `BooterLoad status` 作对照。
2. 先 `nvidia-smi`，再 `~/f0/vec_scan2 48`（`total_bad_units`）。
3. 落盘改 `.fwimage` 内代码 **已知会 0xb**——除非先解决 Booter 验签，否则别重复烟雾。
4. 阴性结果也是结果；ga10x 上的结论不能外推到 tu10x，除非在 tu10x 上重测且注入通道已通。

> **旧 §5.0–5.3 操作序列已作废**（`.fwversion` litmus、签名烟雾、ELF 拉回均已完成）。见 §8。

---

## 6. 支线与备忘

- **PCIe Gen3/4 解锁**（用户问过，已答复，搁置）：Gen3 成功率低但可试，Gen4 无意义（TB3 拓扑封顶 Gen3 x4）。实验阶梯：读 XVE/LINK_CONFIG → retrain.sh 试 Gen3 → SEC2 剧场加 MAX_RATE=3。主线做完若用户还想要再拾。
- **原版 cmpunlocker（amoghmunikote）没改 GSP**——纯 host 补丁 + 寄存器注入。我们本地 = 上游 + 80G 扩展。
- **提醒另一个 Kimi（部署 vLLM 的）**：根修落地前，**单进程映射 ≤30G 才安全**。
- 用户可能向 GPT Pro 咨询过固件加载路径问题，回来可能带新信息——注意整合。
- `NVreg_EnableGpuFirmware=0` 实验无效（默认值 0x12=MODE_DEFAULT|ALLOW_FALLBACK），conf 已还原，但 initramfs 里可能残留带参数的旧 conf，留意。

## 7. 收尾清单（根修通过后）

1. 全量验证：`vec_scan2 48` 全绿 + llama 256K + 双进程左右 + 算力回归。
2. `fw_patch.py` 改 tu10x 路径/偏移；`RE_FINDINGS.md` 补 tu10x 容器偏移表。
3. 提交推送前**先问用户确认**（GitHub `Icyoung/cmpunlocker`）。

---

## 8. 进度总表（Done / Blocked / Next）

> **勿按旧 §5 TODO 重做已完成项。** 探索 ≈ 做完；修复卡在注入通道。

### Done（不必重做）

| 项 | 备注 |
|---|---|
| Bug 定性（E1、alias、PTE/PMA 日志） | `RESEARCH_REPORT_20260808.md`、`PROBLEM_32G_WALL.md` |
| ga10x → tu10x 文件勘误 | §0.5 |
| `.fwversion` litmus | §0.6 |
| FLR 软件冷启动 | §0.6 |
| `.fwimage` 签名烟雾 → Booter `0xb` | §0.7 |
| WPR PRAMIN 探针否定 | `apply_wpr_rmw_probe.py` |
| ga10x RE 地标 + 补丁 A（错误文件） | `re/RE_FINDINGS.md`；阴性结论作废 |
| 拉回 `gsp_tu10x.bin` / `gsp_rm_tu10x.elf` | 本地 `gsp_analysis/` |
| host 绕行否定 | phantom pin 保留；tail-steer / 双进程左右 → 否 |
| 抽出 `booter_load_ga100_prod.bin` | 验签 RE 未完工 |

### Blocked（已知死路，除非前提变化）

| 项 | 阻塞原因 |
|---|---|
| 落盘改 `gsp_tu10x.bin` 内 RM 代码 | Booter 验签 `0xb` |
| 验签后 PRAMIN 改 WPR 镜像 | 写不粘 / init 挂 |
| host region / tail-steer 消 32G 墙 | 不移动 GSP 池、不修双写 |
| 双进程左右分立 | 洞上连坐洞下 |
| P-E printk（RPC 次数） | 可选；E1 已够定性，非阻塞 |

### Next（真正剩余工作，按优先级）

| # | 项 | 产出 |
|---|---|---|
| 1 | **Booter 验签绕过**或验签后可写窗口 | 能合法把补丁字节送进运行中 GSP-RM；RAM 钩子已通（`RMCmpGspFwPatchA=1` → `CMP_GSP_PATCH`），但 Booter `0xffff` |
| 2 | **tu10x 地标重定位**（模式匹配 + 字符串 xref） | tu10x 容器偏移表；RPC→dmaAllocMap 段处理层 |
| 3 | **最小字节补丁** + FLR 验证 | NOP/fix seg2 二次 PTE 写 |
| 4 | **验收** | `vec_scan2 48` 全绿；llama `-c 262144`；双进程互不干扰 |

可选小活：`fw_patch.py` 改 `gsp_tu10x.bin` + 容器偏移 `+0x40`（仅 `.fwversion` 工具）。
