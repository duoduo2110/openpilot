# 第二轮最终报告：C3 + SDM845 + AGNOS(4.9) + ADT-UT3G + RTX 2080 Ti + NVIDIA Turing 驱动链源码级研究

> 团队：c3-nvidia-chain（lead 编译实验 + 4 名研究员并行）
> 证据等级标注：【实机验证】【源码确认】【QEMU验证】【社区案例】【理论推断】【需要C3实机验证】
> 支撑文件：`research2-c3-pcie.md`、`research2-smmu-dma.md`、`research2-nvidia-driver.md`、`research2-agnos-upgrade.md`、`research2-compile-experiment.md`

---

## 三个核心问题的直接回答

### 问题 1：SDM845 + Linux 4.9 能否给 RTX 2080 Ti 提供 PCIe/DMA/IOMMU/MSI/BAR 完整硬件基础？

**分两层回答：**

**硅片/DT 能力层：✅ 具备（但出厂全部关闭）**
- SDM845 有 2 个 DWC-based PCIe 控制器（`sdm845-pcie.dtsi`，compatible `"qcom,pci-msm"`）：
  - **必须用 pcie1**@0x1c08000：MEM 窗口 509MB（≥2080 Ti 全卡 304.5MB）、Gen3（`qcom,max-link-speed=<3>`）、入站窗口 512MB；pcie0 仅 13MB MEM + Gen2 + 入站 16MB —— 三重不足【源码确认 sdm845-pcie.dtsi:34-35,294-295,490】
- SMMU：apps_smmu = ARM SMMUv2 + Qualcomm qsmmu-v500 扩展，PCIe TBU 独立供电（SID 0x1c00–0x1fff）；IOMMU core 自动为 PCIe 设备建翻译型 default domain，驱动 probe 时拿到的就是 `iommu_dma_ops`【源码确认 arm-smmu.c:4618-4634, dma-mapping.c:968-991】
- IOVA 窗口被钳到 <4GB（pcie 节点无 dma-ranges）→ GPU 47-bit 寻址能力无用但也无害【dma-iommu.c:180-224】
- MSI：每 RC 固定 32 向量（GICv3 SETSPI_NSR 门铃式），2080 Ti 只需 1 向量 ✅；无 MSI-X 但不需要【实机验证 lspci：MSI Count=1/1】
- BAR：BAR0 16M + BAR1 256M(pref64) + BAR3 32M + ROM 0.5M ≈ 304.5MB 全部落入 509MB non-prefetch 窗口（setup-res.c 三级 fallback 自动处理 64-bit pref）✅；**Resizable BAR 不需要**【实机验证 ReBAR cap 当前值即固定尺寸】
- ⚠️ DT 缺陷：iommu-map 每条 len=1 无 mask → GPU fn0 挂 SMMU、fn1(audio)/fn2/fn3 绕过 SMMU 走 swiotlb（功能可用，隔离缺失；补一行 `iommu-map-mask=<0xffffff00>` 即修）

**产品交付层：❌ 出厂 AGNOS 完全关闭 PCIe**
- `tici_defconfig:400 CONFIG_PCI=n`、`:414 CONFIG_PCI_MSM=n`（控制器驱动都没编译）
- `comma_common.dtsi:44-50` 把 pcie0/pcie1 均 `status="disabled"`（历史 minimal 分支曾是开的，后期主动关闭）
- **启用 = 重编内核 + DT 覆盖**（社区 nelsonjchen 的 C3 NVMe 改装已实证此路径可枚举真实设备）

**且注意：即使打开 PCIe，ADT-UT3G 这类 ASM2464PD 桥在 C3 上走的不是 PCIe 总线而是 USB 协议（见问题 2 与 §11）。**

### 问题 2：NVIDIA Turing driver 能否在 ARM64 + C3 SDM845 + Linux 4.9 运行？

**可以，且有两条已被证据支撑的路线（推翻"平台阻断"的早期保守结论）：**

**路线 α（推荐）：专有驱动 R535 LTS aarch64 —— 官方原生支持 4.9 内核，零补丁**
- 关键事实（一手来源，research2-nvidia-driver.md §1/§5）：
  - TU102 首个支持驱动 = **410.57**（2018-09-19，supportedchips.html 含 1E04/1E07）
  - **R410–R550 各分支官方最低内核仅 2.6.9（nvidia.ko）/2.6.32（uvm）→ 3.10**；R570 起才抬到 4.15
  - **aarch64 `.run` 的 Appendix A 明确包含 GeForce RTX 2080 Ti（1E04/1E07）**——"ARM64 不支持 GeForce"被一手证据否定
  - aarch64 `.run` 是全功能包：libcuda.so + libnvidia-ml + nvidia-smi + GSP 固件（gsp_tu10x.bin）+ 图形栈
  - glibc 下限 2.17（实测符号版本）；AGNOS 用户态 Ubuntu 24.04 满足
- 前提：宿主内核 ≥3.10 且启用 PCI（即上面的重编内核）→ conftest 对老内核有完整回退路径，无需任何补丁

**路线 β（本团队实验实证）：open 模块 580.178.04 打 +109 行补丁后可在 4.9.103 上交叉编译出 nvidia.ko**
- vermagic = `4.9.103+ SMP preempt mod_unload aarch64`【实机验证，tt:/home/tt/egpu-tools/nvidia-open/kernel-open/nvidia.ko】
- 补丁全集 = 内核侧 K1-K4 + 驱动侧 N1-N16（9 文件 +109 行，逐条 error→定位→patch 见 research2-compile-experiment.md）
- 官方门槛 4.15 是保守声明；实际拦路 API 只有 6 个（timer_setup v4.15、pgprot_decrypted v4.14、kvmalloc_array v4.13、sched 头拆分 v4.11、vm_fault 重构 v4.17、Tegra BPMP 头 v4.14），每个都有机械解法
- 局限：只编了 nvidia.ko（无头 CUDA 计算够用）；insmod/运行时未验（需真机）

**不可行子路线**：open 模块不打补丁直接上 4.9（官方 #error 4.15 门槛 + timer_setup 无回退）。

### 问题 3：openpilot 的 AMD eGPU 路径要改多少才能跑 2080 Ti？

**取决于传输层选择，两条路工作量天差地别：**

| 路线 | 物理层 | 需要改什么 | 工作量 |
|---|---|---|---|
| **γ1：沿用 ADT-UT3G USB 通道** | USB3 + ASM2464PD 固件 TLP 引擎 | 必须自研 **tinygrad NV 版 USBIface**（现有 `ops_amd.py USBIface` 是 AMD 专属；`ops_nv.py PCIIface` 要求真 sysfs PCI 枚举且 ID 掩码不含 0x1Exx）+ 解决 ops_nv.py 本身不支持 Turing（缺 TURING_COMPUTE_A 等，见第一轮）+ GSP 固件 30MB 过 ~700MB/s SRAM 窗口的加载链。**等于从零写一个 NVIDIA 用户态驱动栈** | 极大（人月级，高风险）|
| **γ2：原生 PCIe 直连**（放弃 ADT，走 M.2 槽/板级改装——nelsonjchen NVMe 先例） | SDM845 pcie1 差分对直连 GPU | ①内核 K1-K4 + CONFIG_PCI=y/PCI_MSM=y + DT `&pcie1{status="ok"}`；②R535 aarch64 专有驱动零补丁安装；③my SP 的 SConscript 加 CUDA 后端分支（QUEUE_DEV='CUDA'）+ tg_input_devices；④tinygrad 零改动（sm_75 CUDA 后端第一轮已实机验证）| 中（周级，主要风险在物理改装与真机时序）|

---

# 1. C3 Kernel

- Git：https://github.com/commaai/agnos-kernel-sdm845
- Branch：master；HEAD：`eccd146599f2e2f159d951092642689bede91632`（AGNOS 构建锁定 commit：`c368754c26c7b9659de187addc6cccedc6cfb0a0`，两提交 Makefile 一致）
- Version：**Linux 4.9.103**（Makefile VERSION=4 PATCHLEVEL=9 SUBLEVEL=103，LOCALVERSION="" → uname 即 `4.9.103`）
- defconfig：`tici_defconfig`（agnos-builder/build_kernel.sh:4）；产出 Image-dtb + mkbootimg
- cmdline 要点：`firmware_class.path=/firmware/image`（影响 GSP 固件加载位）、`androidboot.selinux=permissive`

# 2. C3 Device Tree

- 板级：master 无 comma_tici.dts（仅 comma_tizi.dts/comma_mici.dts + comma_common.dtsi）；`comma_tici.dts` 存在于 minimal 分支（已恢复取证）
- PCIe：sdm845-pcie.dtsi 定义 pcie0/pcie1；**comma_common.dtsi L44-50 双双 status="disabled"**（minimal 分支无此段 → 后期策略性关闭）
- IOMMU：msm-arm-smmu-sdm845.dtsi:55-68 apps_smmu（qcom,qsmmu-v500）；pcie1 iommu-map SID 0x1c00-0x1f（sdm845-pcie.dtsi:507-523）；PCIe TBU 独立 GDSC/时钟（dtsi:288-300）
- 完整拓扑：
```
SDM845 SoC
 ├─ PCIe Controller 1 @0x1c08000 (qcom,pci-msm, DWC IP + PARF)
 │   ├─ PHY: qcom,sdm845-qhp-pcie-phy (Gen3 级), num-lanes=<1> [主线佐证]
 │   ├─ PERST=GPIO102, CLKREQ=GPIO103, WAKE=GPIO104
 │   ├─ MEM 0x40300000+509MB / IO 0x40200000+1MB / 入站(SLV)512MB
 │   ├─ MSI: gicm 0x17a00040 base 0x2e0 → GIC SPI 704-735 (32向量)
 │   └─ iommu-map → apps_smmu SID 0x1c00..0x1c0f
 └─ PCIe Controller 0 @0x1c00000 (Gen2, MEM 仅13MB, 入站16MB) ← 不适用于 dGPU
```
- ⚠️ USB-C ≠ PCIe：全树 grep usb4/thunderbolt/altmode 零命中；Type-C SS 引脚走 USB3 PHY（sdm845.dtsi:4014-4018），与 PCIe 控制器无连接

# 3. SDM845 PCIe

- driver：**`drivers/pci/host/pci-msm.c`**（6845 行高通下游私有实现，非主线 dwc/pcie-qcom.c；主线 pcie-qcom.c 的 of_match 不含 sdm845）
- source 门控：`drivers/pci/host/Makefile:10 obj-$(CONFIG_PCI_MSM)`；tici_defconfig CONFIG_PCI_MSM=n（sdm845_defconfig 参考配置是 y）
- probe：`msm_pcie_probe()` :5629 → `msm_pcie_enable()` :3742-4035（PERST assert:3765 → vreg → clk → RC 模式 PARF_DEVICE_TYPE=0x4:3793 → PHY init:3853 → PHY ready 轮询:3874 → PERST deassert:3906 → Gen3 EQ:3913 → **LTSSM 启动 PARF_LTSSM(0x1B0) BIT(8)**:3926 → XMLH_LINK_UP 轮询:3936）
- 配置读写：msm_pcie_rd_conf/wr_conf(:2820/:2833) → oper_conf（自旋锁+shadow）；EP 经 iATU(:2527)
- MSI：QGIC 帧（msi-gicm-addr 写 msg.address_lo :5038，**经 dma_map_resource 映射成 IOVA** :5057——MSI 写也要过 SMMU！）；默认路径 PARF 内部控制器 0xa0000000 256 向量；`arch_setup_msi_irqs` 强符号覆盖上限 32
- DMA：DT iommu-map 预接线；probe 前 archdata.dma_ops 已切 iommu_dma_ops（bind 时序 dd.c:376-402 → dma-mapping.c:968-991）
- 热插拔：**无**（hotplug 零命中）→ GPU 必须在 RC probe 时序内就位

# 4. SDM845 SMMU

- driver：drivers/iommu/arm-smmu.c（5759 行 QC 下游版）；模型 `qcom,qsmmu-v500` → QCOM_SMMUV500 + qsmmuv500_arch_ops（arm-smmu.c:4618-4634）
- 行为：default domain（IOMMU_DOMAIN_DMA）自动创建（iommu.c:829-863）；IOVA cookie（dma-iommu.c 旧版，功能等价主线 iommu-dma）
- IOVA 窗口：<4GB（of/device.c:91-131 无 dma-ranges 时钳 32bit）
- 绕过选项：①删 iommu-map 条目→swiotlb 直通；②未匹配流默认 bypass（disable_bypass=false, arm-smmu.c:321）；③DOMAIN_ATTR_S1_BYPASS（arm-smmu.c:3417）
- NVIDIA 驱动视角：probe 起 `get_dma_ops()` 即 iommu_dma_ops；dma_set_mask(47bit) 经 iommu_dma_supported 恒真放行，实际总线地址恒 <4GB——对 GPU 无害
- ERRATA1 TLB WA 仅涉 SID 0x800-0xfff，PCIe 不受累

# 5. RTX 2080 Ti（TU102）PCI 需求【实机验证】

- BAR0 16MB(np) + BAR1 256MB(64-bit pref) + BAR3 32MB(64-bit pref) + ROM 512KB ≈ 304.5MB
- Resizable BAR：cap 在但当前值即标准尺寸 → **不需要 ReBAR**
- MSI 单向量（Count=1/1），无 MSI-X；INTx pin A 存在兜底
- DMA 寻址 47-bit（nouveau/nova 佐证）——被 IOVA<4GB 收窄后无影响
- 多功能：fn0 VGA/fn1 Audio/fn2 USB/fn3 TypeC-UCSI

# 6. NVIDIA Driver（专有）

- Turing 最低版本：**R410 分支 410.57**（2018-09-19，supportedchips.html 一手核对；396.54 无 Turing）
- aarch64：`NVIDIA-Linux-aarch64-*.run` 公开存档自 450.51（2020）；**580.65.06 README Appendix A 实测含 RTX 2080 Ti 1E04/1E07**（472 条 GeForce 条目）
- 主机 CPU 白名单（README §2A）：Grace/Tegra K1+/X-Gene/ThunderX——**无 Qualcomm**，属"无官方背书"而非明文禁止
- 内核要求分层：R410-R550 → 2.6.9~3.10+；**R570+ → 4.15+**
- 包内容（实测 --list）：libcuda.so.96MB + libnvidia-ml + nvidia-smi + **gsp_tu10x.bin 30MB** + 完整图形栈

# 7. NVIDIA Open GPU Kernel Modules

- Turing：✅（README "any Turing or later"；芯片表含 2080 Ti）
- ARM64：✅ TARGET_ARCH=aarch64（515.43.04 起即有）；NVCPU_AARCH64 条件编译遍布 nv.c/nv-dma.c/os-interface.c
- Kernel：官方下限 **4.15**（README L73-77）；首个公开 tag **515.43.04**（2022-04/05，无任何 510.x tag）
- 新于 4.9 的 API 依赖（conftest 实证）：timer_setup(v4.15,无回退)、vm_fault_t(v4.17)、vmf_insert_pfn_prot(v4.20)、proc_ops(v5.6)、pin_user_pages(v5.6)、mmu_interval_notifier(v5.10)、dma_resv_add_fence(v5.19)、follow_pte 签名(v6.8)
- **但本团队实验证明以上门槛可通过 +109 行版本门控补丁绕过并成功编译 nvidia.ko 于 4.9.103**（见 §13/research2-compile-experiment.md）
- GSP：硬依赖（osapi.c:4976-4984 关闭 monolithic 回退）；从 `/lib/firmware/nvidia/<ver>/gsp_tu10x.bin` request_firmware；AGNOS cmdline `firmware_class.path=/firmware/image` 需适配

# 8. Linux 4.9 Compatibility（API 逐项，来自实际编译错误）

| API | 引入内核 | 4.9 状态 | 补丁 |
|---|---|---|---|
| linux/sched/{signal,task,task_stack,mm}.h | v4.11 拆分 | ❌ 无 | N1/N3/N15/N16 版本门控回退 sched.h |
| timer_setup() | v4.15 | ❌（setup_timer 时代）| N4 宏桥接 |
| pgprot_decrypted() | v4.14（SME）| ❌ | N6 no-op inline |
| kvmalloc_array/kvzalloc | v4.13 | ❌ | N7/N8 kmalloc→vmalloc 回退 |
| dma_(un)map_page_attrs | v4.10 | ❌ | N9 包装旧 dma_map_page |
| soc_device_match() | v4.10 | ❌ | N13 NULL stub |
| wait_for_random_bytes() | v4.16 | ❌ | N12 get_random_bytes 替代 |
| vm_fault 重构（.vma/.address/vm_fault_t）| v4.11/v4.17 | ❌ | N10 双签名门控 |
| devfreq governor 宏 | v4.11 | ❌（Kconfig-only）| N11 字符串 "performance" |
| soc/tegra/bpmp-abi.h | v4.14 | ❌ | N14 非 Tegra clk stub |
| get_user_pages_remote 签名差异 | Android BSP 变体 | ⚠️ 参数集不同 | N17（待细化）|
| 官方 4.15 #error 门槛 | — | 命中 | N2 实验旁路 |

# 9. CUDA ARM64（非 Jetson）

- Driver API：libcuda.so **存在于 aarch64 驱动包/sbsa deb**（不在 toolkit）✅ 实测解包（libnvidia-compute_610.57.04_arm64.deb → libcuda.so.610.57.04）
- Runtime：libcudart targets/sbsa-linux ✅（cuda-cudart-13-2_13.2.86_arm64.deb 实测）
- NVRTC：libnvrtc.so.13.2.86 sbsa ✅ 实测
- glibc 下限：三库实测均 GLIBC_2.17；CUDA 文档 sbsa 最低 RHEL8(2.28) 为发行版口径
- sm_75：CUDA 10.0 起（与 RTX 同日）；现行为 13.x 仍支持（第一轮附录）
- Jetson 区分：L4T/JetPack 独立栈，与本路线无关

# 10. ION / DMA-BUF

- C3 使用：msgq visionipc 相机缓冲走 ION（visionbuf_ion.cc L46-115）；KGSL/UI 走 /dev/kgsl-3d0
- NVIDIA CUDA 路径：tinygrad CUDAAllocator = cuMemAlloc_v2/cuMemHostAlloc/cuMemcpy*（ops_cuda.py L67-72），**零 dmabuf 依赖**
- 判定：ION/dma-buf 与 NVIDIA eGPU 推理**正交**，不是阻塞项；它只在"换主线内核"场景打击现有 QCOM 设备端管线

# 11. AMD 成功案例的真实机制（关键纠错）

**chestnut/ADT-UT3G 在 C3 上不是 PCIe 枚举，是纯用户态 USB 协议**【源码确认】：

```
tinygrad 用户态 (ops_amd.py USBIface + support/usb.py USB3/libusb)
  → USB 控制传输: pcie_cfg_req 配置空间读写
  → USB bulk/TLP 通道: BAR MMIO 访问 (USBMMIOInterface)
  → 桥内 512KB SRAM: DMA 窗口 (copy_buf 0xf000↔0x200000 等)
  → System.pci_setup_usb_bars(): 软件扮演 RC 给 GPU 分配 BAR
GPU 从未出现在 /sys/bus/pci；无需任何自定义内核模块；
ROM 态 usb-storage 认领由用户态 detach（flash.py unbind_drivers）
LTSSM 状态读 0xB450 确认桥↔GPU link（modeld ChestnutState 在用）
```

这同时解释了：① tici_defconfig CONFIG_PCI=n 是设计使然而非疏漏；② AMD 成功不依赖 PCIe 核心层；③ **该成功无法直接迁移给 NVIDIA**（NV 闭源栈是 PCI 总线驱动模型，不存在等价的 USB 用户态驱动）。

# 12. NVIDIA 阻塞点（精确到文件/函数/API）

| 层 | 阻塞点 | 位置 | 性质 |
|---|---|---|---|
| 物理 | SDM845 无 USB4；Type-C SS=USB3 PHY；对外无 PCIe 连接器 | sdm845.dtsi:4014-4018 + 全树 grep 零命中 | 硬约束（除非 M.2/板级改装直连 pcie1）|
| 内核 | CONFIG_PCI=n / CONFIG_PCI_MSM=n | tici_defconfig:400,414 | 重编可解（实验已证）|
| DT | &pcie1 status="disabled" | comma_common.dtsi:44-50 | 覆盖可解 |
| 驱动 | 580 open 模块 #error <4.15 + 6 类新 API | nv-linux.h:61 等 | +109 行已实证可解（§13）；或换 R535 专有零补丁 |
| 用户态 | libcuda aarch64 存在但依赖 nvidia.ko 运行 | — | 随驱动解决 |
| 传输 | ADT-UT3G USB 通道无 NV 用户态驱动 | ops_amd.py USBIface 为 AMD 专属；ops_nv.py PCIIface 需 sysfs 且 ID 掩码不含 0x1Exx | **最深层阻塞**（若坚持 ADT 通道）|

# 13. 最小 Patch（两条路线的完整清单）

### 路线 γ2-A：原生 PCIe + 专有 R535 aarch64（推荐起点）
```diff
--- a/arch/arm64/configs/tici_defconfig        # K-a
-CONFIG_PCI=n
-CONFIG_PCI_MSM=n
+CONFIG_PCI=y
+CONFIG_PCI_MSM=y
--- a/arch/arm64/boot/dts/qcom/comma_common.dtsi   # K-b
-&pcie0 { status = "disabled"; };
-&pcie1 { status = "disabled"; };            # 仅启 pcie1：
+/* &pcie0 保持 disabled */                   
+&pcie1 { status = "ok"; };
+#include 层面建议补 iommu-map-mask=<0xffffff00>
驱动：NVIDIA-Linux-aarch64-535.xx.run（官方支持 kernel>=3.10 + TU102）零补丁
SP：SConscript usbgpu 分支 + QUEUE_DEV='CUDA'（Patch B，见 FINAL_REPORT §13）
tinygrad：0 改动
```
### 路线 γ2-A 实证升级【实机验证 · 决定性】
```bash
# tt 上实测：R535 LTS 专有驱动对 C3 内核树零补丁构建
$ cd nv535-extract/kernel
$ IGNORE_MISSING_MODULE_SYMVERS=1 make -j32 module ARCH=arm64 \
    CC=aarch64-linux-gnu-gcc LD=aarch64-linux-gnu-ld AR=aarch64-linux-gnu-ar \
    OBJCOPY=aarch64-linux-gnu-objcopy SYSOUT=SYSSRC=~/egpu-tools/agnos-kernel-sdm845
EXIT=0 → nvidia.ko(72MB) + nvidia-uvm.ko(34MB) + nvidia-drm/modeset/peermem 全部产出
vermagic: 4.9.103+ SMP preempt mod_unload aarch64   ← 精确匹配 C3 目标
firmware: nvidia/535.309.01/gsp_tu10x.bin            ← GSP 声明内置
```
- **nvidia-uvm.ko 也成功**（统一内存），优于 580 补丁版（该版本 uvm 未移植）
- 唯一环境开关 `IGNORE_MISSING_MODULE_SYMVERS=1` 源于实验树无完整内核构建（K3）；真机全量构建内核后无需此变量
- 路线 α 从"理论可行"升级为"**编译级实证**"；剩余未验证项收窄为：insmod 加载、SMMU×NVIDIA DMA 运行时、物理接入

### 路线 γ2-B：原生 PCIe + open 580（已实证）
- K1-K4 + N1-N16 共 25 commits，git log 于 tt:~/egpu-tools/{agnos-kernel-sdm845,nvidia-open}，净 diff 9 文件 +109 行
- 附带收益：即使未来升级驱动分支，补丁模式可直接复用

### 路线 γ1：ADT USB 通道 + 自研 NV USBIface
- 需新增：`tinygrad/runtime/ops_nv.py` 增加 USBPCIDevice 后端（仿 ops_amd.py USBIface L910 结构）+ ops_nv.py Turing 支持（TURING_COMPUTE_A=0xC46F/GPFIFO、COMPUTE 0xC5C0、DMA 0xC5B5 + QMD 位域核对）+ GSP 30MB 固件经 SRAM 窗口装载逻辑 + 700MB/s 带宽下的性能评估
- 评级：研究项目（人月级），不建议作为关键路径

# 14. Kernel 升级方案

| 目标 | BSP 支持 | AGNOS 三大件(ION/KGSL/CamX) | 判定 |
|---|---|---|---|
| 4.14 | ❌ 无 sdm845 | — | 死路 |
| 4.19 | ⚠️ 仅残留 dtsi，无 defconfig/tici | KGSL/CamX 未移植 | 死路（≈重做 BSP）|
| 5.4/5.10/5.15/6.1 BSP | ❌ | GKI 移除 ION 等 | 死路 |
| 主线 6.x（社区 sdm845 到 6.18） | ✅ 平台成熟 | binder✅ ION❌(dma-heaps) KGSL❌(Freedreno 替代) CamX❌(最大阻塞) | = 主线移植工程，非"升级" |

**最小可行 = 留在 4.9**（配 R535 专有或打补丁的 580 open）；主线 6.x 属独立大工程。

# 15. 最终架构（推荐落地形态）

```
[现实最优 · 中等工作量]
C3 (SDM845, 重编内核 4.9.103: CONFIG_PCI=y + CONFIG_PCI_MSM=y, DT pcie1=ok)
  → M.2/板级 PCIe 直连 (pcie1, Gen3 x1, PERST GPIO102 时序内就位)
  → RTX 2080 Ti (BAR 304.5MB 落 509MB 窗口)
  → NVIDIA-Linux-aarch64-535.x.run (官方支持 4.9 内核, 零补丁)
  → libcuda.so (GLIBC_2.17) + NVRTC(sbsa)
  → tinygrad CUDA 后端 (sm_75 已实机验证, 880M 32.9FPS@x86 同构推理)
  → my SP SConscript CUDA 分支 (QUEUE_DEV='CUDA')
预期性能: PCIe Gen3 x1 ≈ 0.985GB/s → 权重常驻显存后推理不受限;
         H2D 冷加载 1.77GB pkl ≈ 2.2s(理论)

[对照 · 已产品化]
C3/3X → ADT-UT3G(UT3G-DUAL ADD1:0002, my SP 已适配) → USB 用户态协议
      → AMD RX 9060XT (tinygrad AM USBIface) → 880M
[对照 · 研究向]
C3 → ADT-UT3G → 自研 NV-over-USB 用户态栈 (人月级, 不推荐)
```

# 16. 可行性评分

| 层 | 路线γ2(原生PCIe) | 路线γ1(ADT USB) |
|---|---|---|
| 硬件 | 75%（硅片✅/DT✅/量产走线需改装）| 90%（AMD 已证物理+固件通路）|
| Kernel | 85%（编译实验实证；真机加载⏳）| 100%（无需内核改动）|
| Driver | 95%（R535 零补丁编译实证：nvidia+uvm+drm+modeset 全家桶 vermagic 匹配）| 20%（需自研用户态栈 + Turing 补丁）|
| CUDA | 90%（官方 aarch64 全组件）| 10% |
| tinygrad | 95%（sm_75 实机✅）| 15% |
| openpilot/SP | 70%（SConscript 小改 + 真机联调）| 10% |
| **综合** | **≈75%**（R535 零补丁编译实证后上调；关键剩余风险：insmod 真机加载、SMMU×NVIDIA DMA 运行时、MSI-IOVA 行为、物理接入）| **≈10-15%** |

---

## 严格证据索引
- 内核/DT/SMMU/PCIe/MSI/BAR 全部行号：research2-c3-pcie.md §6、research2-smmu-dma.md §6
- NVIDIA 版本/包内容/glibc：research2-nvidia-driver.md（download.nvidia.com 一手档案 + .run 解包实测）
- 编译实验全程日志：tt:/tmp/nvbuild*.log、/tmp/kbuild.log；git 仓 tt:~/egpu-tools/{agnos-kernel-sdm845,nvidia-open}（25 commits）
- 传输机制：research2-agnos-upgrade.md §4（flash.py/system.py/ops_amd.py/asm2464pd-firmware 源码链）

---

## 附：第二轮新增交付物清单（2026-08-25）

| 产物 | 位置 | 说明 |
|---|---|---|
| **nvidia.ko (535.309.01, aarch64, 零补丁)** | tt:~/egpu-tools/nv535-extract/kernel/nvidia.ko | vermagic=4.9.103+ SMP preempt aarch64 |
| nvidia-uvm/drm/modeset/peermem.ko | 同上 | 全套五模块 |
| nvidia.ko (580.178.04 open + 补丁版) | tt:~/egpu-tools/nvidia-open/kernel-open/nvidia.ko | +109 行移植实证 |
| C3 内核实验树（含 K1-K4 提交）| tt:~/egpu-tools/agnos-kernel-sdm845 | git log 可查全部 diff |
| R535/R580 aarch64 .run 及解包 | tt:~/egpu-tools/NVIDIA-*.run, nv*-extract/ | libcuda/GSP固件/图形栈齐全 |

### 与第一轮结论的差异修订
| 第一轮说法 | 第二轮修正 |
|---|---|
| "NVIDIA 驱动被平台阻断，AMD 为唯一路线" | 平台阻断仅指**出厂配置**；重编内核+R535 后驱动层畅通（编译级实证）|
| "C3+chestnut 走 PCIe 枚举" | 实为 USB 用户态 TLP 协议（GPU 不进 /sys/bus/pci）——AMD 成功不可迁移给 NVIDIA |
| "open 模块需 ≥4.15 → 死路" | 专有 R410-R550 官方支持内核 ≥2.6.9~3.10；open 580 亦可 +109 行移植 |
| "ADT-UT3G 可用于 2080 Ti" | 仅当自研 NV-over-USB 用户态栈；否则 2080 Ti 需走 M.2 级原生 PCIe 直连 |

### 剩余真机验证清单（收敛后）
1. 重编 AGNOS 内核刷入 → `lspci` 应见 GPU（pcie1）
2. insmod nvidia.ko（顺序 nvidia → uvm → modeset）→ dmesg 观察 SMMU/DMA 行为
3. GSP 固件部署到 `/lib/firmware/nvidia/535.309.01/`（注意 cmdline firmware_class.path=/firmware/image）
4. nvidia-smi → cuInit → tinygrad NVRTC 冒烟 → 880M pkl 推理
5. my SP SConscript CUDA 分支联调
