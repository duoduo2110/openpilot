# RTX 2080 Ti (Turing) 在 aarch64 Linux 的驱动/CUDA 可行性研究报告

> 任务：区分 Jetson / C3（comma three）/ 标准 SBSA 三条路径，评估 2080 Ti 在 ARM64 上的驱动与 CUDA 支持；给出 C3 SoC 规格；梳理 AMD RDNA4 的成功参照路径。
> 结论速览：**2080 Ti 硬件本身在 aarch64 上有驱动代码支持（开源内核模块明确列出 RTX 2080 Ti），但官方仅承诺数据中心 GPU + SBSA ACPI 平台；C3 是 SDM845 + Android 下游内核 4.9 + 设备树平台，三条 NVIDIA 路径全部走不通，需要自研用户态驱动级别的工程**。AMD 路径（tinygrad AM 用户态驱动）已被 Tiny Corp 在 Apple Silicon 上完整验证，是现实得多的参照。

---

## 1. 三条路径的本质区别

| 路径 | 平台形态 | NVIDIA 驱动来源 | 2080 Ti 可行性 |
|---|---|---|---|
| **Jetson**（Orin/Nano 等） | Tegra SoC，iGPU，L4T/BSP 内核 | Tegra 专用驱动（iGPU），非 dGPU 驱动 | ❌ 完全不适用：Jetson 驱动栈只服务片上 GPU，PCIe 外接 dGPU 仅 DRIVE OS（车规）支持 Ampere+ |
| **标准 aarch64 SBSA 服务器** | UEFI+ACPI 启动的 ARM 服务器（Ampere Altra、Kunpeng 等） | `NVIDIA-Linux-aarch64-*.run`（SBSA 版）/ 开源内核模块 | ⚠️ 半可行：官方支持矩阵只有数据中心卡；GeForce 属"能编译、不承诺"，社区有成功案例但无保证 |
| **C3（comma three/tici）** | SDM845 手机 SoC，Android 系（AGNOS），设备树启动，下游内核 4.9 | 无任何官方渠道 | ❌ 三重阻断（见 §3） |

---

## 2. NVIDIA 在 aarch64 上的官方现状

### 2.1 SBSA 专有驱动
- NVIDIA 提供 aarch64 版 `.run` 安装包与仓库包（如 Ubuntu arm64 deb），但其 README《Minimum Requirements》明确只支持特定服务器 SoC 与**数据中心 GPU**（开发者论坛 2023 年官方回复确认：GeForce RTX 2060 + LX2160A 这类组合不在支持列表，无解）。GeForce/Turing 从未被列入 aarch64 支持矩阵。
- x86_64 上 GeForce 与数据中心卡共用同一驱动；aarch64 上则人为分割——这是商业策略限制而非技术缺失。

### 2.2 开源内核模块（open-gpu-kernel-modules）
- 官方 README："The NVIDIA open kernel modules can be used on any Turing or later GPU"，兼容表**逐条列出 NVIDIA GeForce RTX 2080 Ti（PCI ID 1E04 / 1E07）**。
- CPU 架构支持 x86_64 与 aarch64（社区分析还观察到 riscv 目录）。
- 自 R560 分支起，开源内核模块成为默认安装选项；Turing 及以后必须/默认使用 GSP 固件（GPU 内 RISC-V 核心运行的完整 Resource Manager 固件，`gsp_tu10x.bin` 对应 Turing）。
- **关键点**：这意味着 2080 Ti 的内核态驱动源码在 aarch64 上是可编译的，GSP 架构也大幅降低了对宿主平台的假设。真正的门槛不在 GPU 侧，而在**宿主平台侧**：

### 2.3 平台要求 = C3 的死穴
开源模块/专有驱动的运行前提：
1. **标准 PCI 枚举 + BAR 映射**（C3 有：NVMe 就走 PCIe ✓）
2. **ACPI**（电源管理 `_DSM`、中断映射等大量调用；ARM 服务器走 SBSA ACPI；C3 是**设备树启动，无 ACPI 表** ✗）
3. **内核版本匹配**：当前开源模块要求较新内核（≥4.15 且持续抬高下限）；**C3 的 AGNOS 内核是 4.9.103**（`commaai/agnos-kernel-sdm845` master Makefile：`VERSION = 4 PATCHLEVEL = 9 SUBLEVEL = 103`，高通下游 Android 内核）✗
4. **Android 用户态/内核差异**：binder、sepolicy、无 systemd、内核头文件需自行从 comma 仓库构建 ✗（可做但全靠自己）

---

## 3. C3（comma three）SoC 规格与 eGPU 接入点

### 3.1 规格（tici）
| 项目 | 规格 |
|---|---|
| 代号 | tici（comma three，2021-07 发布，2023-10 停产）；comma 3X = tizi，同 SoC |
| SoC | **Qualcomm Snapdragon 845（SDM845）**，Thundercomm D845 SOM |
| CPU | 8 核 Kryo 385（4× Cortex-A75 + 4× Cortex-A55），**降频至约 1.68 GHz** |
| GPU | **Adreno 630**（openpilot 车型模型即跑在此 GPU 上，经 tinygrad QCOM/OpenCL 后端；tinygrad 官网原话："tinygrad is used in openpilot to run the driving model on the Snapdragon 845 GPU"） |
| RAM | 8 GB LPDDR4X |
| 存储 | 32GB UFS / 256GB NVMe / 1TB NVMe 变体 → **板上有 PCIe host（NVMe 用）** |
| OS | AGNOS（Android 基础的 AOSP 系统）；内核 `commaai/agnos-kernel-sdm845`（**Linux 4.9.103 下游内核**，设备树 `arch/arm64/boot/dts/qcom/comma_tici.dts`） |

> 注意：常见误解是 C3 用骁龙 855/SM8150——错误。855 是手机平台；C3/3X 均为 **SDM845**。

### 3.2 若坚持在 C3 上接 2080 Ti，物理层可行、软件层需自研
- 物理层：NVMe M.2 M-key 槽即 PCIe 通道，可用转接线上 dGPU（供电另配 ATX 电源）；SDM845 PCIe 为 Gen3 x1 级别带宽（~985 MB/s），对推理权重加载是瓶颈但可接受。
- 软件层的三条可能路线（按工程量排序）：
  1. **移植 tinygrad 用户态 NV 后端思路到 AGNOS**：tinygrad 的 `ops_nv.py` 本身就是"绕过 libcuda、用户态 ioctl 直连"的驱动（见 research-tinygrad.md §4），它依赖的只是 `/dev/nvidiactl`、`/dev/nvidia-uvm` 和内核 nvidia.ko 提供的 ioctl 接口——仍需某个能跑在 4.9/DT 内核上的 nvidia 内核模块，工作量最大。
  2. **给 4.9 DT 内核移植 open-gpu-kernel-modules**：需 backport 到 4.9 + 补 ACPI 依赖（GSP 路径减少了对 ACPI 的部分依赖，但 nv-acpi.c 的调用面仍在）；高风险研究项目。
  3. **放弃 NVIDIA，改用 AMD（推荐，见 §5）**。

---

## 4. Jetson 为什么完全不适用（澄清用）

- Jetson 的"L4T/DRIVE"驱动栈面向 Tegra iGPU（统一内存、片上 ISP/ALE），与桌面 dGPU 驱动是两套代码。
- Jetson PCIe 可以插卡（如 Orin NX 有 PCIe endpoint/host），但 NVIDIA 不提供任何在 L4T 上驱动 GeForce dGPU 的组件；dGPU over PCIe 仅在 **DRIVE OS**（车规，配合 Ampere dGPU）受支持。
- 因此"用 Jetson 的驱动经验迁移到 C3"不成立——两者唯一共同点是 aarch64 指令集。

---

## 5. AMD RDNA4 成功路径（对照参照系）

### 5.1 Tiny Corp / tinygrad 已验证的时间线（Apple Silicon = aarch64）
| 时间 | 里程碑 |
|---|---|
| 2025-05 | 世界首次从 Apple Silicon 经 **USB3**（改装 ASM2464PD 方案的 ADT-UT3G 盒）驱动 AMD GPU，纯用户态驱动 |
| 2025-10 | NVIDIA RTX 经 USB4 在 MacBook Pro M3 Max 上跑通（当时需关 SIP） |
| 2026-03-31 | **Apple 正式批准 TinyGPU DriverKit 扩展**：macOS 免关 SIP、系统设置里开关即可外接 AMD(RDNA3+)/NVIDIA(Ampere+) GPU；基准：Mac mini M4 + RX 7900 XTX 跑 Qwen 3.5 27B 达 18.5 tok/s |
| 2026-05 | Plugable 用 TB5 坑位实测 **RX 9070（RDNA4）与 RTX 5070** 均可在 M2 MacBook Pro 上完成 TinyGPU 初始化并跑 tinygrad LLM demo |

要点：**这条链路里 NVIDIA 能跑是因为 macOS DriverKit 版用户态驱动由 TinyGPU.app 提供；AMD 能跑是因为 tinygrad 自己写了用户态驱动**。

### 5.2 tinygrad AM 用户态驱动（RDNA3/RDNA4 一等公民）
- `docs/developer/am.md`："AM driver is a userspace driver targeting AMD's RDNA3/RDNA4. You only need tinygrad to send compute tasks to your GPU! **Make sure that amdgpu module is unloaded** and just run tinygrad with `DEV=AMD`."
- 即 tinygrad 对 AMD 的路径**根本不需要 amdgpu 内核模块**（可选 vfio-pci 处理 IRQ），直接用户态 ioctl/MMIO 驱动 MEC 计算队列与 SDMA。代码在 `tinygrad/runtime/ops_amd.py` + `ops_rdma.py`，RDNA4（gfx1200/gfx1201）有专属 TensorCore 定义（`codegen/opt/tc.py:108-112`）。
- 这意味着把 AMD dGPU 接到**任何能枚举 PCIe 的 ARM 板**上时，缺的只是一个暴露 MMIO/IRQ 的通道——正是 TinyGPU 在 macOS 上解决、usbgpu 在 Linux 上解决的问题（`extra/usbgpu/`，含 USB 远程 PCI 的 `APLRemotePCIDevice`）。

### 5.3 传统 amdgpu 路线在 ARM SBC 上的先例
- Raspberry Pi 5 / CM4 + AMD 卡（Polaris/RX 6000）：Ubuntu arm64 内核自带 amdgpu，免编译内核即可点亮并跑 Vulkan/compute；社区教程明确建议 "**Avoid Nvidia. The proprietary driver stack on ARM is a closed-source nightmare compared to the open amdgpu stack**"。
- 局限：ROCm 在 arm64 仍是实验性；但 tinygrad 的 AM/HIP 路径不依赖 ROCm 内核组件（HIP 编译器甚至可用 LLVM COMGR 替代，macOS 上原生运行）。

---

## 6. 对最终目标（2080 Ti → tinygrad → TinyGPU → 880M on C3）的影响评估

1. **tinygrad 层（已验证，见 research-tinygrad.md）**：CUDA 后端对 sm_75 零障碍；NV 用户态后端需给 Turing 打补丁（补 TURING_COMPUTE_A=0xC5C0 / TURING_CHANNEL_GPFIFO_A=0xC46F / TURING_DMA_COPY_A=0xC5B5 三个类常量 + 核对 QMD v3 位域）。
2. **TinyGPU 层**：TinyGPU.app 是 macOS DriverKit 应用，**不能装在 C3（AGNOS/Linux）上**；其价值在于证明"用户态 GPU 驱动 + 远程 PCIe"技术路线可行，且源码就在 tinygrad 主仓 `extra/usbgpu/tbgpu/installer/`（Swift app + DriverKit 扩展 `org.tinygrad.tinygpu.driver2` + server.c），架构可参考移植。
3. **C3 硬件层**：SDM845 + 4.9 下游内核 + DT 启动 → NVIDIA 官方/开源两条驱动路径均被平台要求挡死；即便移植成功，Adreno 630→2080 Ti 的收益也要扣掉 PCIe Gen3 x1（NVMe 槽）带宽与 8GB RAM 共享的约束。
4. **路线建议排序**：
   - A（最短路径）：**换 AMD RDNA3/RDNA4 卡**复用 tinygrad AM 用户态驱动 + usbgpu 思路接 C3；RDNA4 在 tinygrad 是一等公民且有 Apple Silicon 全链成功先例；
   - B：2080 Ti 先在任意 x86/aarch64-SBSA Linux（UEFI+ACPI）上用 CUDA 后端打通模型逻辑（零障碍），再评估 C3 移植成本；
   - C（高风险研究向）：给 AGNOS 4.9 内核 backport open-gpu-kernel-modules + tinygrad NV 后端 Turing 补丁，三处未知叠加，不建议作为关键路径。

## 附：主要证据源
- NVIDIA open-gpu-kernel-modules README（RTX 2080 Ti PCI ID 1E04/1E07 在列；"any Turing or later"；x86_64+aarch64）
- NVIDIA 数据中心驱动安装指南（R560 起开源内核模块为默认；开源模块仅支持 Turing+）
- NVIDIA 开发者论坛 [AARCH64] 帖（aarch64 驱动仅支持列出的数据中心 GPU/服务器 SoC）
- commaai/agnos-kernel-sdm845 master Makefile（Linux 4.9.103）；nelsonjchen 硬件报告（tici=tici D845 SOM/SDM845/Adreno 630/8GB）；comma.ai 官方博客（3X 同 845）
- docs.tinygrad.org/tinygpu（TinyGPU 要求 macOS 13+/USB4/AMD RDNA3+ 或 NVIDIA Ampere+）；NYU RITS/Tom's Hardware/Plugable 报道（2025-05 USB3 AMD 首秀 → 2025-10 M3 Max+NVIDIA → 2026-03 Apple 官方批准 → 2026-05 RX 9070/RTX 5070 实测）
- tinygrad docs/developer/am.md（AM 用户态驱动，无需 amdgpu 模块）；huuphan.com Pi+AMD 教程（amdgpu 在 arm64 SBC 的先例与"Avoid Nvidia"共识）
