# 研究档案索引(2026-08 战役)

> 本文件是 2026-08 研究战役的总目录。**入口结论:`FINAL_VERDICT_40G_WALL.md`**——80G 显存墙的终审判词(含架构级结论:为什么除签名固件与 fuse 烧写权外原理上不可解锁)。

## 战果(生产可用)

| 项 | 状态 | 证据 |
|---|---|---|
| 显存几何解锁 10G→80G 可见 | ✅ 生产稳定 | v62-acceptance,81920 MiB |
| 算力全速(SS0/SS1) | ✅ 满血 | BF16 166.5 / FP16 168.9 / FP32 12.26 / FP64 12.02 TFLOPS |
| PCIe Gen1→Gen2 | ✅ 生产 regkey | fuse-shadow 路线 |
| 40G 折叠墙 | ❌ 判定不可修(当前) | 根因=SEC2 加密 FWSEC + fuse 选行,见判词 |
| PCIe Gen3/Gen4 | ❌ 同上同墙 | `re/PCIE_GEN3_RE.md` |

生产配方:干净 build(`CMPUNLOCKER_PRODUCTION=1`)+ 5G phantom reserve 洞(PMA pin [36G,41G))+ 标准 regkey;llama 256K 上下文可行(全对象须落洞下 [5G,36G),配方见 EXPERIMENTS v62)。

## 文档地图(docs/)

**结论与实验日志**
- `FINAL_VERDICT_40G_WALL.md` — 终审判词(战果/根因/十条路线排除清单/存档出口/生产配置/架构结论)
- `EXPERIMENTS_20260810.md` — 主实验日志 v1–v62(每次实验的设计、数据、判读)
- `EXPERIMENT_32G_WALL_RESULTS.md` / `EXPERIMENT_32G_WALL_RECONFIRM.md` — 墙的独立复测
- `WALL_ALIAS_DECODE.md` — 折叠签名逐位反解(+35 GiB + 256B tail,8G die 粒度)
- `PLAN_SEC2_DMA_POSTPATCH.md` — SEC2 post-auth 注入方案史

**逆向工程(docs/re/,纯文字分析;固件字节与反汇编未入库)**
- Booter/SEC2 利用链:`BOOTER_RE.md`、`EXFIL_RE.md`、`REFILL_PAYLOAD.md`、`sig_dmem_template.gadget.md`、`TU10X_RELOC.md`
- GSP-RM 运行时补丁:`RE_FINDINGS.md`、`PATCH_A_RE.md`、`PATCH_B_RE.md`、`PATCH_C_VERIFY.md`、`PROGRESS_20260809.md`
- FB 路由与 VBIOS 表:`FB_ROUTING_RE.md`(GSP-RM 无 FB 路由代码的穷尽性证明)、`A100_80G_INIT_TABLE.md`、`A100_40G_80G_COLUMN_DIFF.md`(**列语义裁决:真杠杆=列 7/8 的 19 个 per-partition dword**)、`LATE_OVERRIDE_0294.md`(SKU 落地=FWSEC devinit 按 fuse 选行)、`V59_TABLE_PATCH.md`、`V59_UPLOAD_PATH.md`、`VBIOS_ROMS.md`、`VBIOS_FLASH_RISK.md`、`VBIOS_ECC_BBX.md`
- R1(WPR2 窗口)侦察:`R1_WINDOW_RECON.md`、`R1_VA_WINDOW.md`
- PCIe:`PCIE_GEN3_RE.md`

## 工具资产

**driver/(构建期补丁生成器,由 build.sh 调用)**
- `apply_profile.py` — 生产:显存几何 profile(10gb80)
- `apply_phantom_reserve.py` — 生产:5G 洞(GSP 元数据带保护)
- `apply_feat_restore.py` / `apply_ss_config4.py` / `apply_early_lmr_p1a.py` — 算力解锁(SS0/SS1/CFG1/LMR)
- `apply_sec2_dma_probe.py` — **研究平台核心**:SEC2 post-auth DMA 注入(probe==10/15/16/17/18/19),实现 HS 寄存器写入、VBIOS 表补丁 hook、GSP-RM WPR 运行时补丁 arm
- 其余 `apply_booter_*.py` / `apply_*_log.py` / `apply_*probe*.py` — 各阶段探针(历史档案,生产 build 已用 PRODUCTION 门跳过)

**tools/(卡上测试,服务器侧构建)**
- `wall_reconfirm.c` — 折叠墙探针(40G 对象即可探,增强采样)
- `decode_wall_samples.py` — 折叠签名反解器(LSB 优先逐位反解)
- `prove_80g.c` / `prove_80g_b.c` — 80G 物理存在性证明
- `mmio_rw.c` / `mmio_list.c` / `mmio_dump.c` — BAR0 读写/枚举(判 STICKS/REJECTED)
- `f0_*.cu` / `dual_*.cu` / `dual_34g_fullscan.c` — 各阶段显存扫描/混叠/腐蚀地图探针
- `vllm-*.sh` — 大模型部署配方

## 复现指引

想复现研究:先读 `FINAL_VERDICT_40G_WALL.md` 拿全局,再按 `EXPERIMENTS_20260810.md` 的版本号顺藤摸瓜到对应 `re/` 文档与工具。想直接用卡:`README.md` + `docs/re/` 不用看,生产路径只有 install.sh → build(PRODUCTION)→ 标准 regkey。
