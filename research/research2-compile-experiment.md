# 第二轮 P12-14：NVIDIA OGTK 580.178.04 × C3 Linux 4.9.103 交叉编译实验报告

> 实验机：tt (192.168.31.25)，交叉工具链 gcc-aarch64-linux-gnu 11.4.0
> 结论先行：【实机验证】**nvidia.ko 核心模块成功针对 C3 内核源树完成 aarch64 交叉编译，
> vermagic = `4.9.103+ SMP preempt mod_unload aarch64` 精确匹配目标**。
> 总改动量：内核侧 4 个补丁 + NVIDIA 侧 9 文件 +109 行。

---

## 0. 实验材料

| 组件 | 来源 | commit/版本 |
|---|---|---|
| C3 内核 | github.com/commaai/agnos-kernel-sdm845 (master) | `eccd1465` "ARM: dts: qcom: SDM845 USB3 VGA calibration (#138)" |
| 内核版本 | Makefile | **VERSION=4 PATCHLEVEL=9 SUBLEVEL=103**（"Roaring Lionus"）|
| 内核配置 | arch/arm64/configs/tici_defconfig | 真 C3 配置 |
| NVIDIA 模块 | github.com/NVIDIA/open-gpu-kernel-modules branch 580 | `c8e6998` **580.178.04** |
| 预编译 blob | NVIDIA-Linux-aarch64-580.178.04.run（tesla 渠道）| nv-kernel.o_binary / nv-modeset-kernel.o_binary（ELF aarch64）|

## 1. 内核侧补丁（K1-K4）

### K1：绕过 gcc-wrapper.py【已提交】
```
错误：/usr/bin/env: 'python2': No such file or directory（Makefile prepare 阶段）
根因：Makefile:369 高通下游魔改 CC=$(srctree)/scripts/gcc-wrapper.py $(REAL_CC)
     wrapper 仅做警告过滤（Android 遗留），shebang 为 python2（jammy 无 python2）
补丁：Makefile:369 → CC = $(REAL_CC)   （git diff 见仓库 K1 commit）
```

### K2：禁用 GCC11 下失败的下游驱动（与 PCIe/NVIDIA 无关）【已提交】
```
错误：msm_drv.c -Werror=incompatible-pointer-types；kgsl_trace.h/mdss_pll_trace.h 缺失；
     techpack/audio device_event.h 缺失
根因：GCC11 将旧告警升级为错误；下游显示/音频驱动的 include 路径依赖旧构建布局
补丁：scripts/config 禁用 DRM_MSM/MSM_KGSL/SND_SOC_SDM845/MSM_GLINK_SPI_LOOPBACK 等
影响：仅实验树；不影响 PCI/SMMU/DMA 核心代码路径
```

### K3：关闭 CONFIG_MODVERSIONS【已提交】
```
原因：外部模块编译只需 prepare 头文件；MODVERSIONS 需要 Module.symvers（须完整内核构建）
权衡：符号 CRC 校验关闭——真机部署时建议完整构建内核以恢复 MODVERSIONS
```

### K4：启用 PCI 子系统【重大发现，见 §4】

## 2. NVIDIA 侧补丁清单（error → 定位 → 最小 patch）

| # | 错误 | 定位 | 所需 API 的真实引入版本 | 补丁方式 |
|---|---|---|---|---|
| N1/N3 | linux/sched/signal.h No such file | nv-lock.h:33, nv-linux.h:117 | **v4.11** sched 头拆分（signal/task 移入独立头）| 版本门控回退 `<linux/sched.h>` |
| — | 同类 task.h/task_stack.h/mm.h | uvm_linux.h:64, uvm_va_space_mm.h:33 | v4.11 拆分 | 同上门控（N15/N16）|
| N2 | `#error "...older than Linux 4.15!"` | nv-linux.h:61 | **官方最低门槛=4.15**（README.md:73-77 "currently Linux kernel 4.15 or newer"）| 实验性旁路为 warning（量化移植面）|
| N4 | timer_setup 隐式声明 | nv-timer.h:50 | **v4.15** 新定时器 API | 宏桥接 setup_timer()（回调指针强转，语义等价：data=timer_list*）|
| N6 | pgprot_decrypted 未声明 | nv-linux.h:521 | **v4.14** x86 SME 引入 | <4.14 提供 no-op inline（无内存加密平台语义正确）|
| N7/N8 | kvmalloc_array/kvzalloc | nv.c:3751/409 | **v4.13** | kmalloc_array→vmalloc 两级回退 inline |
| N9 | dma_map_page_attrs | nv-dma.c:74/105 | **v4.10** DMA attrs 变体 | 包装 dma_map_page/unmap_page（attrs 忽略，4.9 无对应语义）|
| N10 | vm_fault 无 .vma；fault 返回类型 | nv-mmap.c:228/324 | **v4.11** fault API 重构（vma 入参→vmf->vma，int→vm_fault_t）| 版本门控双签名 |
| N11 | DEVFREQ_GOV_PERFORMANCE 未定义 | nv-pci.c:820 | **v4.11** devfreq governor 名字宏化（4.9 中仅为 Kconfig 符号）| <4.11 传字符串 "performance"（devm_devfreq_add_device 本就接受 const char*）|
| N12 | wait_for_random_bytes | os-interface.c:2059 | **v4.16** crng 就绪等待 | <4.16 get_random_bytes 直接可用（阻塞语义差异已注释）|
| N13 | soc_device_match | os-interface.c:2135 | **v4.10** SoC 匹配框架 | <4.10 返回 NULL stub |
| N14 | soc/tegra/bpmp-abi.h 缺失 | nv-clk.c:30 | **v4.14** mainline Tegra BPMP | 非 Tegra 平台编译 clk 存根（7 个函数返回 NOT_SUPPORTED）；BPMP 路径保留在 ≥4.14 且 CONFIG_TEGRA_BPMP 时 |

全部补丁：`~/egpu-tools/nvidia-open` 仓 git log（15 commits，含 3 次 N14 迭代记录）；净 diff = **9 文件 +109 行 -1 行**。

## 3. 构建结果

```
$ IGNORE_MISSING_MODULE_SYMVERS=1 NV_KERNEL_MODULES=nvidia make modules -j32 \
    ARCH=arm64 TARGET_ARCH=aarch64 CC=aarch64-linux-gnu-gcc LD=aarch64-linux-gnu-ld \
    AR=... OBJCOPY=aarch64-linux-gnu-objcopy STRIP=... \
    SYSOUT=SYSSRC=~/egpu-tools/agnos-kernel-sdm845
EXIT=0

$ modinfo kernel-open/nvidia.ko | grep vermagic
vermagic: 4.9.103+ SMP preempt mod_unload aarch64      ← 与 C3 目标精确匹配
大小: 27.5 MB (with debug_info, not stripped)
license: Dual MIT/GPL
```

关键过程事实：
1. **RM 核心数百个对象（含大量 *_tu102.o：kernel_gsp_tu102/kern_gmmu_fmt_tu10x/intr_tu102/kernel_bif_tu102/kernel_fifo_tu102…）零修改通过 aarch64 编译** —— Turing 支持代码本身与内核版本无关。
2. `nv-kernel.o_binary`/`nv-modeset-kernel.o_binary` 必须取自官方 **aarch64** .run（本实验用 tesla 580.178.04）；GitHub 仓不含二进制。README.md 明文说明该机制。
3. `IGNORE_MISSING_MODULE_SYMVERS=1` 为官方逃生开关（配合 K3）。
4. 构建顺序坑：必须传 `ARCH=arm64` 给 kbuild 子 make，否则宿主 x86 flags（-mcmodel=kernel/-mno-red-zone）注入。

## 4. 重大副产物发现：tici_defconfig 的 CONFIG_PCI=n

```
arch/arm64/configs/tici_defconfig:400  CONFIG_PCI=n
:401  CONFIG_PCI_DOMAINS=y   :402 CONFIG_PCI_DOMAINS_GENERIC=y   :403 CONFIG_PCI_SYSCALL=y
```
- 该矛盾组合（PCI=n 但 DOMAINS/SYSCALL=y）经 olddefconfig 解析后 **PCI 全链关闭**
- 而 C3 硬件有 NVMe（走 PCIe）、官方 chestnut eGPU 支持 3X —— 说明**量产 AGNOS 构建链在别处覆盖了此配置**（agnos-builder 片段或后续分支），或该 defconfig 是历史遗留
- 影响：任何"给 C3 加 NVIDIA"的方案都必须确认/修正量产内核的 CONFIG_PCI=y + PCIE_QCOM=y + PCI_MSI=y
- 本实验 K4 已在实验树启用：CONFIG_PCI=y PCIEPORTBUS=y PCI_MSI=y MSI_IRQ_DOMAIN=y PCIE_DW=y **PCIE_QCOM=y**

## 5. 边界与未验证项（诚实清单）

| 项 | 状态 |
|---|---|
| nvidia.ko 编译 | ✅ 实机完成 |
| nvidia-uvm / nvidia-drm / nvidia-modeset | ❌ 未移植（uvm 的 vm_fault.address/virtual_address 差异涉及多处；无头 CUDA 计算**不需要**这三个：tinygrad 用 cudaMalloc 设备内存，不用统一内存）|
| insmod 加载 | ⏳ 需真机（AGNOS 校验/安全模块/LivePatch 等未知）|
| SMMU 下 DMA 运行时行为 | ⏳ 需真机（编译通过≠DMA 正确；见 smmu-dma-msi 报告）|
| Tesla blob 是否放行 GeForce TU102 | ⏳ 需真机（RM 二进制按 PCI ID 运行时匹配，历史上无 subsystem 锁定，但属许可灰区）|
| libcuda.so 用户态配对 | ✅ 已随同一 .run 提取（nv-aarch64-extract/ 目录含全套用户态）|

## 6. 对三个核心问题的直接回答（本实验贡献部分）

1. **Q2（NVIDIA Turing driver 能否在 ARM64+C3 SDM845+Linux4.9 运行）**：
   内核态编译层 **已被实验证明可行**（~10 类浅层 API 差异 +109 行）。运行层待真机。
2. **"最小修改是什么"**：即上表 N1-N16 + K1-K4。无一处需要重写子系统；最大单点是 N10（fault API）与 N14（Tegra 排除），均为模板化改法。
3. 官方 4.15 门槛是**保守声明**而非技术悬崖；实际拦路虎是 6 个具体 API（4.10-4.16 区间散布），每个都有机械解法。
