# NVIDIA Turing 驱动与 ARM64 支持研究报告（Phases 9-11, 18-19）

> 目标硬件：**RTX 2080 Ti（TU102，PCI Device ID 1E04 / 1E07，sm_75）** ← **C3（SDM845 arm64，AGNOS，Linux 4.9 下游内核）**
> 方法：全部结论基于一手来源——NVIDIA 官方驱动档案（download.nvidia.com）、官方 `.run` 安装包实测解包、GitHub 官方仓库源码逐行核对、developer.download.nvidia.com CUDA 仓库实测。所有版本号为精确值。
> 本地克隆：`open-gpu-kernel-modules` 分支 580（tag `580.178.04`，commit `c8e699821c23e4335bf23330a54a23d15cfb95e9`），位于 `/tmp/opencode/nvidia-open`（下文引用行号均出自该克隆）。

---

## 1. TU102 的最低专有（proprietary）Linux 驱动版本

### 结论

| 产品 | PCI ID | 首个支持的 Linux 驱动 | 发布日期 | 分支 |
|---|---|---|---|---|
| **GeForce RTX 2080 Ti** | **1E04 / 1E07** | **410.57**（beta） | **2018-09-19** | R410 |
| Quadro RTX 6000 / 8000（TU102GL） | 1E30 / 1E78 等 | 410.73 | 2018-10-25 | R410 |
| Quadro RTX 4000（TU104GL） | 1EB1 | 410.78 | 2018-11-15 | R410 |
| （R396 分支最终版 396.54） | — | **不支持任何 Turing** | 2018-08-21 | R396 |

即：**R410 是第一个 Turing 分支；GeForce TU102 的确切首发版本是 410.57**。不存在"R415 数据中心先行"的情况——数据中心 Tesla T4 也是在 R410 内（410.78）加入的。

### 证据链（全部一手）

1. **410.57 官方支持芯片表**：https://download.nvidia.com/XFree86/Linux-x86_64/410.57/README/supportedchips.html 中明确列出 `1E04 → GeForce RTX 2080 Ti`、`1E07 → GeForce RTX 2080`（该页面至今仍可访问）。
2. **396.54 无任何 Turing**：https://download.nvidia.com/XFree86/Linux-x86_64/396.54/README/supportedchips.html 全文无 TU10x/RTX 条目（已抓取核对）。
3. **410.48 / 410.51 无法证实更早支持**：这两个版本的 README 目录在官方存档中已被移除（HTTP 404），Wayback Machine 对其 `supportedchips.html` 无任何快照（CDX 前缀查询为空）。因此"首个版本"只能锚定到现存最早的 410.57。
4. **发布日期**：`NVIDIA-Linux-x86_64-410.57.run` HTTP `Last-Modified: Wed, 19 Sep 2018 10:55:56 GMT`；Phoronix 当日报道《NVIDIA 410.57 Linux Beta Released With RTX 2080 Support》（2018-09-19）：*"Support for the GeForce RTX 2080 series is now in place"* ——措辞为"now in place"（新增），佐证此前版本不含 RTX 20 系列。https://www.phoronix.com/news/NVIDIA-410.57-Beta-Released
5. **同期旁证**：GamingOnLinux（2018-09-20）："Adds support for GeForce RTX 2080 Ti, GeForce RTX 2080 and some Tesla cards"。https://www.gamingonlinux.com/2018/09/nvidia-have-released-the-41057-driver-as-well-as-a-3965406-vulkan-beta-driver-to-help-dxvk/
6. **410 分支时间线**：Wayback 存档的官方 Unix 驱动页（2018-10-15 快照）仍列 Short-Lived 最新版为 396.54（410.57/410.60 为 beta 未上主表）；Phoronix 称 410.66（2018-10-16）是"first stable release in the 410 series"；Quadro RTX 5000/6000 于 410.73 加入（https://www.phoronix.com/news/NVIDIA-410.73-Linux-Driver ，2018-10-25）；Quadro RTX 4000 与 Tesla T4 于 410.78 加入（NVIDIA 官方驱动页 https://www.nvidia.com/en-us/drivers/details/140135/ ："Added support for the following GPUs: Quadro RTX 4000"；NVIDIA 开发者论坛官方公告 https://forums.developer.nvidia.com/t/linux-solaris-and-freebsd-driver-410-78/67462 ）。

---

## 2. 专有驱动的 aarch64 支持（非 Jetson）

### 2.1 安装包形态与获取渠道

- 命名：`NVIDIA-Linux-aarch64-<版本>.run`（安装横幅："NVIDIA Accelerated Graphics Driver for Linux-aarch64 580.65.06"，实测解包输出）。
- 公开存档：https://download.nvidia.com/XFree86/Linux-aarch64/ （目录索引现存最早为 **450.51**，2020 年）；数据中心渠道 `https://us.download.nvidia.com/tesla/<ver>/NVIDIA-Linux-aarch64-<ver>.run`（本次实测下载了 580.65.06，315,886,352 字节）。

### 2.2 官方 GPU 覆盖范围 —— 并非 datacenter-only

对 `NVIDIA-Linux-aarch64-580.65.06.run` 解包后读取其 `README.txt`：

- **Appendix A（Supported NVIDIA GPU Products）共约 720 个条目，其中 GeForce 系 472 条**，且明确包含：
  ```
  NVIDIA GeForce RTX 2080 Ti    1E04    J     (README.txt 行 9631)
  NVIDIA GeForce RTX 2080 Ti    1E07    J     (README.txt 行 9632)
  ```
  即 **RTX 2080 Ti 在通用 aarch64 驱动的官方支持列表内**。"aarch64 只支持数据中心卡"的说法不成立（历史上早期 aarch64 构建确实偏数据中心，但现行驱动明确覆盖 GeForce）。
- **主机 CPU 白名单（README.txt §2A，原文）**：
  > The NVIDIA aarch64 driver supports any supported discrete NVIDIA GPU connected to one of the following processors:
  > o NVIDIA Grace and later … o NVIDIA Tegra K1 (64-bit version) … o NVIDIA Tegra X1 or later … o AppliedMicro X-Gene … o Cavium ThunderX

  ⚠️ **Qualcomm SDM845 不在该清单中**。该清单明显陈旧（X-Gene/ThunderX 均为 2015-2016 年代平台），但它是目前唯一的官方文字表述——对 C3 项目而言这是"无官方背书"级别的风险点，而非硬性技术阻断（SBSA 标准要求的是 GICv3 ITS/SMMU 等，属 Phase 5-8 议题）。
- **最低软件要求（README.txt §2B 表格）**：Linux kernel **4.15 and newer**；glibc **2.17**；X.Org 1.14–21.1。

### 2.3 包内容实测（`--list` 输出摘录）

aarch64 `.run` 是**全功能包**（含图形栈），并非纯计算包：

| 内容 | 说明 |
|---|---|
| `libcuda.so.580.65.06`（96,164,704 B） | CUDA Driver API 库 ✅ |
| `libnvidia-ml.so.580.65.06`、`nvidia-smi` | 管理栈 |
| `firmware/gsp_tu10x.bin`（30,258,264 B）、`firmware/gsp_ga10x.bin` | GSP 固件随包分发 |
| `nvidia_drv.so`、`libglxserver_nvidia.so`、`libGLX_nvidia.so`、`libEGL_nvidia.so`、`nvidia_icd.json` | Xorg DDX/GLX/EGL/Vulkan 图形栈齐全 |

### 2.4 与 Jetson/Tegra L4T 的彻底区分

CUDA 官方文档将 ARM64 分为两类目标：**x86_64 / ARM64-SBSA / ARM64-Jetson**（《CUDA Installation Guide for Linux》v13.3，§1 Overview 及 Table 2，其中 Jetson 单列为 *"arm64 sbsa Jetson (dGPU + iGPU with OpenRM/nvgpu)"*）。Jetson 走独立的 L4T/JetPack 驱动栈（iGPU、统一内存、`nvgpu` 内核驱动），与 SBSA PCIe 独显的 `NVIDIA-Linux-aarch64` 驱动互不通用。本报告其余部分全部指 **SBSA 通用 ARM64**，与 Jetson 无关。

---

## 3. open-gpu-kernel-modules（GitHub 源码开放内核模块）

### 3a. 首个公开版本 —— 是 515.43.04，不是 510.39.01

- `git ls-remote --tags` 共 **216 个 tag，最早为 `515.43.04`，不存在任何 510.x tag**（任务假设"510.39.01 首发"经核实不成立）。
- 对应 x86_64 驱动 `NVIDIA-Linux-x86_64-515.43.04.run` 的 HTTP `Last-Modified: Fri, 29 Apr 2022 17:33:24 GMT`，即仓库公开发布于 2022 年 4-5 月（R515 发布期）。
- 该 tag 的 README（raw.githubusercontent.com/NVIDIA/open-gpu-kernel-modules/515.43.04/README.md）§Supported Target CPU Architectures 已写明 *"Currently, the kernel modules can be built for x86_64 or aarch64"*，并给出 `TARGET_ARCH=aarch64` 交叉编译示例。
- Turing 支持自始存在：当前分支 README.md §Compatible GPUs（L180-182）：*"The NVIDIA open kernel modules can be used on any Turing or later GPU"*；其支持芯片表含 `GeForce RTX 2080 Ti | 1E04`、`| 1E07`（L200-201）。515.43.04 时期的 x86_64 同版 supportedchips.html 也含 RTX 2080 Ti（https://download.nvidia.com/XFree86/Linux-x86_64/515.43.04/README/supportedchips.html ，已核对）。

### 3b. 最低宿主内核版本 —— 官方下限 4.15（AGNOS 4.9 不满足）

- README.md L73-77（原文）：
  > The NVIDIA open kernel modules support the same range of Linux kernel versions that are supported with the proprietary NVIDIA kernel modules. **This is currently Linux kernel 4.15 or newer.**
- 专有侧印证：580.65.06 x86_64 README minimumrequirements.html 同样写 *"Linux kernel 4.15 and newer"*。（历史对照：R410 时代最低为 2.6.9/nvidia-uvm 2.6.32；R525-R550 为 3.10；R570 起提升至 4.15——各版 minimumrequirements.html 实测。）
- **模块实际使用、且新于 Linux 4.9 的内核 API/conftest 检测点**（文件均为 `kernel-open/conftest.sh` 与所注路径）：

| # | API / 机制 | 引入内核 | conftest 测试名 | 位置 |
|---|---|---|---|---|
| 1 | `timer_setup()` | **v4.15** | 无回退——`nv_timer_setup()` 直接调用 | `common/inc/nv-timer.h` L45-49；另见 `kernel-open/nvidia/nv-nano-timer.c` L146-154（有 `hrtimer_setup`(v6.15+) 则用之，否则必须走 `timer_setup`） |
| 2 | `vm_fault_t` 类型 | v4.17 | `vm_fault_t` | conftest.sh L2547-2559 |
| 3 | `vmf_insert_pfn_prot()` | v4.20 | `vmf_insert_pfn_prot` | conftest.sh L1565-1576 |
| 4 | `struct proc_ops` | v5.6 | `proc_ops` | conftest.sh L2815-2825 |
| 5 | `pin_user_pages()` / `pin_user_pages_remote()`（FOLL_PIN） | v5.6 | `pin_user_pages` / `pin_user_pages_remote` | conftest.sh L1905-1972 / L1974-2080 |
| 6 | `struct mmu_interval_notifier`（interval-tree notifier，UVM/HMM 用） | v5.10 | `mmu_interval_notifier` | conftest.sh L4566-4579；使用于 `kernel-open/nvidia-uvm/uvm_hmm.c` 等 |
| 7 | `dma_resv_add_fence()` / `dma_resv_reserve_fences()` | v5.19 | `dma_resv_add_fence` / `dma_resv_reserve_fences` | conftest.sh L3884 起 |
| 8 | `follow_pte()` 首参改 VMA | v6.8 | `follow_pte_arg_vma` | conftest.sh L3311 起 |

  符号存在性探测机制本身见 conftest.sh L383-388（生成 `NV_IS_EXPORT_SYMBOL_PRESENT_<sym>` 宏）。
- **判定：AGNOS 的 4.9 内核低于官方 4.15 下限，open 模块按官方口径不可构建/不支持**；#1（timer_setup 无回退）是最直接的代码级体现。

### 3c. aarch64 支持的源码级证据

- 构建：README.md L27-32 明确 x86_64/aarch64 双架构 + `TARGET_ARCH=aarch64` 交叉编译变量（515.43.04 起即如此）。
- 条件编译：`NVCPU_AARCH64` 由 `kernel-open/common/inc/cpuopsys.h` L197 依据编译器 `__aarch64__` 定义；使用点包括：
  - `kernel-open/nvidia/nv-mmap.c` L203（arm64 mmap 属性处理）
  - `kernel-open/nvidia/nv-vm.c` L649（内存映射属性选择）
  - `kernel-open/nvidia/nv.c` L3260 / L3318 / L3391 / L3852 / L5361
  - `kernel-open/nvidia/nv-dma.c` L883（DMA 配置）
  - `kernel-open/nvidia/os-interface.c` L474/L544/L1094/L1250/L1324/L1365/L1439/L1596
  - `kernel-open/common/inc/nv-linux.h` L169（`CONFIG_SWIOTLB && NVCPU_AARCH64` 时包含 swiotlb 头）、L1159（`CONFIG_ARM64_4K_PAGES`）
- 用户态布局：aarch64 `.run` 安装 64 位用户库（libcuda/libnvidia-ml/GLX/EGL/Vulkan JSON 等，见 §2.3）；CUDA sbsa deb 安装至 `/usr/lib/aarch64-linux-gnu/`（libcuda）与 `/usr/local/cuda-*/targets/sbsa-linux/lib/`（toolkit 库），均为实测解包结果。

### 3d. GSP 固件（Turing = `gsp_tu10x.bin`）

- 芯片族映射：`kernel-open/common/inc/nv-firmware.h` —— `NV_FIRMWARE_CHIP_FAMILY_TU10X = 1`（L44-45）；TU10X/TU11X/GA100 → `"gsp_tu10x"`（L104-108）；GA10X/AD10X/GH100… → `"gsp_ga10x"`（L99-102）。
- 固件路径构造：`kernel-open/nvidia/nv.c` L27：
  ```c
  #define NV_FIRMWARE_FOR_NAME(name)  "nvidia/" NV_VERSION_STRING "/" name ".bin"
  ```
  即从 **`/lib/firmware/nvidia/<驱动版本号>/gsp_tu10x.bin`** 加载（`nv_get_firmware()` 注释原文 "// path is relative to /lib/firmware"，nv.c ≈L4057；实际 `request_firmware()` 调用在 nv.c L4064）。
- 模块内置声明：nv.c L29 `MODULE_FIRMWARE(...)` → `modinfo nvidia.ko | grep firmware:` 可见 `nvidia/580.xx.xx/gsp_tu10x.bin`。
- **必要性**：官方驱动手册 Chapter 45 "Open Linux Kernel Modules"（https://download.nvidia.com/XFree86/Linux-x86_64/580.178.04/README/kernel_open.html ）原文：
  > The open flavor of kernel modules supports Turing and later GPUs. The open kernel modules cannot support GPUs before Turing, because the open kernel modules depend on the GPU System Processor (GSP) first introduced in Turing.
  且 `src/nvidia/arch/nvalloc/unix/src/osapi.c` L4976-4984（`rm_set_rm_firmware_requested`）设置 `request_firmware = NV_TRUE; allow_fallback_to_monolithic_rm = NV_FALSE;` —— **open 模块没有 monolithic 回退，GSP 固件是硬依赖**。
- **宿主内核影响**：
  1. 固件经 `request_firmware()` 由内核向用户态请求 → 要求宿主 rootfs/initramfs 在模块初始化时能提供 `/lib/firmware/nvidia/<ver>/gsp_tu10x.bin`（AGNOS 这类定制系统需确认 firmware loader/udev 可用，或把固件打进 initramfs）；
  2. 固件体积 ~30 MB（580.65.06 的 `gsp_tu10x.bin` 为 30,258,264 字节，随 `.run` 分发，需复制到 `/lib/firmware`）；
  3. 同手册列出的 open 模块已知代价：GPU 初始化更慢、进出省电模式延迟更大等（kernel_open.html "Known Issues"）。

---

## 4. 用户态 CUDA 的 ARM64（非 Jetson）可用性

### 4.1 组件清单（arm64-sbsa 仓库实测）

数据源：`https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2204/sbsa/` 目录索引（2026-08 抓取，共 **8377 个 .deb**）：

| 组件 | 包名示例（arm64） | 状态 |
|---|---|---|
| Toolkit 元包 | `cuda-toolkit_13.3.1-1_arm64.deb`（另有 12.x/13.x 全系） | ✅ |
| Runtime API | `cuda-cudart-13-2_13.2.86-1_arm64.deb` → `targets/sbsa-linux/lib/libcudart.so.13.2.86` | ✅ 实测解包 |
| NVRTC | `cuda-nvrtc-13-2_13.2.86-1_arm64.deb` → `libnvrtc.so.13.2.86` | ✅ 实测解包 |
| nvcc / gdb / sanitizer / nsight / profiler | `cuda-nvcc-*`、`cuda-gdb-*`、`cuda-sanitizer-*`、`nsight-compute-*` 等 | ✅ |
| Driver API 头 | `cuda-driver-dev-*`（31 个版本） | ✅ |
| **libcuda.so（Driver API 运行库）** | **不在 toolkit 组件里，而在驱动包**：`libnvidia-compute_610.57.04-1ubuntu1_arm64.deb` → `/usr/lib/aarch64-linux-gnu/libcuda.so.610.57.04`（✅ 实测解包）；或 `NVIDIA-Linux-aarch64-*.run` 内的 `libcuda.so.580.65.06`（✅ 实测 `--list`） | ✅ |

**结论：aarch64 上存在 Jetson 之外的官方 libcuda.so**，来源只有驱动层（sbsa 驱动 deb 或 aarch64 `.run`），CUDA toolkit 仓库只提供 cudart/nvrtc 等上层组件——与 x86_64 的分层完全一致。

### 4.2 glibc 下限（实测 + 官方双证）

对三个关键库做符号版本实测（`strings | grep GLIBC_` 取最大值）：

| 库 | 版本 | 最高 GLIBC 符号要求 |
|---|---|---|
| `libcuda.so`（aarch64） | 610.57.04 | **GLIBC_2.17** |
| `libcudart.so`（sbsa-linux） | 13.2.86 | **GLIBC_2.17** |
| `libnvrtc.so`（sbsa-linux） | 13.2.86 | **GLIBC_2.17** |

官方口径一致：aarch64 驱动 README §2B 要求 glibc **2.17**；CUDA 13.3 文档验证过的 sbsa 发行版 glibc 最低为 RHEL8 的 2.28（《CUDA Installation Guide for Linux》Table 2 "Generic arm64 systems (sbsa)"）。

---

## 5. 分层判定表（通用 aarch64，非 Jetson）

| 层 | 通用 aarch64 是否存在 | 分发形态 | 最低版本（就 TU102 而言） | 关键约束 |
|---|---|---|---|---|
| **内核模块（专有）** | ✅ | `NVIDIA-Linux-aarch64-*.run` / sbsa 驱动 deb（`nvidia-kernel-source`、`nvidia-driver-580` 等） | **410.57**（2018-09-19）起支持 TU102 | 现代分支（R570+）要求宿主内核 ≥4.15；R410–R550 分支官方最低内核仅 2.6.9~3.10（410.57 README：kernel 2.6.9+，uvm 2.6.32+；525/535/550：3.10） |
| **内核模块（open）** | ✅（`TARGET_ARCH=aarch64`） | GitHub 源码 / `--no-kernel-modules` 配套 | **515.43.04**（首个公开版，2022-04/05）即支持 Turing | 官方最低内核 **4.15**（AGNOS 4.9 不满足）；强制 GSP 固件 `gsp_tu10x.bin` |
| **libcuda.so** | ✅ | 驱动层：aarch64 `.run` 或 sbsa `libnvidia-compute_*_arm64.deb` | 随驱动版本；符号要求 ≤GLIBC_2.17 | 非 CUDA toolkit 组件 |
| **libcudart** | ✅ | `cuda-cudart-<maj>-* _arm64.deb`（targets/sbsa-linux） | CUDA 10.0 起 Turing/sm_75 即受支持（CUDA 10 与 RTX 同日发布）；现行为 13.x | GLIBC_2.17（实测 13.2.86） |
| **NVRTC** | ✅ | `cuda-nvrtc-<maj>-* _arm64.deb` | 同上 | GLIBC_2.17（实测 13.2.86） |

### 对 C3（SDM845 + AGNOS 4.9）的直接含义

1. **每一层在"通用 aarch64"上都官方存在，且 RTX 2080 Ti 明确在列**（专有 aarch64 README Appendix A 含 1E04/1E07）——"ARM64 不支持 GeForce"类结论被一手证据否定。
2. 真正的硬约束有两个：
   - **宿主内核版本**：现代驱动（R570+，含全部 open 模块）要求 ≥4.15；若坚持 AGNOS 4.9 不动，官方支持的驱动窗口是 **R410.57 – R550 系列**（最低内核 2.6.9→3.10，同时完整支持 TU102），其中 R470/R535 为 LTS 分支，是 4.9 内核上的最优候选；
   - **官方主机 CPU 白名单未列 Qualcomm SoC**（README §2A 仅 Grace/Tegra/X-Gene/ThunderX），SDM845 属"无官方背书但无明文禁止"，可行性取决于 SMMU/MSI/BAR 等平台能力（Phase 5-8 议题）。
3. 若走 open 模块路线，除内核 ≥4.15 外还须部署 `/lib/firmware/nvidia/<ver>/gsp_tu10x.bin` 并保证 initramfs/udev 固件加载链路可用。

---

## 附：主要证据文件与复现命令

```bash
# TU102 支持表（一手）
curl -s https://download.nvidia.com/XFree86/Linux-x86_64/410.57/README/supportedchips.html | grep -o '1E04\|1E07'
# aarch64 驱动实测
curl -sO https://us.download.nvidia.com/tesla/580.65.06/NVIDIA-Linux-aarch64-580.65.06.run
sh NVIDIA-Linux-aarch64-580.65.06.run --list          # 文件清单（libcuda/gsp_tu10x.bin/图形栈）
sh NVIDIA-Linux-aarch64-580.65.06.run -x              # 解包读 README.txt §2A/§2B/Appendix A
# open 模块源码
git clone --depth 1 --branch 580 https://github.com/NVIDIA/open-gpu-kernel-modules.git
grep -n '"nvidia/" NV_VERSION_STRING' kernel-open/nvidia/nv.c
# CUDA sbsa 仓库
curl -s https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2204/sbsa/ | grep -oE "href='[^']*\.deb'"
```

本地留档：`/tmp/opencode/nvwork/`（aarch64 .run 及解包、410.57/396.54/515.43.04 supportedchips 快照、sbsa deb 清单、三个库的 glibc 实测对象）、`/tmp/opencode/nvidia-open`（580.178.04 源码克隆）。

*报告完成于 2026-08-25，由 nvidia-driver-arm64-researcher 产出。*
