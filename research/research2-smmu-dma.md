# C3（SDM845 / AGNOS kernel 4.9）外接 NVIDIA RTX 2080 Ti：SMMU / DMA / BAR / MSI 源码级分析

> 分析对象：`commaai/agnos-kernel-sdm845`（本地克隆 `/mnt/d/dev/egpu/repos2/agnos-kernel-sdm845`，HEAD `eccd1465`，kernel **4.9.103**）
> 目标 GPU：NVIDIA RTX 2080 Ti（TU102，Rev. A，实机位于 tt@192.168.31.25 的 03:00.0）
> 标注约定：【源码确认】= 本仓库源码逐行验证；【实机验证】= ssh tt 实测；【理论推断】= 基于源码逻辑推断
> 姊妹篇：`research2-c3-pcie.md`（内核版本/DT 拓扑/控制器驱动）、`research2-nvidia-driver.md`（驱动/CUDA 层）。
> 版本说明：本报告为 smmu-dma-msi-researcher 独立复核后的**合并修订版**，修正了早期草稿中两处错误（§1.3 多功能 SID 映射、§2.3 行号），其余结论经双通道交叉验证一致。

---

## 0. 结论速览

| # | 结论 | 置信度 |
|---|------|--------|
| 1 | **出厂 AGNOS 内核 `CONFIG_PCI=n`、`CONFIG_PCI_MSM=n`、PCIEPORTBUS 关闭** —— PCI 子系统整体未编译，外接 GPU 必须重编内核 | 【源码确认】tici_defconfig:400,404,414 |
| 2 | 所有板级 DTS 中 pcie0/pcie1 均 `status="disabled"`；SMMU/SID/gicm 等 DT 基础设施已预接线，随控制器一起休眠 | 【源码确认】comma_common.dtsi:44-50 |
| 3 | 只能用 **pcie1**：MEM 窗口 509MB 放得下整卡（~304.5MB）；pcie0 仅 13MB 连 BAR0 都放不下，且入站窗口默认仅 16MB | 【源码确认】sdm845-pcie.dtsi:34-35,294-295,490 |
| 4 | PCIe 设备默认被 SMMU 强制翻译：IOMMU core 自动创建 DMA default domain，NVIDIA 驱动 probe 时拿到的就是 `iommu_dma_ops` | 【源码确认】iommu.c:851-855 + dma-mapping.c:968-991 |
| 5 | IOVA 窗口被钳到 **<4GB**（pcie 节点无 dma-ranges）；GPU 的 40/47-bit 寻址能力在本平台无意义，也无需 >4GB BAR 窗口 | 【源码确认】of/device.c:97-100,128-131 + dma-iommu.c:180-224 |
| 6 | MSI = DWC 内部控制器 → GICv3 SETSPI_NSR 门铃（非 ITS），每 RC 固定 32 向量；2080 Ti 仅需 1 向量（无 MSI-X） | 【源码确认+实机验证】 |
| 7 | **关键坑**：设备挂 SMMU domain 后 MSI 目标地址被 `dma_map_resource()` 映射成 IOVA——MSI 写也要过 SMMU，SMMU 不通则中断不来 | 【源码确认】pci-msm.c:5023-5068 |
| 8 | Resizable BAR **不需要**：ReBAR cap 当前值即标准固定尺寸，Turing 初始化按固定 BAR 工作 | 【实机验证】 |
| 9 | ⚠️ DT 缺陷：iommu-map 每条 len=1 且无 mask，GPU 的 fn1–fn3（Audio/USB/C）**不会**挂上 SMMU，只有 fn0 走翻译 | 【源码确认】of_pci.c:382 |

---

## 1. SMMU 分析

### 1.1 驱动版本与硬件模型

本树 `drivers/iommu/arm-smmu.c` 共 **5759 行**（主线 4.9 约 2200 行），是 Qualcomm 下游深度重构版（后来 5.x 上游 arm-smmu-qcom.c 的前身形态）：引入 `arch_ops` 回调、qsmmuv500 TBU 子设备探测与电源管理、SMR/S2CR 静态配置、SCM 安全调用、trace 点等。【源码确认】

模型匹配表（`arm-smmu.c:4618-4634`）：

```c
ARM_SMMU_MATCH_DATA(arm_mmu500, ARM_SMMU_V2, ARM_MMU500, NULL);        /* arm,mmu-500 */
ARM_SMMU_MATCH_DATA(qcom_smmuv2, ARM_SMMU_V2, QCOM_SMMUV2, &qsmmuv2_arch_ops);   /* qcom,smmu-v2 */
ARM_SMMU_MATCH_DATA(qcom_smmuv500, ARM_SMMU_V2, QCOM_SMMUV500,
                    &qsmmuv500_arch_ops);                              /* qcom,qsmmu-v500 */
```

SDM845 apps_smmu 节点（`arch/arm64/boot/dts/qcom/msm-arm-smmu-sdm845.dtsi:55-68`）：

```dts
apps_smmu: apps-smmu@0x15000000 {
    compatible = "qcom,qsmmu-v500";     /* → QCOM_SMMUV500 模型 */
    reg = <0x15000000 0x80000>, <0x150c2000 0x20>;
    #iommu-cells = <2>;
    qcom,skip-init;
    qcom,use-3-lvl-tables;              /* 3 级页表 */
    qcom,no-asid-retention;
    qcom,disable-atos;
};
```

**结论：C3 平台 SMMU = ARM SMMUv2 架构 + Qualcomm SMMU-500 私有扩展（QCOM_SMMUV500），不是通用 MMU-500 绑定。**【源码确认】

#### MMU-500 支持与 TLB quirk

- `ARM_MMU500` 模型存在且有专门处理：`arm_smmu.c:3855-3880` 对 MMU-500 r2+ 清 `ARM_MMU500_ACR_CACHE_LOCK`、清 `ARM_MMU500_ACTLR_CPRE`（关 next-page prefetcher）。【源码确认】
- TLB 同步：`arm_smmu_tlb_sync_cb()`（`arm-smmu.c:1176-1186`）写 `TLBSYNC` 后轮询 `TLBSTATUS.SACTIVE`，超时上限 `TLB_LOOP_TIMEOUT=500000`（约 500ms，:173）。【源码确认】
- **QCOM MMU500 ERRATA1 workaround**（`__qsmmuv500_errata1_tlbiall`，`arm-smmu.c:5099-5153`）：TLBSYNC 超时后经 SCM 调 TrustZone（`SCM_CONFIG_ERRATA1`）+ NOC 限流 WA 再等，仍超时直接 `BUG()`。该 WA 仅对 DTS 声明的 errata1 客户端生效：`qcom,mmu500-errata-1 = <0x800 0x3ff>, <0xc00 0x3ff>`（`msm-arm-smmu-sdm845.dtsi:362-364`）——SID 0x800–0xfff（mnoc 相机/显示 TBU）。**PCIe TBU 的 SID 区间 0x1c00–0x1fff 不在列表内，GPU 不受此 errata 影响。**【源码确认】
- PCIe 专用 TBU：`anoc_1_pcie_tbu`，`stream-id-range = <0x1c00 0x400>`，独立 GDSC（`hlos1_vote_aggre_noc_mmu_pcie_tbu_gdsc`）+ 时钟 `GCC_AGGRE_NOC_PCIE_TBU_CLK`（`msm-arm-smmu-sdm845.dtsi:288-300`）。注意该 GDSC 掉电时 PCIe DMA 全部 fault。【源码确认】

### 1.2 PCI 设备如何获得 Stream ID

DTS 用 `iommu-map` 按 RID 映射（不是 `iommus` 属性）。pcie0（`sdm845-pcie.dtsi:222-241`）：

```dts
qcom,smmu-sid-base = <0x1c10>;
iommu-map = <0x0   &apps_smmu 0x1c10 0x1>,
            <0x100 &apps_smmu 0x1c11 0x1>,
            ...
            <0xf00 &apps_smmu 0x1c1f 0x1>;   /* 共 16 条 */
```

pcie1（`sdm845-pcie.dtsi:507-523`）：同构，SID base = **0x1c00**（0x1c00–0x1c0f）。【源码确认】

内核路径：

1. 枚举时 `pci_dma_configure()`（`drivers/pci/probe.c:1768-1787`）→ `of_dma_configure(&dev->dev, bridge->parent->of_node)`。
2. `of_iommu_configure()` 对 PCI 设备走 `of_pci_iommu_configure()`（`drivers/iommu/of_iommu.c:146-177`）：`pci_for_each_dma_alias()` 收集别名 RID → `of_pci_map_rid(np, rid, "iommu-map", "iommu-map-mask", ...)`。
3. 命中后 `arm_smmu_of_xlate()`（`arm-smmu.c:3565-3576`）：`fwid = args[0]`（若给第 2 参数则作为 SMR mask 高位）；本表只给 1 个参数 → 纯静态 SID 无掩码。【源码确认】

匹配语义（`drivers/of/of_pci.c:367-397`）【源码确认】：

```c
masked_rid = map_mask & rid;                 /* map_mask 默认 0xffffffff (:358) */
...
if (masked_rid < rid_base || masked_rid >= rid_base + rid_len)
    continue;                                /* :382 */
*id_out = masked_rid - rid_base + out_base;
```

⚠️ **多功能设备映射缺口（修正早期草稿的错误结论）**：每条目 `rid_len=1` 且未提供 `iommu-map-mask` → **精确匹配单个 RID**。GPU 在根端口后为 01:00.0–01:00.3，RID = 0x100/0x101/0x102/0x103：
- fn0（VGA，RID 0x100）→ 命中第二条 → **SID 0x1c01** ✓；
- fn1/fn2/fn3（RID 0x101–0x103）→ **无任何条目命中** → `of_pci_map_rid` 返回 -EFAULT → `of_iommu_configure` 返回 NULL → 这三个 function **不建 fwspec、不挂 SMMU**，回落 swiotlb ops。

即"4 个功能各得独立 SID/上下文"的说法在本树不成立——只有 fn0 被 SMMU 翻译，其余直通（swiotlb 物理地址 DMA）。对 nvidia 驱动而言 audio function 的 dma_map 仍合法（swiotlb 也是有效 ops），功能不受损，但隔离性描述需更正。修复方式：DT 增加 `iommu-map-mask = <0xffffff00>` 或补齐 16×8 条目。【源码确认+理论推断】

### 1.3 默认 DMA domain：自动创建吗？—— 会

4.9 IOMMU core 已有 default domain 机制：

- `iommu_group_get_for_dev()`（`drivers/iommu/iommu.c:829-863`）：
  ```c
  if (!group->default_domain) {
      group->default_domain = __iommu_domain_alloc(dev->bus, IOMMU_DOMAIN_DMA);
      if (!group->domain)
          group->domain = group->default_domain;
  }
  ```
- 触发链：PCI 设备 `device_add` → BUS_NOTIFY_ADD_DEVICE → `iommu_bus_notifier`（`iommu.c:919-922`）→ `arm_smmu_add_device()`（`arm-smmu.c:3079-3142`：校验 SID/SMR mask 范围、分配 SME）→ 组建 group 时自动创建 **IOMMU_DOMAIN_DMA 翻译型 default domain**。【源码确认】

### 1.4 iommu-dma glue 在本树的形态

- 本树存在 `drivers/iommu/dma-iommu.c`（835 行）——Robin Murphy 旧版适配层（导出 `iommu_get_dma_cookie`/`iommu_put_dma_cookie`/`iommu_dma_init_domain`）。主线 4.12+ 才改名 `iommu-dma.c` 并扩展；**本树是改名前版本，IOVA-cookie + dma ops 功能等价**。【源码确认】
- `CONFIG_IOMMU_DMA=y`（`tici_defconfig:4408`；且 `drivers/iommu/Kconfig:385` 由 ARM_SMMU select）、`CONFIG_ARM_SMMU=y`（:4409）、`CONFIG_IOMMU_IOVA=y`（:4406）。【源码确认】
- QC 私货 `drivers/iommu/dma-mapping-fast.c`（fastmap，`CONFIG_IOMMU_IO_PGTABLE_FAST=y` tici:4403）与 `msm_dma_iommu_mapping.c`（`CONFIG_QCOM_LAZY_MAPPING=y` :4411）：相机/显示客户端**显式 opt-in** 的快速路径（`fast_smmu_dma_ops`，dma-mapping-fast.c:795，仅 VA[0,4GB) 位图管理），不会自动作用于 PCI 设备。【源码确认】

### 1.5 PCIe 设备能否绕过 SMMU？

三条路径，按可行性排序：【源码确认】

1. **不映射（最干净）**：从 DT 删除对应 `iommu-map` 条目 → `of_pci_iommu_configure` 返回 NULL → 无 fwspec → 保持默认 `swiotlb_dma_ops`（物理地址直通 + bounce 兜底）。该 SID 未编程 SMR → 落入未匹配流处理。
2. **未匹配流默认 bypass**：模块参数 `disable_bypass`（`arm-smmu.c:321-324`）默认 false → 未匹配 S2CR 为 `S2CR_TYPE_BYPASS`（:363），`sCR0.USFCFG=0`（:3917-3925）。未被 domain 收编的 stream 天然直通。
3. **DOMAIN_ATTR_S1_BYPASS**（QC 扩展，`arm-smmu.c:3417-3431`）：attach 前可把整个 domain 配置为 stage-1 bypass（attach 后改返回 -EBUSY）。pci-msm.c 的 MSI 代码会查询此属性（§4.1）。

### 1.6 NVIDIA 驱动实际看到的 dma_map_ops

时序【源码确认】：

1. 枚举：`pci_dma_configure` → fwspec 建立（含 SID）。
2. `device_add` → BUS_NOTIFY_ADD_DEVICE → `arm_smmu_add_device`（SME/group/default domain 就绪）。
3. driver bind 前：`really_probe()` 先调 `driver_sysfs_add()`（`drivers/base/dd.c:376-379`）→ BUS_NOTIFY_BIND_DRIVER → arm64 notifier（`__iommu_attach_notifier`，`arch/arm64/mm/dma-mapping.c:1010-1027`）→ `do_iommu_attach()`（`dma-mapping.c:968-991`）：
   ```c
   if (domain->type == IOMMU_DOMAIN_DMA) {
       if (iommu_dma_init_domain(domain, dma_base, size, dev))
           goto out_err;
       dev->archdata.dma_ops = &iommu_dma_ops;
   }
   ```
4. 之后才执行 `drv->probe`（`dd.c:396-402`）。

**NVIDIA 驱动 probe 一开始 `get_dma_ops(&pdev->dev)` 就是 `iommu_dma_ops`**（`dma-mapping.c:927-943`：alloc/free/map_page/map_sg/sync_*/map_resource 全套经 SMMU 翻译）。【源码确认】

非一致性：pcie 节点无 `dma-coherent` 属性 → `coherent=false` → 每次 map/unmap/sync 做 CPU cache 维护（`__iommu_sync_single_for_cpu/device` 经 `iommu_iova_to_phys()` 反查物理地址再刷 cache，`dma-mapping.c:823-847`）。GPU 高频 DMA 有真实开销但不影响正确性。【源码确认+理论推断】

---

## 2. DMA API 分析

### 2.1 dma_set_mask_and_coherent 对 >32bit mask 的行为

arm64 4.9 无架构私有 `dma_set_mask`，走通用层 → `ops->dma_supported`：

- **SMMU 路径**：`iommu_dma_supported()`（`drivers/iommu/dma-iommu.c:743-751`）**无条件 return 1** → 任意 mask（40/47/64bit）都成功。【源码确认】
- **swiotlb 路径**：`__swiotlb_dma_supported`（`arch/arm64/mm/dma-mapping.c:428-433`）→ `swiotlb_dma_supported`（`lib/swiotlb.c:1011-1017`）：`phys_to_dma(hwdev, io_tlb_end - 1) <= mask`。swiotlb 位于低段内存，40-bit mask 必过。【源码确认】

⚠️ **mask 成功 ≠ 能拿到 >4GB 的 DMA 地址**，真正约束在 IOVA 窗口：

- `of_dma_configure()`（`drivers/of/device.c:85-152`）：pcie 节点**没有 dma-ranges** → `ret<0` 分支取 `size = coherent_dma_mask + 1 = 2^32`（此时 coherent_dma_mask 刚被设为 32bit，:91-93），并把两个 mask clamp 到 `DMA_BIT_MASK(32)`（:128-131）。
- `do_iommu_attach` → `iommu_dma_init_domain(domain, base=0, size=4GB, dev)`（`dma-iommu.c:180-224`）→ `init_iova_domain(iovad, granule, base_pfn, end_pfn=(4GB-1)>>12)`。
- **此后所有 dma_map 返回的总线地址（IOVA）都在 [0, 4GB)**，无论驱动请求多大 mask。TU102 是 47-bit 寻址能力，接收 <4GB 地址毫无问题。【源码确认+理论推断】
- 附带保护：`iova_reserve_pci_windows()`（`dma-iommu.c:112-130`）把 host bridge MEM 窗口（BAR 区域）从 IOVA 空间保留，防止 GPU DMA 写到自己 BAR。

### 2.2 swiotlb 与一致性

- `CONFIG_SWIOTLB=y`（`tici_defconfig:32`）。`arch_setup_dma_ops()`（`dma-mapping.c:1103-1109`）先挂 `swiotlb_dma_ops` 兜底，再由 IOMMU 路径覆盖。【源码确认】
- 非 coherent 设备的 map/unmap 由 `__swiotlb_*` 包装做 cache 清洗。SMMU 路径下 IOVA 永远可达，不会触发 bounce。【理论推断】
- NVIDIA open 模块在 aarch64+SWIOTLB 下有显式头文件关联（`kernel-open/common/inc/nv-linux.h:169`，详见 research2-nvidia-driver.md §3c）。【理论推断】

### 2.3 入站窗口约束（PARF SLV_ADDR_SPACE_SIZE）

【源码确认】（修正早期草稿行号）

- `pci-msm.c:59`：`#define PCIE20_PARF_SLV_ADDR_SPACE_SIZE 0x358`；
- `pci-msm.c:3832-3833`：`writel_relaxed(dev->slv_addr_space_size, dev->parf + PCIE20_PARF_SLV_ADDR_SPACE_SIZE)`；
- `pci-msm.c:5809-5814`：默认 `SZ_16M`，可被 DT 属性 `qcom,slv-addr-space-size` 覆盖；
- DT：仅 pcie1 设置 `qcom,slv-addr-space-size = <0x20000000>`（512MB，`sdm845-pcie.dtsi:490`）；**pcie0 未设置 → 入站窗口仅 16MB**。

含义：RC slave 方向（GPU→SoC 读主机内存）的 AXI 可寻址窗口由该寄存器界定，配合 SMMU IOVA 翻译使用。512MB 是 pcie1 的配置上限——大块 pinned memory 场景够用但非充裕。"必须用 pcie1"的第二条硬理由。

### 2.4 NVIDIA 驱动请求的 dma mask（引用性说明，详见姊妹篇）

- nouveau：`nvkm/engine/device/pci.c:1712-1719` —— `bits = mmu->dma_bits`，失败 fallback 32-bit。
- nova-core（Rust 新驱动）：`GPU_DMA_BITS = 47`；NVIDIA 工程师明确 "Turing/Ampere 均 47-bit，至少可追溯到 Pascal"（nouveau 邮件列表 2025-10）。
- open-gpu-kernel-modules：`kernel-open/nvidia/nv.c` ~:3243 `nvl->dma_dev.addressable_range.limit = new_mask; dma_set_mask(&nvl->pci_dev->dev, new_mask);`。

**对本平台的意义**：无论请求 40 还是 47 bit，`iommu_dma_supported` 都放行，最终 GPU 收到的都是 <4GB IOVA。**GPU 侧寻址宽度不是本项目约束。**【理论推断】

---

## 3. BAR 分析

### 3.1 实测 RTX 2080 Ti【实机验证】

`ssh tt@192.168.31.25` → `sudo lspci -vv -s 03:00.0`（2026-08-25 采集）关键输出：

```
03:00.0 VGA compatible controller: NVIDIA Corporation TU102 [GeForce RTX 2080 Ti Rev. A] (rev a1)
Region 0: Memory at fa000000 (32-bit, non-prefetchable) [size=16M]
Region 1: Memory at e0000000 (64-bit, prefetchable) [size=256M]
Region 3: Memory at f0000000 (64-bit, prefetchable) [size=32M]
Region 5: I/O ports at e000 [size=128]
Expansion ROM at fb000000 [virtual] [disabled] [size=512K]
Capabilities: [68] MSI: Enable+ Count=1/1 Maskable- 64bit+
Capabilities: [bb0 v1] Physical Resizable BAR
    BAR 0: current size: 16MB, supported: 16MB
    BAR 1: current size: 256MB, supported: 64MB 128MB 256MB
    BAR 3: current size: 32MB, supported: 32MB
LnkCap: Speed 8GT/s, Width x16    LnkSta: Speed 2.5GT/s (downgraded), Width x16 (ok)
Kernel driver in use: nvidia
```

同卡另有 03:00.1 Audio / 03:00.2 USB / 03:00.3 Type-C 三个功能。【实机验证】

要点：
- 总 MMIO 足迹 ≈ 16 + 256 + 32 + 0.5(ROM) ≈ **304.5 MB**。
- **Resizable BAR 不需要**：ReBAR cap 存在但当前尺寸就是标准固定值（BAR0/BAR3 不可调，BAR1 已是最大档 256MB）。Turing 初始化按固定 BAR 工作即可。【实机验证】
- LnkSta 2.5GT/s 为空闲降速，正常。（C3 侧 PHY 单 lane Gen3 x1 的带宽约束见 research2-c3-pcie.md。）【理论推断】

### 3.2 arm64 4.9 PCI 资源分配机制

- `arch/arm64/kernel/pci.c` 极简（215 行）：`pcibios_align_resource()` 直接返回 `res->start`（:36-42）；`PCIBIOS_MIN_MEM = 0`（`arch/arm64/include/asm/pci.h:12`）。【源码确认】
- QC host 驱动枚举流程（`drivers/pci/host/pci-msm.c`，compatible `"qcom,pci-msm"` :6133）：
  1. `of_pci_get_host_bridge_resources(DT node, 0, 0xff, &res, &iobase)`（:4308）—— 从 DT `ranges` 解析总线窗口；
  2. `pci_create_root_bus()`（:4327）→ `pci_scan_child_bus()`；
  3. `pci_assign_unassigned_bus_resources(bus)`（:4334）→ 标准 setup-bus 分配。
  
  【源码确认】

### 3.3 SDM845 的 MMIO 窗口够不够？

DT 声明的窗口【源码确认】：

| RC | I/O | MEM（non-prefetch, 32-bit） | 来源 |
|---|---|---|---|
| pcie0 | 0x60200000 + 1MB | **0x60300000 + 0xd00000（≈13MB）** | sdm845-pcie.dtsi:34-35 |
| pcie1 | 0x40200000 + 1MB | **0x40300000 + 0x1fd00000（≈509MB）** | sdm845-pcie.dtsi:294-295 |

- **pcie0：13MB 连 BAR0(16MB) 都放不下**，GPU 完全不可用。【源码确认】
- **pcie1：509MB > 304.5MB ✓** 放得下整卡。
- 没有 64-bit prefetchable 窗口声明。64-bit pref BAR 怎么办？看三级 fallback（`drivers/pci/setup-res.c:241-283`）：
  1. 试 `PREFETCH|MEM_64` 窗口（不存在）；
  2. BAR 是 64-bit pref → 试 32-bit pref 窗口（也不存在）；
  3. `if (res->flags & (IORESOURCE_PREFETCH | IORESOURCE_MEM_64))` → type_mask=0，**落入 non-prefetch 窗口**。
  
  → BAR1/BAR3 自动分配进 509MB non-prefetch 窗口，无需改代码。【源码确认】
- >4GB 分配：`CONFIG_PCI_BUS_ADDR_T_64BIT=y`（tici:405）且 `pci_bus_alloc_resource()` 有 pci_high region（`drivers/pci/bus.c:234-253`），但 DT 未声明任何 >4GB 窗口 → 实际不可能分到 4GB 以上，也**不需要**（SMMU 翻译下 GPU 用 <4GB IOVA 访问系统内存；BAR 只是 CPU 侧窗口）。【源码确认+理论推断】
- CPU 访问 BAR：BAR 位于 AXI 物理空间（0x40300000–0x60000000），CPU 直访不经 apps_smmu（SMMU 只翻译设备侧流量），`ioremap` 后正常读写。【理论推断】

### 3.4 对照判定表

| 需求 | 实测卡 | C3 pcie1 | 判定 |
|---|---|---|---|
| MEM 窗口 ≥ 304.5MB | 16+256+32+ROM0.5 MB | 509MB | ✅ |
| 64-bit pref BAR 落位 | BAR1/BAR3 | fallback 进 np 窗口（setup-res.c:276-283） | ✅ |
| IO 窗口 ≥128B | 128B | 1MB | ✅ |
| 入站 DMA 窗口 | — | 512MB（PARF SLV_ADDR_SPACE_SIZE，dtsi:490） | ⚠️ 够用非充裕 |
| Resizable BAR | cap 存在但不需要 | — | ✅ 无需 |
| pcie0 替代方案 | — | MEM 13MB + 入站 16MB | ❌ 双重不足 |

---

## 4. MSI / MSI-X 分析

### 4.1 交付路径：DWC 内部 MSI 控制器 + GICv3 SETSPI_NSR 门铃（非 ITS）

DTS 证据（`sdm845-pcie.dtsi`）【源码确认】：

```dts
/* pcie0 */ qcom,msi-gicm-addr = <0x17a00040>;  qcom,msi-gicm-base = <0x2c0>;  (:215-217)
/* pcie1 */ qcom,msi-gicm-addr = <0x17a00040>;  qcom,msi-gicm-base = <0x2e0>;  (:495-497)
/* pcie0 interrupt-map: msi_0..msi_31 → &pdc 672..703 (:51-84) */
/* pcie1 interrupt-map: msi_0..msi_31 → &intc 704..735 直连 GIC (:301-338) */
interrupt-names = ..., "msi_0", ..., "msi_31";
```

- `msi-gicm-addr = 0x17a00040` 即 GICv3 `GICD_SETSPI_NSR` 门铃寄存器；MSI write 到此地址 → GIC 产生对应 SPI。
- pcie0 的 32 个向量经 PDC（SPI 672–703）；pcie1 直连 GIC（SPI 704–735）。
- **每 RC 固定 32 个 MSI 向量**：`MSM_PCIE_MAX_MSI=32`（`pci-msm.c:195`）、向量名表 ：864-869、DT IRQ 解析 ：3655-3670。

驱动实现（`drivers/pci/host/pci-msm.c`）【源码确认】：

- `arch_setup_msi_irq()`（:5117-5124）：有 `msi_gicm_addr` → QGIC 模式。
- `arch_setup_msi_irq_qgic()`（:5070-5102）：
  ```c
  irq_set_irq_type(irq, IRQ_TYPE_EDGE_RISING);
  irq_set_msi_desc(firstirq, desc);
  msm_pcie_map_qgic_addr(dev, pdev, &msg);          /* 关键，见下 */
  msg.data = dev->msi_gicm_base + (firstirq - dev->msi[0].num);
  write_msi_msg(firstirq, &msg);
  ```
- **关键集成点 `msm_pcie_map_qgic_addr()`（:5023-5068）**：
  ```c
  struct iommu_domain *domain = iommu_get_domain_for_dev(&pdev->dev);
  ...
  iommu_domain_get_attr(domain, DOMAIN_ATTR_S1_BYPASS, &bypass_en);
  if (bypass_en) return 0;                          /* bypass → 直接用物理门铃地址 */
  iova = dma_map_resource(&pdev->dev, dev->msi_gicm_addr, PAGE_SIZE,
                          DMA_BIDIRECTIONAL, 0);    /* 否则映射成 IOVA! */
  msg->address_lo = iova;
  ```
  **GPU 挂在 SMMU 翻译 domain 下时，MSI 目标地址是一个 IOVA**——MSI 写事务同样要过 SMMU 翻译到 0x17a00040。SMMU 页表/TLB 异常时 GPU 不仅 DMA 失败，**中断也会消失**。该映射由内核在分配中断时自动建立（`iommu_dma_ops.map_resource = iommu_dma_map_resource`，`dma-mapping.c:937`），驱动无需干预。【源码确认】
- legacy 兜底模式（无 gicm_addr 时）：DW `MSI_CTRL_ADDR` 编程固定 `MSM_PCIE_MSI_PHY=0xa0000000`（:204,3326-3339），单根物理线 int_msi 由 `handle_msi_irq` 软件解复用 256 个虚拟 IRQ（`PCIE_MSI_NR_IRQS=256` :194，linear irq_domain :5279-5297）。SDM845 提供 gicm_addr，走 QGIC 模式。【源码确认】
- irq chip：`pcie_msi_chip`（:4928-4936，mask/unmask 直写 MSI cap，`handle_simple_irq`）。

### 4.2 pci_alloc_irq_vectors 在本树可用吗？

可用，但底下接的是 QC 老式 arch hook 而非通用 MSI-domain 框架：【源码确认】

- `pci_alloc_irq_vectors()` 存在：`include/linux/pci.h:1318`、`drivers/pci/msi.c:1201`。
- MSI 路径：`msi_capability_init()` → `pci_msi_setup_msi_irqs(dev, nvec, PCI_CAP_ID_MSI)`（msi.c:54-76,640）→ `arch_setup_msi_irqs()` —— 被 pci-msm.c 强符号覆盖（:5126-5150）：`nvec > 32 → -ENOSPC`，逐 entry 走 qgic/default 分配器。
- `CONFIG_PCI_MSI=y`、`CONFIG_PCI_MSI_IRQ_DOMAIN=y`（tici:406-407）——后者主要服务其它 MSI-domain 控制器，QC 这条路不用通用 irq-domain MSI 层。

### 4.3 dGPU 能拿到多少向量？

- 2080 Ti 实测：`MSI: Enable+ Count=1/1 Maskable- 64bit+`，**无 MSI-X capability** → 单向量 MSI。【实机验证】
- Host 提供 32 向量/RC，单向量需求轻松满足；nvec 上限 32 远超需求。【源码确认+实机验证】
- INTx 兜底：interrupt-map 提供 int_a..int_d（pcie0→PDC 140/149-152；pcie1→intc 307/434-439）；实测 GPU pin A 存在（tt 机 routed to IRQ 43）。【实机验证】
- 参考：闭源驱动 `nv_start_device()`（open-gpu-kernel-modules nv.c）优先 MSIX→MSI→INTx，`NVreg_EnableMSI` 控制；消费级 NVIDIA 卡单向量 MSI 是常态，不影响功能。【理论推断】

---

## 5. 综合判定与工程清单

平台机制层（SMMU/DMA/BAR/MSI）在 C3 上**齐备且预接线完成**：iommu-map/SID/TBU/gicm/PARF 全部在原厂 DT 定义好，只是随 PCIe 控制器一起被禁用。启用路径：

1. **重编内核**：`CONFIG_PCI=y` + `CONFIG_PCI_MSM=y`（参考 sdm845-perf_defconfig:57-58；SMMU/DMA 相关 CONFIG 已就绪：ARM_SMMU/IOMMU_DMA/IOMMU_IOVA/SWIOTLB 均 y）。【源码确认】
2. **DT 启用**：`&pcie1 { status="ok"; }`（勿用 pcie0：13MB MEM + 入站 16MB 双重不足）；建议顺手补 `iommu-map-mask=<0xffffff00>` 修多功能映射缺口（§1.2）。【源码确认+理论推断】
3. **SMMU 是功能性依赖**：默认强制翻译 + IOVA<4GB + MSI 地址经 dma_map_resource 映射成 IOVA。若想绕过：删 iommu-map 条目（最干净）或 DOMAIN_ATTR_S1_BYPASS（§1.5）。【源码确认】
4. 上电时序：GPU 就位后再 probe（无热插拔）；必要时关 ASPM。
5. 之后进入深水区——NVIDIA 驱动 × 4.9.103 下游内核（见 research2-nvidia-driver.md）。

风险表：

| # | 风险 | 等级 | 缓解 |
|---|---|---|---|
| 1 | 512MB 入站窗口下大块 pinned memory 行为未验证 | 中 | 实机测 cudaHostAlloc/批量 H2D；必要时分块 |
| 2 | 非 coherent DMA 的 cache 维护开销影响吞吐 | 中 | 性能实测；必要时评估 S1 bypass 权衡 |
| 3 | qsmmu-v500 私有 TLB 路径 × NVIDIA 大 DMA 流量兼容性 | 中 | 先 NVMe/小卡验证枚举+DMA 再上 2080 Ti |
| 4 | fn1–fn3 绕过 SMMU（DT 缺陷） | 低 | 补 iommu-map-mask；audio function 走 swiotlb 仍可用 |
| 5 | PCIe TBU GDSC 掉电导致 DMA fault | 低 | 保持 HLOS 投票（DT 已定义） |

---

## 6. 引用清单

| 事实 | 来源（文件:行号） |
|---|---|
| CONFIG_PCI=n / PCI_MSM=n / SWIOTLB / ARM_SMMU / IOMMU_DMA | tici_defconfig:400,414,32,4409,4408 |
| apps_smmu 节点与属性 | msm-arm-smmu-sdm845.dtsi:55-68 |
| PCIe TBU（SID 0x1c00-0x1fff） | 同文件:288-300 |
| errata1 客户端范围（不含 PCIe） | 同文件:362-364 |
| SMMU 模型匹配（qsmmu-v500 等） | drivers/iommu/arm-smmu.c:4618-4634 |
| of_xlate / add_device / S1_BYPASS / disable_bypass | arm-smmu.c:3565,3079,3417,321 |
| ERRATA1 TLB WA | arm-smmu.c:5099-5153 |
| RID→SID 匹配语义（len=1 精确匹配） | drivers/of/of_pci.c:358,367,382 |
| of_pci_iommu_configure | drivers/iommu/of_iommu.c:146-177 |
| default domain 自动创建 | drivers/iommu/iommu.c:829-863,919-922 |
| dma ops 切换时序（bind 前） | arch/arm64/mm/dma-mapping.c:927,968-991,1046-1061,1103；drivers/base/dd.c:376-402 |
| iommu_dma_supported 恒真 / IOVA 窗口 | drivers/iommu/dma-iommu.c:743-751,112-130,180-224 |
| dma-ranges 缺失 → mask 钳 32bit | drivers/of/device.c:91-93,97-100,128-131 |
| PARF 入站窗口 | drivers/pci/host/pci-msm.c:59,3832-3833,5809-5814；sdm845-pcie.dtsi:490 |
| BAR 三级 fallback | drivers/pci/setup-res.c:241-283 |
| host bridge 资源解析与枚举 | pci-msm.c:4308-4334；arch/arm64/kernel/pci.c:36-42 |
| iommu-map / 窗口 / gicm / slv-size DT | sdm845-pcie.dtsi:222-241(pcie0),507-523(pcie1),34-35,294-295,215-217,495-497,490 |
| MSI QGIC 交付 + IOVA 映射 | pci-msm.c:5023-5068,5070-5102,5117-5150,194-204,3326-3339,864-869,3655-3670 |
| pci_alloc_irq_vectors | include/linux/pci.h:1318；drivers/pci/msi.c:54-76,109,640,1201 |
| 实机 BAR/MSI/链路 | tt@192.168.31.25 `sudo lspci -vv -s 03:00.0`（2026-08-25） |
| NVIDIA 驱动 dma mask 引用 | nouveau nvkm/engine/device/pci.c:1712-1719；nova-core GPU_DMA_BITS=47（邮件列表 2025-10）；open-gpu-kernel-modules nv.c ~:3243 |

*报告完成于 2026-08-25，由 smmu-dma-msi-researcher 产出（P5-8 任务，合并修订版）。*
