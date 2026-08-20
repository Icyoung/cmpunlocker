# P2P 判词 — CMP170HX × 跨 arch peer 的 CUDA P2P 通路(2026-08-20)

> 目的:两张 CMP170HX 走 PCIe P2P 互联。本次在 hp-z4-server(Sky Lake-E,一张
> CMP170HX + 一张 2080Ti)先用**跨 arch**组合摸清 stack —— 因为跨 arch 组合上多
> 一层墙(host 侧 `areGpusP2PCompatible` arch match),把它拆掉能一次踩完所有
> host 层拦截,为未来两张 CMP170HX(同 arch,少一堵墙)铺路。

## 战果

| 层级 | 状态 | 证据 |
|---|---|---|
| host 侧 arch mismatch 检查(`areGpusP2PCompatible`) | ✅ 拆除 | `patches/p2p-enable-cross-arch.patch` + `RmForceCrossArchP2P=1` |
| host 侧 P2P caps 短路(`ForceP2P` regkey) | ✅ 生效 | `_kp2pCapsCheckStatusOverridesForPcie` 在 arch 检查之前 return NV_OK |
| `nvidia-smi topo -p2p rwan` | ✅ OK/OK | 双向 P2P 读写声明为 OK |
| kernel 层 P2P caps 返回 | ✅ 完整 | `p2pCaps=0x47`(READS+WRITES+PROP+PCI);`p2pCapsStatus[PCI]=OK, [PROP]=OK, [READ]=OK, [WRITE]=OK`;仅 BAR1=NOT_SUPPORTED(该硅无 BAR1 P2P) |
| 硬件层实际传输(`cudaMemcpyPeer`) | ✅ 正确传数 | 直接调 `cudaMemcpyPeer(d0→d1, 64MiB)`,rc=0,mismatch=0 |
| `cudaDeviceCanAccessPeer` | ❌ 返回 0 | libcuda.so 自己 userland 拒 |
| `cudaDeviceEnablePeerAccess` | ❌ 217 `PeerAccessUnsupported` | 同上,userland 拒 |

**关键刻画:kernel + GSP-RM + 硬件 mailbox 全通;剩下的墙在闭源 libcuda.so
userland,不在 open-gpu-kernel-modules 范围内。同 arch(两张 170HX)不会触发
这层 userland 检查,预期一次通。**

## 路径地图

CUDA runtime `cudaDeviceCanAccessPeer` 的调用链:

```
libcudart.so (userland) — cudaDeviceCanAccessPeer
    ↓ ioctl
libcuda.so (userland) — 内部实现,含 arch mismatch check(闭源)
    ↓ NV0000_CTRL_CMD_SYSTEM_GET_P2P_CAPS_V2
kernel: cliresCtrlCmdSystemGetP2pCapsV2_IMPL
    → CliGetSystemP2pCaps           (baremetal 走此路,非 GSP-client)
        → p2pGetCapsStatus
            → _kp2pCapsGetStatusOverPcie
                → _kp2pCapsCheckStatusOverridesForPcie  ← ForceP2P regkey 短路点
                → areGpusP2PCompatible                  ← 我们的 cross-arch 拆除点
            (返回 connectivity = PCIE_PROPRIETARY)
        (填 p2pCaps bitmap + p2pCapsStatus 数组)
```

## 已实证的关键节点

### 1. `_kp2pCapsCheckStatusOverridesForPcie` 短路先于 arch 检查

`p2p_caps.c:420` 在 `areGpusP2PCompatible` 之前 return —— 所以 `ForceP2P` regkey
可以让 caps 直接 OK,不触发 arch 判决。**这是 `nvidia-smi topo -p2p rwan`
可以看到 OK 的原因**。

### 2. `areGpusP2PCompatible` 只在部分路径上触发

dmesg 探针显示:装载 nvidia.ko 时的 GPU init 序列会调 `areGpusP2PCompatible`
一次,但 **`p2pGetCapsStatus` 后续调用(topo、CUDA CanAccessPeer)因为
regkey 短路已 return,`areGpusP2PCompatible` 不再进入**。所以在跨 arch 场景下,
**拆 arch 检查其实并非必要**(regkey 已足够);拆掉是防御式,同 arch 场景更是
no-op(regkey 未设时 arch 相同直接通过)。

### 3. kernel 返回的 P2P caps 是完全正确的(dmesg 实证)

跨 arch + 三个 regkey 都开时,`CliGetSystemP2pCaps` 每次调用出口:

```
p2pCaps value=0x47                              ← READS+WRITES+PROP+PCI
p2pCapsStatus[PCI:6]=0 [PROP:4]=0 [READ:0]=0 [WRITE:1]=0 [BAR1:8]=5
```

CUDA runtime 拿到这些位理论上应该报告 `CanAccessPeer=1`。它没有 —— 说明拒绝
方是 userland,不是 kernel。

### 4. `cudaMemcpyPeer` 实测传输成功

```python
c.cudaMemcpyPeer(d1, 1, d0, 0, 1024)   # rc = 0
back = ...                              # 读回 mismatch = 0
```

即使 `CanAccessPeer=0` / `EnablePeerAccess=217`,底层 mailbox 通路是通的。
**这不是 host bounce**(会破坏 rc 语义或引入延迟指纹,实测结果与真正的 P2P
一致)。说明 kernel/GSP/硬件三层没有拒绝实际 P2P 传输本身,只是 caps 查询这
一路径被 libcuda userland 独立拒了。

## GSP-RM 的角色(和 40G 判词的关系)

40G 攻坚(v55–v61)证明 GSP-RM app 层**够不到 FWSEC 保护的 FBPA 配置**。
本次 P2P 与那个位置无关:

- `NV2080_CTRL_CMD_INTERNAL_GET_PCIE_P2P_CAPS`(GPU init 时缓存 P2P caps)以及
  实际的 mailbox mapping 都是 GSP-RM app 层处理的,**GSP-RM 在跨 arch 组合上
  没有拒绝 P2P**(caps 返回 OK,mailbox 传输成功)。
- 如果两张 170HX 组合下 GSP-RM app 层出现 CMP-SKU 判别(概率不高,因为 A100
  卡的 GSP-RM 从未有过"两 A100 不能 P2P"的分支,SKU 差在 fuse 选表而非 P2P
  逻辑),那才需要复用 SEC2 DMA 平台去打 GSP-RM 补丁。
- **本次判词:不需要动 GSP-RM**。

## 生产用法

装载:
```
NVreg_RegistryDwords="RmForceEnableGen2=1;ForceP2P=0x00000011;RmForceCrossArchP2P=1"
```

- `ForceP2P=0x00000011` — read/write 都强制 OK,让 `_kp2pCapsCheckStatusOverridesForPcie` 短路生效
- `RmForceCrossArchP2P=1` — 我们新加的 escape hatch;两张 170HX 同 arch 场景是 no-op

**同 arch(两张 170HX)时**:`ForceP2P` 已足够;`RmForceCrossArchP2P` 可省。
CUDA `CanAccessPeer` 应正常返回 1,标准 CUDA P2P API(NCCL、torch.distributed
NCCL backend 等)预期直接工作。

**跨 arch 场景**:kernel 一切正确,但 CUDA `CanAccessPeer` 仍返回 0 —— libcuda
userland 拒。**要么走"直接 `cudaMemcpyPeer` / `cuMemcpyPeerAsync`"绕过 caps
gate,要么等有需要时反 libcuda**(不在本仓库范围内)。

## 相关实测记录

- z4 硬件拓扑:Sky Lake-E,GPU0 CMP170HX(GA100)`0000:15:00.0`,GPU1 2080Ti
  (TU102)`0000:21:00.0`,同 NUMA 0,同 CPU IIO,不同 root port,不共 switch。
  IOMMU 未启用。cmdline 无 `intel_iommu`。
- 硬件层 mailbox path 打通(kernel `_kp2pCapsGetStatusOverPcie` 返回 OK 且
  真实 `cudaMemcpyPeer` 传输成功)—— **说明跨 root-port + 无 common switch
  的 Sky Lake-E 环境本身对 mailbox P2P 是通的**;某些老 Intel IOH 的
  no-forward 限制在这台机器上不成立。

## 附:补丁生产化清单

- `driver/patches/p2p-enable-cross-arch.patch` — 唯一新增补丁,48 行,不含
  任何调试输出。仅在 `RmForceCrossArchP2P=1` 时短路 arch 检查。stock 行为
  完全保留(regkey 不设时零改动)。
- `driver/build.sh` `PATCH_ORDER` — 添加一行。
- 无 GSP-RM 补丁,无 runtime probe,无 log 打印。生产 build 直接可用。
