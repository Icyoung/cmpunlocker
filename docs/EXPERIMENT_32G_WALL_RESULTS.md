# 32G 墙再确认实验结果

日期：2026-08-11  
目标机：`p3-server`，CMP 170HX 10G SKU，NVIDIA 610.43.02，CUDA 报告
`81920 MiB`。所有用例之间均执行文档 §5 的 FLR + 驱动重载；未使用任何
`RMCmp*` regkey。主机 staging buffer 为 pinned host memory，单次
`cudaMemcpy` 最大 256 MiB。

## 总结

结论：**32G 墙存在；高区写入可以因果性地污染低区。结合后续
`wall_reconfirm2` 的 60G/72G 实验和 delta 解码，污染不是简单的
`addr-32G`、`addr+32G` 或 `addr mod 4G`，而是主 delta 为 `+35 GiB`、带
256B 粒度尾差的地址路由混叠；72G 对象污染到约 35.4G，支持
mod-40G 折叠类机制，排除位掩码预测的 `[0,24G)`。**

P4 原始设计还存在一个独立混淆：当前 CUDA allocator 按倒序 VA 分配，且 filler 在 H 写之前就会污染 C，因此不能把 P4 的 C 坏点直接归因给 H 写。

## P0 — vec_scan2 基线

命令：`cd ~/f0 && sudo ./vec_scan2 48`，连续 3 次。

三次均为：

```text
free=74.0G total=79.3G
test=48G total_bad_units=845545477
DONE
```

第一轮原始 1G 桶（`bad16B`；第二、三轮桶内小数点级别变化，但总数完全相同）：

```text
0:40131584  1:66965504  2:66960840  3:66960336
4:66960152  5:66960512  6:66960160  7:66960704
8:66957664  9:66956464 10:66958280 11:66959184
12:66956616 13:5
35:107520 36:143360 37:148024 38:148528
39:148712 40:148352 41:148704 42:148160
43:151200 44:152400 45:150584 46:149680 47:152248
```

P0 复现成立，且 3 次后 `nvidia-smi` 仍为 `81920 MiB`，没有 Xid/GSP 死亡。

## P1 — 30G 墙下对照

对象：`0x7b0240000000`，30G。

```text
WRITE P1_30G done in 31.4 s
VERIFY P1_30G done in 29.1 s bad_qwords=0
  hypotheses: H1-=0 H1+=0 H2=0 H3-other=0
=== SINGLE P1_30G RESULT=PASS ===
```

P1 全绿，说明基础的 pinned staging、HtoD、DtoH 和 host 比对路径正常。

## P2 — 48G 对象内交叉污染

对象：`0x7c5200000000`，48G。工具额外保留了一个“高区写入前”的低区基线回读，
用于证明因果。

```text
VERIFY cross48-low-before-H done in 31.0 s bad_qwords=0
WRITE cross48-high-H done in 16.8 s
VERIFY cross48-low-after-H done in 41.5 s bad_qwords=1691090944
VERIFY cross48-high done in 15.6 s bad_qwords=0
```

高区写后低区 1G 桶：

```text
0:80478208
1..12: 每桶 134217728
13 及以上: 0
```

即污染集中在 `[0, 13G)`，总计 `1,691,090,944` 个 qword；高区自身全绿。

最终假设统计（低区 after-H）：

```text
H1-=pat(addr-32G)       0
H1-=pat(addr-32G)|MARK  0
H1+=pat(addr+32G)       0
H1+=pat(addr+32G)|MARK  0
H2=pat(addr%4G)         0
H2=pat(addr%4G)|MARK    0
H3-other                1691090944
```

前 5 个失配样本（工具实际打印前 20 个）：

```text
addr=0x19a00000 expected=0x315dd4ab99140000 actual=0xbe7117dde348bc20
addr=0x19a00008 expected=0x2319a2779747e0a9 actual=0xb02ce5a9e9e49c89
addr=0x19a00010 expected=0x14d570438df3c152 actual=0xa1e8b375f6107f72
addr=0x19a00018 expected=0x06913e0f8a2fa1fb actual=0x93a48141fc4c5fdb
addr=0x19a00020 expected=0xf84d0bdb805b82a4 actual=0x85604f0dfaf83e84
```

这一步是本实验最强的因果证据：低区先全绿，只有在高区 H 写入后才坏；但
失配值不是本工具写入的高区 hash 的简单 ±32G/4G 映射。

## P3 — 单对象 60G / 72G

### 60G

对象：`0x747800000000`。

```text
WRITE P3_60G done in 62.7 s
VERIFY P3_60G done in 78.7 s bad_qwords=3301703680
=== SINGLE P3_60G RESULT=FAIL_DATA ===
```

坏桶为 `0:80478208`、`1..24:每桶 134217728`，即主要覆盖 `[0,25G)`；
所有 H1/H2 候选统计为 0，H3-other 为全部坏 qword。

### 72G

对象：`0x782840000000`。

```text
WRITE P3_72G done in 74.2 s
VERIFY P3_72G done in 99.4 s bad_qwords=4697620480
=== SINGLE P3_72G RESULT=FAIL_DATA ===
```

坏桶为 `0:80478208`、`1..34:每桶 134217728`、`35:53739520`，即覆盖到
约 `[0,36G)`；所有 H1/H2 候选统计为 0。

两次均能分配并完整写入，未出现 CUDA 错误、Xid 或 GSP 死亡；失败发生在
全量 host 回读校验。这里旧工具的 H1/H2 candidate 只检查了简单的
±32G/4G 公式，计数为 0 不代表新解码得到的 35G 路由 delta 不存在。

### P3b — wall_reconfirm2 delta 解码复核

独立 FLR 后重新执行：

```text
single 60G: bad_qwords=3301703680
  坏桶到 24G..25G，25G 以上无坏点
single 72G: bad_qwords=4697620480
  0G 桶从约 0.4G 开始，1..34G 满桶，35..36G 部分坏，36G 以上无坏点
```

`tools/decode_wall_samples.py` 的结果：

```text
single 60G: delta = 0x8c0000000 + {0x900, 0xb800, 0xbc00}
single 72G: delta = 0x8c0000000 + {0x900, 0xb800, 0xbc00, 0x11400}
```

即两组均为 `+35 GiB + 256B 粒度 tail`。72G 的污染范围落在
`[0,37G)` 预测侧，而不是位掩码的 `[0,24G)`，并且尾差指向带 swizzle/hash
的地址路由折叠。

## P4 — 金丝雀与 allocator 混淆

### 文档原顺序 C-first

VA 实际为倒序连续布局：

```text
canary-C  0x7502a0000000 .. 0x7504a0000000
filler    0x74fba0000000 .. 0x7502a0000000
high-H    0x74f6a0000000 .. 0x74fba0000000
```

因此 H 并未落在数值 VA 的“上方”。更关键的是，C 在 H 写入前已经：

```text
canary-C-after-filler-before-H bad_qwords=1020002304
canary-C-after-high-H        bad_qwords=1020002304
high-H-self                  bad_qwords=0
```

### 反向分配以获得 C→filler→H 的数值 VA 顺序

VA 顺序正确，但 28G filler 仍在 H 写之前污染 C：

```text
canary-C-after-filler-before-H bad_qwords=1073741824
canary-C-after-high-H        bad_qwords=1073741824
high-H-self                  bad_qwords=0
```

### 控制变体：C=8G、filler=24G、H=20G

该变体避免显式让 filler 大于 24G，但 C 在 H 写前仍已有坏点：

```text
canary-C-after-filler-before-H bad_qwords=590610432
canary-C-after-high-H        bad_qwords=590610432
high-H-self                  bad_qwords=0
```

因此 P4 的“C 被污染”不能在当前 allocator/物理布局下单独裁定为 H 写新增的
跨对象污染；它首先证明 filler 本身会触发共享/混叠错误。P2 已经提供了不依赖
P4 的高写→低坏因果证据。

## 历史记录裁定

| 历史记录 | 裁定 | 本次依据 |
|---|---|---|
| `vec_scan2 48 → total_bad_units=845545477` | 成立 | P0 三次精确复现 |
| 单笔 72G 分配+写 OK | 观测错误/验证不足 | P3-72G 写入成功，但完整 host 回读有 `4,697,620,480` 个坏 qword；“写 OK”不能代表数据正确 |
| 洞上 20.9G 单独正常 | 条件成立，但不能否定墙 | P2/P3 证明墙存在；P4 的 H 自校验 20G 全绿，符合“同规则写后自读自洽” |
| drip 72G 全程无幻影命中 | 不能作为墙不存在证据 | 本次未把 drip 作为主判据；P3-72G 的无混叠 host 全量回读明确失败，drip 的覆盖/时序不足以裁定全局正确 |

## 最终一句话

**墙存在；高区写会触发低区污染。cross48 与 wall_reconfirm2 解码共同表明，
实际签名是 `+35 GiB` 主 delta 加 256B 粒度尾差，属于 mod-40G 类地址路由
折叠/混叠，而不是 `[0,24G)` 的位掩码。后续 RE 应转向 FB 地址路由及其
swizzle/hash 配置。**

新工具源码：[tools/wall_reconfirm.c](../tools/wall_reconfirm.c)。
