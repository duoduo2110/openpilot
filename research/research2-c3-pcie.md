# C3（comma three / tici）内核精确版本与 PCIe 子系统源码研究（P1-4）

> 目标：为"C3（SDM845）外接 RTX 2080 Ti"评估提供平台侧事实——内核精确版本、设备树 PCIe 拓扑、SDM845 PCIe 控制器驱动能力。
> 方法：直接分析 `commaai/agnos-kernel-sdm845`（本地克隆 `/mnt/d/dev/egpu/repos2/agnos-kernel-sdm845`，master HEAD `eccd1465`）与 `commaai/agnos-builder`（浅克隆 `/tmp/opencode/agnos-builder`）。所有结论给 file:line。
> 关联：`research2-nvidia-driver.md`（驱动/CUDA 层）、`FINAL_REPORT.md` §11-14。

---

## 0. 结论速览

| # | 问题 | 结论 |
|---|---|---|
| 1 | C3 内核精确版本 | **Linux 4.9.103**（高通下游 Android 内核）；AGNOS 用 `tici_defconfig` 构建，`CONFIG_LOCALVERSION=""` → `uname` 即 `4.9.103` |
| 2 | 原厂是否启用 PCIe | **AGNOS 出厂镜像：完全没有**。双重关闭：① `tici_defconfig` `CONFIG_PCI=n` + `CONFIG_PCI_MSM=n`（PCI 子系统与控制器驱动均未编译）；② 设备树 `comma_common.dtsi` 将 pcie0/pcie1 均 `status="disabled"`（AGNOS 锁定的内核提交 c368754c 同样如此）。**历史注记（第二轮复核）**：早期 `tici_old_defconfig` 曾是 `CONFIG_PCI=y + CONFIG_PCI_MSM=y`（L400/L414），且 `minimal` 分支的 `comma_common.dtsi` 尚无禁用段、板级 `comma_tici.dts` 还存在——即高通参考设计本可开，是 comma 在后期主动关掉的 |
| 3 | 控制器驱动 | `drivers/pci/host/pci-msm.c`（6845 行，compatible `"qcom,pci-msm"`，高通下游私有驱动）：RC 默认模式、默认 Gen2（DT 可覆盖 Gen3）、GICv2m 风格 MSI 帧、无热插拔支持 |
| 4 | eGPU 应选哪个口 | **必须 pcie1**（@0x1c08000）：MEM 窗口 509MB、`max-link-speed=0x3`；pcie0 MEM 窗口仅 13MB 且默认 Gen2，装不下 2080 Ti 的 256MB BAR1 |
| 5 | 启用代价 | 自编内核（开 CONFIG_PCI/CONFIG_PCI_MSM）+ DT 覆盖 `&pcie1 status="ok"` + PERST GPIO102 供电链——社区 nelsonjchen 的 NVMe 改装已证明该路径可走通 |

---

## 1. 内核精确版本与构建链

### 1.1 版本号

- `Makefile`（master HEAD `eccd1465` 与 AGNOS 构建锁定的提交 `c368754c26c7b9659de187addc6cccedc6cfb0a0` 完全一致）：
  ```
  VERSION = 4
  PATCHLEVEL = 9
  SUBLEVEL = 103
  EXTRAVERSION =
  NAME = Roaring Lionus
  ```
- 后缀：`arch/arm64/configs/tici_defconfig`
  - L48: `CONFIG_LOCALVERSION=""`
  - L49: `# CONFIG_LOCALVERSION_AUTO is not set`
  - → 最终内核 release 字符串就是 **`4.9.103`**（无 `-perf` 等后缀；`sdm845-perf_defconfig` L1 有 `CONFIG_LOCALVERSION="-perf"` 但 AGNOS 未使用它）。

### 1.2 构建方式（commaai/agnos-builder）

- `build_kernel.sh` L4：`DEFCONFIG=tici_defconfig` —— **AGNOS 官方构建脚本明确用 tici_defconfig**。
- `build_kernel.sh` L78/L86：`make tici_defconfig O=out` → `make -j` 产出 `arch/arm64/boot/Image-dtb`（内核+DT 合一镜像），再用 `mkbootimg --kernel Image-dtb --kernel_offset 0x8000 ...` 打包 boot.img。
- boot.img cmdline（build_kernel.sh L92-93）值得注意的两项：
  - `firmware_class.path=/firmware/image` —— 内核固件加载路径被指到 `/firmware/image`（与 open 模块 GSP 固件部署位置相关，联动 research2-nvidia-driver.md §3d）；
  - `androidboot.selinux=permissive`。
- 内核子模块锁定：`.gitmodules` → `../../commaai/agnos-kernel-sdm845.git`；`git ls-tree HEAD` = **`160000 commit c368754c26c7b9659de187addc6cccedc6cfb0a0`**。已 fetch 该提交核对：Makefile 同为 4.9.103，且其 `comma_common.dtsi` L44-50 同样禁用两个 PCIe 控制器（见 §2.3）。

### 1.3 与既有报告的一致性

`FINAL_REPORT.md` §11 "AGNOS 内核 4.9.103" 与本节源码证据一致；`research-nvidia-arm64.md` 中提到的板级 DTS 文件名 `comma_tici.dts` 不存在于当前树中——实际板级文件为 `comma_tizi.dts`（comma 3X）与 `comma_mici.dts`，公共部分在 `comma_common.dtsi`（openpilot 侧平台代号仍为 tici，见 `repos/op-0.11.1/system/hardware/tici/`）。此为本报告的更正项。

---

## 2. 设备树 PCIe 拓扑

### 2.1 总体

- `arch/arm64/boot/dts/qcom/sdm845.dtsi` L4126：`#include "sdm845-pcie.dtsi"`；L40-41 声明 `pci-domain0 = &pcie0; pci-domain1 = &pcie1;`。
- SDM845 共 **2 个 PCIe 控制器**（DWC IP + 高通 PARF 私有寄存器层），均为 x1 PHY（树中无 num-lanes 属性，驱动亦不解析 lane 数）。

### 2.2 两个控制器的关键参数（sdm845-pcie.dtsi）

| 参数 | pcie0 @ 0x01c00000 | pcie1 @ 0x01c08000 |
|---|---|---|
| compatible | `"qcom,pci-msm"` | 同左 |
| IO 窗口 | 0x60200000，1MB | 0x40200000，1MB |
| **MEM 窗口（bars）** | **0x60300000，仅 0xd00000 = 13MB** | **0x40300000，0x1fd00000 = 509MB** |
| 入站（slv）窗口 | 未设 | `qcom,slv-addr-space-size = <0x20000000>`（512MB） |
| 链路速度 | **未设 max-link-speed → 驱动默认 Gen2** | `qcom,max-link-speed = <0x3>`（Gen3） |
| PERST / WAKE GPIO | tlmm 35 / tlmm 37 | tlmm 102 / （wake 被注释） |
| 供电 | pcie_0_gdsc + pm8998 L26/L1/S9(SVS) | pcie_1_gdsc + pm8998 L26/L1/S9(NOM) |
| L1 / L1SS | `qcom,l1-supported; qcom,l1ss-supported; qcom,aux-clk-sync` | 同左 |
| MSI 帧 | `msi-gicm-addr=0x17a00040, base=0x2c0`，32 个 MSI（msi_0..31，经 PDC） | 同 addr，base=0x2e0，32 个 MSI（直连 GIC SPI 704-735） |
| SMMU | `iommu-map` → apps_smmu SID **0x1c10–0x1c1f**（16 个流 ID） | apps_smmu SID **0x1c00–0x1c0f** |
| 参考设计负载 | wil6210 802.11ad wigig（sdm845.dtsi L3978-3980 `qcom,pcie-parent = <&pcie0>`） | MTP 上用于 WLAN/其他 |

行号索引（均在 `sdm845-pcie.dtsi`）：pcie0 节点 L17 起（reg/ranges L20-31，interrupt-map L37-77，perst/wake L120-121，L1SS L139-141，gicm L152-153，iommu-map L156-173，clocks L182-196）；pcie1 节点 L207 起（bars 窗口 L216-222，max-link-speed L287，slv-addr-space L277，perst L292，gicm L305-306，iommu-map L310-326）。

### 2.3 关键事实：原厂设备树把两个口都关了

`arch/arm64/boot/dts/qcom/comma_common.dtsi` L44-50：

```dts
&pcie0 {
  status = "disabled";
};

&pcie1 {
  status = "disabled";
};
```

- `comma_tizi.dts` 与 `comma_mici.dts` 均 include `comma_common.dtsi`，且**没有任何重新启用的覆盖**（对两文件 grep `pcie` 零命中）。
- AGNOS 构建锁定的提交 `c368754c` 中同样如此（已 fetch 核对）。
- 全树唯一的 `&pcie1` 覆盖是 `sdm845-v2.dtsi` L87 起（v2 硅片的 PHY 序列修正，不改 status）。
- **推论：原厂 C3 上电后不存在任何被 probe 的 PCIe 主机控制器**——`lspci` 无输出不是驱动问题，而是总线根本没上电没注册。

---

## 3. 控制器驱动分析：drivers/pci/host/pci-msm.c

### 3.1 编译门控（为什么原厂连代码都没编进去）

- 匹配表：`"qcom,pci-msm"`（drivers/pci/host/pci-msm.c）。
- `drivers/pci/host/Makefile` L10：`obj-$(CONFIG_PCI_MSM) += pci-msm.o`。
- `tici_defconfig`：L400 `CONFIG_PCI=n`、L414 `CONFIG_PCI_MSM=n`、L404 `# CONFIG_PCIEPORTBUS is not set`、L1323-1324 `CONFIG_NVME_CORE=n / CONFIG_BLK_DEV_NVME=n`。
- 对照：通用 `sdm845_defconfig` L59-60 / `sdm845-perf_defconfig` L57-58 为 `CONFIG_PCI=y + CONFIG_PCI_MSM=y`（高通参考配置是开的，comma 选择关闭）。
- **推论：在 C3 上启用 PCIe 必须重编内核**（至少改 `CONFIG_PCI=y`、`CONFIG_PCI_MSM=y`；若同时想用 NVMe 盘再加 NVME 两项）。

### 3.2 运行行为要点（file:line）

| 能力 | 证据 | 对 eGPU 的含义 |
|---|---|---|
| RC/EP 模式 | `qcom,boot-option` 解析于 pci-msm.c L5727-5730，默认 0（RC）；C3 DT 两口均 `<0x0>` | 主机模式现成可用 |
| 链路速度 | 默认 `max_link_speed = GEN2_SPEED`（L5758），DT `qcom,max-link-speed` 可覆盖（L5759-5761）；Gen3 EQ 编程序列 L3717-3739；运行时 FORCE_GEN1 选项 L408/L1944 | pcie1 已配 Gen3；pcie0 若要用必须补属性 |
| MSI | GICv2m 风格"gicm"帧：MSI 地址写 `dev->msi_gicm_addr`（L5038 `msg->address_lo`），经 `dma_map_resource()` 映射（L5057），data = `gicm_base + irq偏移`（L5099）；每 RC 固定 32 个 MSI（DT msi_0..31） | 2080 Ti 单卡 MSI 向量需求小，32 够用；但**没有 MSI-X 多向量余量**，且属非 ITS 的老式帧机制 |
| 热插拔 | 全文件 `hotplug` 零命中；链路检测 `msm_pcie_is_link_up()` L2511，probe 时等待链路 L2746 | GPU 必须在上电/PERST 时序内就位；不支持运行中插入，重扫需走驱动 rescan/remove 接口 |
| DMA/SMMU | DT `iommu-map` 预映射 16 个 SID 到 apps_smmu | 设备 DMA 天然过 SMMU（详细分析归 P5-8 报告） |
| L1.2 低功耗 | DT `qcom,l1ss-supported` + `qcom,ep-latency=<10>` | GPU 类设备通常需在 BIOS/驱动侧禁 ASPM，此处可通过不动 L1SS 属性规避 |

### 3.3 与上游 mainline 的差异提示

此驱动是高通下游私有实现（2017-2018 版权头），**不是** mainline 的 `drivers/pci/controller/dwc/pcie-qcom.c`。含义：任何针对 mainline qcom PCIe 的文档/补丁不能直接套用；但反过来，nelsonjchen 在 C3 上的 NVMe 改装（自定义内核 + 启用 pcie1）已经实证这条下游栈能枚举真实 PCIe 设备。

---

## 4. eGPU 启用路径判定（平台侧）

要在 C3 上让 RTX 2080 Ti 被 PCI 核心枚举，最小工程清单：

1. **重编内核**：`tici_defconfig` 改 `CONFIG_PCI=y`、`CONFIG_PCI_MSM=y`（建议同时保留 `CONFIG_PCI_DOMAINS=y`/`CONFIG_PCI_MSI=y`，二者已是 y，L401/L406）。
2. **DT 覆盖**：`&pcie1 { status = "ok"; }`（必须选 pcie1——13MB 的 pcie0 MEM 窗口连 2080 Ti 的 256MB BAR1 都放不下；509MB 的 pcie1 才可行）。硬件接线沿用 NVMe 槽/转接（PERST=GPIO102，供电轨 pm8998 L26/L1/S9-NOM + pcie_1_gdsc）。
3. **时序约束**：无热插拔支持 → GPU 须在 RC probe 前完成上电与 PERST 释放（开机即插即用型方案，或加受控 reset 脚本后 `echo 1 > /sys/bus/pci/rescan`）。
4. **之后才是驱动层问题**（已在 research2-nvidia-driver.md 判定）：4.9.103 内核只能配 R410-R550 专有分支（R470/R535 LTS 最优）；open 模块要求 ≥4.15 不可用；GSP 固件路径注意 boot cmdline 的 `firmware_class.path=/firmware/image`。

风险注记：以上 1-3 只解决"枚举"，BAR 大小、TLP/DMA 质量、SMMU 行为（P5-8）、以及 NVIDIA 驱动在 4.9 下游内核上的编译兼容性仍是独立未知数。

---

## 5. 第二轮独立复核补充发现（c3-pcie-dt-researcher）

> 以下为独立重做源码取证的新增证据，与 §1-4 结论互补；行号除注明外基于本地克隆 `/home/gjf/repos2/agnos-kernel-sdm845`（= `/mnt/d/dev/egpu/repos2/agnos-kernel-sdm845` 符号链接，master HEAD `eccd146599f2e2f159d951092642689bede91632`）。

### 5.1 恢复了被删除的 comma_tici.dts（minimal 分支）

- master 的 `arch/arm64/boot/dts/qcom/Makefile:5-7` 只构建 `comma_tizi.dtb / comma_mici.dtb / comma_ultimate_provisioning.dtb`，全目录 grep `tici` 零命中。
- 逐分支探测 raw.githubusercontent.com：`comma_tici.dts` 仅 **`minimal` 分支**存在（HTTP 200；master-backup/master-thundercomm/agnos18.1.1/pcie-disable/nvme-regulator/nvme-upstream/3s 均 404）。已存 `/home/gjf/repos2/tici-dts/comma_tici.dts`。
- 板级内容：`model = "comma tici"`（L142）、`compatible = "qcom,sda845-mtp","qcom,sda845","qcom,mtp"`（L143）、`board-id <0x8 0>,<0x20 0>`（L145）、include 链 `sda845-v2.1.dtsi:14 → sdm845-v2.1.dtsi:13 → sdm845-v2.dtsi:13 → sdm845.dtsi:4119 → sdm845-pcie.dtsi`。
- **决定性 diff**：minimal 分支的 `comma_common.dtsi` **没有** `&pcie0/&pcie1 status="disabled"` 段（master 版 L33-38 才加入）；且 `sdm845-pcie.dtsi` 两节点均无 status 属性 → DT 默认 enabled。⇒ 早期 tici 树上两个控制器是使能的，"全禁用"是后期策略（与 §5.2 defconfig 演变互证）。

### 5.2 defconfig 演变：PCIe 是被 comma 主动关掉的

| defconfig | PCI | PCI_MSM | 行号 |
|---|---|---|---|
| `tici_old_defconfig` | **=y** | **=y** | L400 / L414 |
| `tici_defconfig`（现行） | =n | =n | L400 / L414 |

两份均保留 `CONFIG_PCI_MSI=y`、`CONFIG_PCI_MSI_IRQ_DOMAIN=y`（各 L406-407）。高通参考 `sdm845_defconfig` 本来就开（与 §3.1 对照一致）。

### 5.3 宽度 x1 的外部权威印证 + PHY 等级差异

§2.1 "均为 x1" 现在有引用支撑：

- torvalds/linux master `arch/arm64/boot/dts/qcom/sdm845.dtsi`（本地快照 `/tmp/opencode/sdm845-mainline.dtsi`）：`pcie0 @1c00000` 与 `pcie1 @1c08000` 均为 `compatible="qcom,pcie-sdm845"` + **`num-lanes = <1>`**（快照 L2327/L2452）；PHY 分别为 `qcom,sdm845-qmp-pcie-phy`（pcie0）与 `qcom,sdm845-qhp-pcie-phy`（pcie1，Gen3 级 QHP PHY）。
- Bjorn Andersson 主线补丁原文称 pcie0 为 "**GEN2** PCIe controller and PHY"（LKML 1911.0/05722，2019-11-06）——与 §2.2 "pcie0 默认 Gen2、pcie1 Gen3" 完全吻合。
- 本树排除项：`drivers/pci/host/pcie-qcom.c` of_match 仅 ipq8064/apq8064/apq8084（L571-574）；全树 grep `qcom,pcie-sdm845` 零命中 → SDM845 在 4.9.103 里唯一绑定就是 `"qcom,pci-msm"`。

### 5.4 pci-msm.c 关键函数行号补全（第一轮未覆盖部分）

| 功能 | 函数 | 行号 |
|---|---|---|
| probe 入口 | `msm_pcie_probe()` | 5629 |
| 上电+训练总控 | `msm_pcie_enable()`：PERST assert(3765)→vreg(3773)→clk(3780)→RC 模式 `PARF_DEVICE_TYPE=0x4`(3793)→PHY init(3853)→PHY ready 轮询(3874-3893)→PERST deassert(3906)→Gen3 setup(3913)→**LTSSM 启动 `PCIE20_PARF_LTSSM(0x1B0) BIT(8)`**(3926；宏 L86)→ELBI `XMLH_LINK_UP` 轮询+LTSSM state bits[17:12](3936-3943)→EP 配置空间可达检查(3979-3998) | 3742-4035 |
| 配置读写 | `msm_pcie_ops`(2840-2844)→`msm_pcie_rd_conf`(2820，bus0 class 改写为 PCI bridge)/`wr_conf`(2833)→`msm_pcie_oper_conf`（自旋锁+shadow）；EP 经 `msm_pcie_cfg_bdf`(2646)+iATU `msm_pcie_iatu_config`(2527) | 2527-2844 |
| MSI-QGIC | `msm_pcie_map_qgic_addr()`：`msg->address_lo = msi_gicm_addr`(5038)，S1 开启时对门铃页 `dma_map_resource`(5057)；`msm_pcie_create_irq_qgic()`(4993，pos<32 检查 5012)；选择点 `if (!dev->msi_gicm_addr) msm_pcie_config_msi_controller(dev);`(4000-4001) | 4821-5060 |
| MSI-默认路径 | PARF 内部控制器 @`MSM_PCIE_MSI_PHY 0xa0000000`(L204)写 `PCIE20_MSI_CTRL_ADDR`(3333)；`arch_setup_msi_irq_default`(4963)、`msm_pcie_create_irq`(4937)、irq_chip `pcie_msi_chip` name="msm-pcie-msi"(4928)；容量位图 256(`PCIE_MSI_NR_IRQS` L194)/QGIC 32(`MSM_PCIE_MAX_MSI` L195) | — |
| GCC 复位 | `devm_reset_control_get`(3551 core/3575 pipe)；assert→1ms→deassert 在 clk_init 内(3086-3099) | — |
| regulator | 表 L716-719（vreg-3.3/1.8V/0.9V/cx）+`gdsc-vdd`(3427)；`msm_pcie_vreg_init`(2894)。**无任何插槽 3.3V/12V 供电使能逻辑**——eGPU 供电必须板级自备 | — |
| Gen1 强制 | 模块参数 `msm_pcie_force_gen1` 写 LNKCTL2 SLS=Gen1(3916-3919) | — |

### 5.5 pinctrl GPIO 锚点（硬件对照用）

`sdm845-pinctrl.dtsi`：pcie0 = CLKREQ gpio36(func pci_e0, L320)/PERST gpio35(L333)/WAKE gpio37(L347)；pcie1 = CLKREQ gpio103(func pci_e1, L388)/PERST gpio102(L400)/WAKE gpio104(L414)。

### 5.6 ⚠️ 协议栈错配警示：ASM2464PD 是 USB4 设备端，C3 无 USB4 主机

- ASMedia 官方 datasheet（ssd-tester.de PDF）：ASM2464PD = "**USB4/Thunderbolt to PCIe/NVMe Accessory controller**"，上游 USB4 Gen3x2 (20Gbps×2)、下游 PCIe Gen4x4——其 PCIe 流量依赖 USB4/TB fabric **隧道**，主机侧必须有 USB4/TB 控制器。
- comma Chestnut 官方定位即 "PCIe Gen4 x4 to USB4 dock"（Phoronix 2026-08-13），固件开源在 tinygrad org。
- SDM845 硅片无 USB4；本内核树全部 comma dts/dtsi grep `usb4|thunderbolt|retimer|asm4242|typec|altmode` **零命中**（USB3 子系统 usb30_prim/sec GDSC 与 PCIe 是独立外设，sdm845.dtsi:4014-4018）。
- ⇒ **"chestnut + AMD RDNA4 works on C3" 的机制必须另行解释**：要么实际验证机是带 USB4 主机的 3X/tizi（与本 C3 同用 SDA845 内核基座但主机拓扑不同），要么存在板级把 pcie1 差分对直连 USB-C SS 引脚的非标准走线（内核树内无任何描述）。**建议团队把该前提的验证环境核实列入待办**——若目标是原版 tici，ADT-UT3G 无法经 USB4 隧道接入，只能走 §4 的原生 pcie1 直连路线（此时 ASM2464PD 需工作在其非 USB4 的直连 PCIe 模式或换用纯 PCIe 转接板如 R43SG 类）。
- 带宽结论不变：SoC 侧上限 **Gen3 x1 ≈ 0.98 GB/s**（pcie1），桥的 Gen3x4 能力在 C3 上最多用到 x1。

---

## 6. 引用清单

| 事实 | 来源 |
|---|---|
| 内核 4.9.103 | `repos2/agnos-kernel-sdm845/Makefile` L1-5（HEAD eccd1465 与 pinned c368754c 一致） |
| AGNOS 用 tici_defconfig | `commaai/agnos-builder/build_kernel.sh` L4、L78 |
| Image-dtb/mkbootimg/cmdline | 同上 L86-93 |
| 内核子模块 pin c368754c | `agnos-builder/.gitmodules` + `git ls-tree HEAD` |
| 双控制器参数 | `sdm845-pcie.dtsi`（行号见 §2.2 表） |
| PCIe 被禁用 | `comma_common.dtsi` L44-50（master 与 c368754c 均验证） |
| v2 PHY 覆盖 | `sdm845-v2.dtsi` L87 起 |
| wigig→pcie0 | `sdm845.dtsi` L3978-3980 |
| 驱动编译门控 | `drivers/pci/host/Makefile` L10；`tici_defconfig` L400/L414/L1323 |
| 驱动行为 | `drivers/pci/host/pci-msm.c` L2511/L2746/L3717-3739/L5038-5099/L5727-5761 |
| 平台代号对照 | `repos/op-0.11.1/system/hardware/tici/`（comma three=tici）；本仓库板级文件实为 comma_tizi/comma_mici |
| comma_tici.dts 恢复 + minimal 分支 DT 差异 | `minimal` 分支 raw 文件（本地 `/home/gjf/repos2/tici-dts/`，含 sdm845-pcie.dtsi 559 行全文与 comma_common.dtsi diff） |
| tici_old_defconfig PCI=y/PCI_MSM=y | `arch/arm64/configs/tici_old_defconfig` L400/L414 |
| 主线 num-lanes=<1>×2、QMP/QHP PHY | torvalds/linux master sdm845.dtsi 快照 `/tmp/opencode/sdm845-mainline.dtsi` L2317-2570 |
| pcie0="GEN2 controller" 定性 | LKML 1911.0/05722（Bjorn Andersson v2 补丁全文） |
| ASM2464PD=USB4 设备端桥 | ASMedia datasheet（ssd-tester.de PDF）；Phoronix《Comma.ai Launches A PCIe Gen4 x4 To USB4 Dock》2026-08-13 |

*报告完成于 2026-08-25。初版：nvidia-driver-arm64-researcher（任务 #1，P1-4）；第二轮独立复核补充 §5：c3-pcie-dt-researcher（同任务交叉验证）。*
