# C3 (SDM845) 内核升级路径 × AGNOS 依赖 × ION/dma-buf × 传输层澄清（round-2）

> 团队：c3-nvidia-chain ｜ 成员：agnos-upgrade-compat-researcher ｜ 2026-08-25
> 范围：Phase 15-17、22。**不**重复分析 NVIDIA 驱动内部（见 `research2-nvidia-driver.md`），**不**重复证明 AMD 可行。
> 核心问题：若 NVIDIA Turing 驱动无法在 4.9 上构建，**最小的、能保住 AGNOS + C3 硬件 + openpilot 的内核升级是什么？**

---

## 0. 结论速览（TL;DR）

1. **不存在"最小升级"路径。** 高通官方 BSP 对 SDM845 的支持**止步于 4.9**：msm-4.14/4.19/5.4 及更新的 `kernel.lnx.*` 分支均无 SDM845 平台支持（4.19 仅残留 `sdm845.dtsi`+`sdm845-mtp.dts`，无 defconfig、无 tici 板级）。而 NVIDIA open 模块官方下限是 **4.15**（队友报告 §3b 已证）——两者之间**没有交集**。
2. **AGNOS 用户态对 BSP 内核接口依赖极深**：`/dev/ion`（相机缓冲）、`/dev/kgsl-3d0`（Adreno GPU：UI + tinygrad QCOM 推理后端）、CamX 相机内核栈（`cam_sync`/`qcom_cam-req-mgr`）全部为 **BSP-only，主线从未合入**。换任何主线内核 = 三大件全断。
3. **ION→dma-buf 不是 NVIDIA eGPU 推理的阻塞项，二者正交。** tinygrad CUDA 后端分配器只用 `cuMemAlloc_v2`/`cuMemHostAlloc`（`ops_cuda.py` L72），QCOM 后端走 KGSL ioctl 也无 dmabuf；ION 只在 msgq 视觉缓冲（`visionbuf_ion.cc`）里用，与 GPU 间数据通路无关。
4. **传输层澄清（关键纠错）**：chestnut/usbgpu（ASM2464PD）在 comma 3/4 上**不是**"GPU 以普通 PCIe 设备枚举"。SDM845 无 USB4/TBT，tici 出厂内核甚至 `CONFIG_PCI=n`。实际机制是 **libusb 用户态 + ASM2464PD 固件的 PCIe TLP 引擎**——tinygrad `USBIface` 用 USB 控制传输读写 TLP、用桥片内 512KB SRAM 做 DMA 窗口，GPU 从未出现在 `/sys/bus/pci`。
5. **最终判定：2080 Ti 在 C3 上走 [普通 PCIe 枚举 + nvidia.ko + CUDA] —— 否（NO）。** 四重障碍：① 无物理 PCIe 走线到任何外部接口；② AGNOS 内核 `CONFIG_PCI=n`；③ 满足 NVIDIA ≥4.15 的 SDM845 内核不存在；④ tinygrad 的 NV 用户态后端（`ops_nv.py PCIIface`）要求真实 sysfs PCI 枚举且设备 ID 掩码不含 Turing（0x1Exx）。

---

## 1. Phase 15：内核升级可行性矩阵

### 1a. 高通 BSP（CodeLinaro `clo/la/kernel/*`）实测分支矩阵

以下为 2026-08-25 通过 CodeLinaro GitLab API 实测的分支存在性 + 关键文件 raw 探测结果：

| 内核 | 仓库 | 代表性 LA.UM 分支（实测存在） | SDM845 支持？ | 证据 |
|---|---|---|---|---|
| **4.9** | `clo/la/kernel/msm-4.9` | LA.UM.7.6.2.c27 / LA.UM.8.6.2.c31 / LA.UM.9.8.c26 / LA.UM.10.6.2.c26 … | ✅ **完整**（AGNOS 即基于此） | `arch/arm64/boot/dts/qcom/sdm845.dtsi` @ LA.UM.9.8.c26 → HTTP 200 |
| **4.14** | `clo/la/kernel/msm-4.14` | LA.UM.8.2.1.c27 / LA.UM.8.9.1.c25 / LA.UM.8.11.1.c1 / LA.UM.9.1.c25 / LA.UM.9.11.c25 | ❌ **无** | `arch/arm64/configs/vendor/` 仅 sm8150/sdmshrike/sdmsteppe/sdm660/qcs40x 等，**无 sdm845 defconfig**；`sdm845.dtsi` → HTTP 404 |
| **4.19** | `clo/la/kernel/msm-4.19` | LA.UM.8.12.c25 / LA.UM.9.15.c29 / LA.UM.11.2.1.c26 / LA.UM.12.2.1.c26 | ⚠️ **仅残留**（休眠代码） | `sdm845.dtsi` + `sdm845-mtp.dts` 存在（LA.UM.9.15.c29 实测 200），但 vendor defconfig 只有 kona(SM8250)/msm8937 系，**无 sdm845 defconfig、无 tici 板级、驱动未经 SoC 级验证** |
| **5.4** | `clo/la/kernel/msm-5.4` | LA.UM.9.14.1.c30 / LA.UM.9.16.c29 / LA.UM.10.9.1.r1 | ❌ **无** | vendor 配置仅 lahaina(SM8350)/kona(SM8250) GKI/QGKI 片段 |
| 5.10/5.15 | — | （高通未按此方式发布 sdm845） | ❌ | sdm845 从未进入 5.x BSP 产品线 |
| 6.1/6.12 | `clo/la/kernel/qcom` | kernel.lnx.6.1.r52-rel 等 | ❌ | 新平台专用（qclinux），无 sdm845 |

**结论 A**：高通 BSP 路线上 SDM845 是"4.9 一代"平台（同期 4.14 属 SM8150、4.19 属 SM8250、5.4 属 SM8350）。**想留在 BSP 就只能 4.9。**

### 1b. 主线 Linux 对 SDM845 的支持水位

- `torvalds/linux` master 的 `arch/arm64/boot/dts/qcom/sdm845.dtsi` 实测（raw 抓取）：含完整 CPU/OPP/cpufreq、`adsp_pas`/`cdsp_pas` remoteproc、**fastrpc compute-cb 节点（ADSP+CDSP）**、`apps_smmu` 全量引用、GCC/RPMH/interconnect、`pcie0_phy/pcie1_phy` 时钟引用——SoC 级 DT 相当完整。
- 社区状态页 linux-msm.github.io（SDM845/850）给出的主线化版本：

| 组件 | 主线版本 | 组件 | 主线版本 |
|---|---|---|---|
| Pinctrl/GCC | 4.18 | tsens（温度） | 4.19 |
| UART/I2C/SPI | 4.19 | UFS / eMMC | 5.1 |
| SMMU (MMU) | 5.1 | CPUfreq DVFS | 5.1 |
| CPUidle | 5.3 | interconnects | 5.7 |
| **PCIe** | **5.7** | ADSP/CDSP remoteproc | 5.2 |
| MDSP | 5.3 | **FastRPC** | 5.4 |
| WLAN (wcn3990) | 5.1 | Bluetooth | 4.19 |
| IPA | 5.7 | LMH 限频 | 5.16 |

- 社区 `sdm845-mainline/linux`（GitLab）持续维护到 6.18-dev，OnePlus 6/Pocophone 等手机可日常使用——**平台可用性没问题，问题在 AGNOS 用户态**（见 Phase 16）。

### 1c. 升级候选 × 破坏评估总表（核心交付）

NVIDIA 侧需求交叉引用 `research2-nvidia-driver.md` §3b：open 模块官方下限 **kernel ≥ 4.15**（`timer_setup()` v4.15 无回退等 conftest 证据）；Turing 支持自 515 开源版起即含 RTX 2080 Ti (1E04)。

| 候选内核 | BSP 平台支持 | binder/ashmem | ION (/dev/ion) | KGSL (/dev/kgsl-3d0) | fastrpc/adsprpc | CamX 相机栈 | thermal/watchdog | NVIDIA ≥4.15? | 总评 |
|---|---|---|---|---|---|---|---|---|---|
| **4.9 BSP（现状）** | ✅ 原生 | ✅（BSP binder） | ✅ ION_MSM | ✅ QCOM_KGSL | ✅ MSM_ADSPRPC | ✅ 原生 | ✅ QPNP/QCOM_WDT_V2 | ❌ 差 3 个小版本 | AGNOS 全功能；NVIDIA 不可构建 |
| **4.14 BSP** | ❌ 无 sdm845 | — | — | — | — | — | — | ✅≥4.15? 否(4.14<4.15) | **死路**：平台都没有，且仍低于 NVIDIA 下限 |
| **4.19 BSP** | ⚠️ 残留 dtsi，无 defconfig/tici | 理论可配 | 理论可配（4.19 尚有 ION 衍生） | ❌ KGSL 未移植到 4.19 sdm845 | 部分（4.19 有 adsp_rpc） | ❌ CamX 未移植 | 需重写 DTS | ✅ | **死路**：需自行把整个 sdm845 平台从 4.9 backport 到 4.19，工作量≈重做 BSP |
| **5.4 BSP** | ❌ 无 sdm845 | — | ❌（GKI 移除 legacy ION） | ❌ | ✅（fastrpc 已上游化） | ❌ | 需重写 | ✅ | 死路（同上且更远） |
| **5.10/5.15/6.1 LTS** | ❌（高通无 sdm845 产品线） | ✅ 主线 drivers/android | ❌ 主线只有 dma-buf heaps(5.6+) | ❌ KGSL 从未主线化 | ✅ drivers/misc/fastrpc.c | ❌ 主线 CAMSS 不覆盖 Spectra340+tici 传感器管线 | ✅ tsens/qcom-wdt | ✅ | 平台可行但 **AGNOS 三大件全断**（详见 §2） |
| **主线 6.x** | ✅（社区验证到 6.18） | ✅ binderfs(5.0+) | ❌ 同上 | ❌（替代：MSM DRM+Freedreno，但 tinygrad QCOM 后端不适用） | ✅（DT 节点齐备） | ❌ 最大阻塞项 | ✅ | ✅ | 同上；= "mainline 移植项目"而非"升级" |

**结论 B（回答核心问题）**：满足"NVIDIA 可构建(≥4.15) ∧ AGNOS+C3 硬件+openpilot 不坏"的内核**不存在现成选项**。最小代价路线其实是二选一：
- **留 4.9**：放弃 nvidia.ko 路线（转用户态/AMD，属其他成员结论域）；
- **上主线 6.x**：接受三大移植工程（msgq ION→dma-buf-heaps/memfd、UI→Freedreno、CamX→主线 CAMSS bring-up 或外挂 USB 摄像头），其中**相机是最硬骨头**。

---

## 2. Phase 16：AGNOS 用户态内核接口依赖审计

审计对象：`commaai/agnos-builder`（master tarball，2026-08-25）+ `commaai/agnos-kernel-sdm845`（master）+ 本地 sunnypilot/openpilot 克隆（commit `25c2504`）。

**基线事实**：
- AGNOS 内核 = **Linux 4.9.103**（`agnos-kernel-sdm845/Makefile: VERSION=4 PATCHLEVEL=9 SUBLEVEL=103`），defconfig=`tici_defconfig`（`agnos-builder/build_kernel.sh:4`），子模块指向 `../../commaai/agnos-kernel-sdm845.git`（`.gitmodules`）。
- AGNOS 用户态 = **Ubuntu 24.04**（`Dockerfile.agnos:7 FROM ubuntu:24.04`）+ systemd + 高通闭源库——**不是 Android 运行时**。

| # | 组件 | 内核接口 | tici_defconfig 证据 | 用户态消费者 | 4.9 BSP-only 还是主线也有？ |
|---|---|---|---|---|---|
| 1 | binder | `/dev/binder,hwbinder,vndbinder` | `CONFIG_ANDROID_BINDER_IPC=y`、`CONFIG_ANDROID_BINDER_DEVICES="binder,hwbinder,vndbinder"` | AGNOS 自带 libandroid/binder 桥接库；**openpilot 主流程不用 binder** | binder 本体主线有（drivers/android，binderfs 自 5.0）；**低风险** |
| 2 | ashmem | `/dev/ashmem` | `CONFIG_ASHMEM=y` | 少量 Android 兼容库 | 主线 staging 有但已弃用（memfd 替代）；**低风险** |
| 3 | **ION** | `/dev/ion` + `ION_IOC_ALLOC/SHARE/IMPORT/CACHE_SYNC` | `CONFIG_ION=y`、`CONFIG_ION_MSM=y` | **msgq visionipc**：`msgq_repo/msgq/visionipc/visionbuf_ion.cc` L46-115（相机视觉缓冲分配/共享/缓存同步全走 ION） | **BSP-only**。legacy ION 主线从未合入；主线替代为 dma-buf heaps(`/dev/dma_heap/*`, 5.6+)。**换内核必改 msgq** |
| 4 | **KGSL (Adreno)** | `/dev/kgsl-3d0` ioctl + `/sys/class/kgsl/kgsl-3d0/*` | `CONFIG_QCOM_KGSL=y`、`CONFIG_QCOM_KGSL_IOMMU=y`、`CONFIG_QCOM_ADRENO_DEFAULT_GOVERNOR="msm-adreno-tz"` | ① UI：`libEGL_adreno.so/libGLESv2_adreno.so/libllvm-qcom.so`（agnos-builder userspace/root/usr/lib/… 实测存在）经 kgsl 渲染；② **tinygrad QCOM 推理后端**：`tinygrad/runtime/ops_qcom.py:347 FileIOInterface('/dev/kgsl-3d0', os.O_RDWR)`，L374/415 写 idle_timer sysfs | **BSP-only，永不主线化**。主线替代 = msm DRM + Mesa Freedreno（a630 支持成熟），但 tinygrad 没有 MSM-DRM 后端 → **设备端 GPU 推理随内核切换直接死亡** |
| 5 | **fastrpc/adsprpc** | `/dev/adsprpc-smd`、`/dev/fastrpc-adsp`/`-cdsp` | `CONFIG_MSM_ADSPRPC=y` | agnos-builder root 内 `libsdsprpc.so/libmdsprpc.so/libcdsp_default_listener.so`；tinygrad `ops_dsp.py`（Hexagon DSP 后端） | 主线有 `drivers/misc/fastrpc.c`（5.4 起，sdm845.dtsi 含 compute-cb 节点），但 ioctl ABI 与 QuIC 用户态库兼容性需逐版验证；**中风险** |
| 6 | **CamX 相机内核栈** | `/dev/v4l-subdev*`、`/dev/v4l/by-path/platform-soc:qcom_cam-req-mgr-video-index0`、`platform-cam_sync-video-index0`、`media/cam_sync.h` ioctl | BSP camera-kernel（cam_req_mgr/cam_sync/ife/csid，非主线 CAMSS） | `system/camerad/cameras/spectra.cc` L180-187、L950（cam_sync_info）；camera_qcom2.cc 整条 IFE 管线。round-1 已确认 #33720/#33763 把图像处理下沉进 ISP 驱动——仍在 CamX 栈内 | **BSP-only**。主线 CAMSS 驱动不支持 Spectra 340 ISP 的该使用方式与 comma 传感器管线。**最大阻塞项** |
| 7 | thermal | `/sys/class/thermal/*`、QPNP 温度告警 | `CONFIG_THERMAL=y`、`CONFIG_THERMAL_GOV_USER_SPACE=y`、`CONFIG_THERMAL_QPNP=y` | openpilot thermald 读 zone 名并做限权；BSP zone 命名/触发点与主线 tsens 不同 | 主线 tsens 4.19+ 可用，但 **DT + 用户态阈值表要重写**；中风险 |
| 8 | watchdog | `/dev/watchdog` | `CONFIG_QCOM_WATCHDOG_V2=y`（BSP 专有；`# CONFIG_WATCHDOG is not set` 主线框架都没开） | systemd/agnos pet dog | 主线用 `qcom-wdt`（drivers/watchdog/qcom_wdt.c），节点/API 不同；**低-中风险** |
| 9 | udev 规则 | — | services.sh 反而 **mask 了 systemd-udevd**（userspace/services.sh L41-45） | 仅 `78-mm-comma.rules`（ModemManager，USB 串口） | udev 依赖面极窄；**低风险** |
| 10 | USB gadget/OTG | dwc3/configfs | `CONFIG_QCOM_GPI_DMA=y` 等 | adb/network gadget | dwc3 主线成熟；**低风险** |
| 11 | 显示 | MSM SDE DRM | `CONFIG_MSM_SDE_ROTATOR=y`、MSM DRM(BSP) | Qt/UI via Adreno EGL | 主线 msm DRM 支持 sdm845 DSI，但 tici 屏时序要新 DT；配合 #4 一起动；中风险 |
| 12 | 调制解调器 | qrtr/glink/mhi/pd-mapper | BSP IPC router | qmi/modemmanager（agnos-builder 自编译） | 主线 sdm845 modem 支持尚可（手机社区验证）；中风险 |

**结论 C**：AGNOS ≠ "随便换个内核"。真正绑死 BSP 的是 **#3 ION、#4 KGSL、#6 CamX** 三项；其余项主线均有对应物、属工程量而非原理性阻塞。

---

## 3. Phase 17：ION / DMA-BUF 是否阻塞 NVIDIA eGPU 推理？

### 3a. 设备端数据通路实况（源码证据）

- **相机→模型缓冲**：msgq visionipc 在 QCOM 构建下用 ION：`visionbuf_ion.cc` L46-57（`ion_allocation_data`，heap=`ION_IOMMU_HEAP_ID`，`ION_IOC_ALLOC`→`ION_IOC_SHARE` 得 fd）、L82（接收端 `ION_IOC_IMPORT`）、L109-115（cache sync ioctl 注释明确映射 DMA_FROM/TO_DEVICE）。这是 **CPU 侧帧分发**通道。
- **tinygrad QCOM 后端**：`ops_qcom.py` 打开 `/dev/kgsl-3d0` 直接 ioctl 分配/提交，**全文 0 处 dmabuf/ION 引用**（grep 实测）；autogen `kgsl.py` 中亦无 DMA_BUF 结构。
- **tinygrad CUDA 后端分配器**：`tinygrad/runtime/ops_cuda.py` `CUDAAllocator._alloc`（L67-72）＝ `cuMemAlloc_v2` / `cuMemHostAlloc`，`_copyin/_copyout`＝ `cuMemcpyHtoDAsync_v2`/`cuMemcpyDtoH_v2`（host staging 经 pinned memory），**无 dmabuf**。
- round-1 结论（引用）：#33720/#33763 已把图像处理移入 ISP 驱动，进一步减少用户态像素搬运——与 eGPU 无关。

### 3b. 判定

**ION→dma-buf 不是 NVIDIA eGPU 推理的阻塞项，二者正交。**
理由：CUDA 栈的数据入口是 host 内存（pinned copy-in）或 CUDA 自己的 device 内存；只要 openpilot 能把 VIPC 帧以普通用户态指针交给 tinygrad（现在就是这么做的），GPU 侧完全不需要知道帧当初是不是 ION 分配的。dma-buf 互操作（如零拷贝导入相机缓冲）属于**优化**，不是**可行性**前提。唯一受 ION 影响的场景是"换主线内核导致 /dev/ion 消失"，那打击的是**现有 QCOM 设备端管线**（§2 #3），而不是假想中的 NVIDIA 路径。

---

## 4. Phase 22：传输层澄清（TinyGPU vs chestnut/usbgpu vs 纯 CUDA PCIe）

### 4a. 三个概念，务必分开

| 名称 | 是什么 | 与 Linux C3 的关系 |
|---|---|---|
| **TinyGPU** (`tinygrad/tinygpu_releases`) | **macOS DriverKit** 宿主 app：在 macOS 上通过 DriverKit/USB 驱动 ASM2464 并跑 tinygrad AMD 后端 | **无关**。Linux C3 不用 DriverKit |
| **chestnut / usbgpu**（openpilot `system/hardware/chestnut/`，任务书写的 `extra/usbgpu/` 为旧路径/别名） | comma 的 USB-C 外接 GPU 产品线：ASM2464PD 桥 + 定制开源固件（源自 `tinygrad/asm2464pd-firmware`） | **就是本项目的相关物**。本地克隆已含全部源码（sunnypilot commit `25c2504`） |
| **纯 CUDA PCIe** | GPU 以 PCI 设备枚举 → nvidia.ko → CUDA | **在 C3 上不可达**，理由见 4c 判定 |

### 4b. chestnut 在内核层面到底发生了什么（源码逐条）

1. **识别与刷固件**：`system/hardware/chestnut/flash.py` —— ROM 态 VID/PID `(174c,2464)/(174c,2463)`（ASMedia 原厂 NVMe 桥），刷完变 `(add1,0001)`（"custom …-CLEAN"）；用 `USBDEVFS_CONTROL` EP0 控制传输发 0xF3/0xE4 消息，读 `0xB450` 判 LTSSM==0x78(L0) 确认**桥↔GPU 之间**的 PCIe link 已起来；必要时 unbind usb-storage（ROM 态会被 usb-storage 认成 U 盘）。
2. **系统监控面**：`common/hardware/usb.py` 定义 `CHESTNUT_USB_IDS=((0xADD1,0x0001),(0x3801,0x0001))`，只做 USB 设备枚举上报（chestnutPresent）。
3. **真正的 GPU 数据面在 tinygrad 用户态**（本地 tinygrad commit `138fb4a78`）：
   - `runtime/support/usb.py` `class USB3`：**libusb-1.0** 打开设备、claim interface 0、bulk 4MB 缓冲；若内核驱动占用则 `libusb_detach_kernel_driver`。**不需要任何自定义内核模块**。
   - `runtime/support/system.py:226 class USBPCIDevice(PCIDevice)`：`CustomASM24Controller(usb)` 包装固件协议；`System.pci_setup_usb_bars(self.usb, gpu_bus=4, mem_base=0x10000000, pref_mem_base=(32<<30))` —— **由软件扮演 PCIe 固件/根复合体**给 GPU 分配 BAR 地址；`read_config/write_config` = `usb.pcie_cfg_req(offset, bus=4, dev=0, fn=0)`（配置空间走 USB 控制传输）；BAR MMIO = `USBMMIOInterface`（即固件 0xF0 TLP 通道）；系统内存窗口 = 桥内 SRAM（`alloc_sysmem` 返回 `0xf000+off` 控制地址 ↔ `0x200000+off` GPU 可见地址）。
   - `runtime/ops_amd.py:910 class USBIface(PCIIface)`：设备发现就是 `USB3.list_devices(0xADD1,0x0001)+USB3.list_devices(0x3801,0x0001)`；DMA 区域映射到 SRAM（copy_buf 0xf000↔0x200000 size 0x80000、cq_buf 0xb800↔0x822000）——与 `asm2464pd-firmware/USBGPU.md` 公开的寄存器表（0xB210 PCIE_FMT_TYPE、0xB218 PCIE_ADDR、0xB254 PCIE_TRIGGER、0xB296 STATUS 等）一一对应。
   - `modeld.py` ChestnutState 里 `Device["AMD"].iface.pci_dev.usb.read(0xB450,1)` 直接读 LTSSM——再次印证 GPU 完全由用户态经 USB 驱动。
4. **ASM2464PD 本身需要什么内核模块？** 不需要专门的桥驱动。Linux 只把它当普通 USB 设备（ROM 态可能被 usb-storage 认领，用户态主动 detach/unbind，见 flash.py `unbind_drivers`）。PCIe 协议全部由**固件 + tinygrad 用户态**实现。

### 4c. "GPU 会作为普通 PCI 设备枚举吗？"——分宿主回答

- **USB4/TBT 宿主**（PC/Mac）：ASM2464PD 原生能力是 USB4 PCIe 隧道，GPU 可作为真 PCI 设备出现（comma 商店页："Connect chestnut to your computer and use it like any other USB4 dock"）。
- **comma 3/4 (SDM845)**：**不会**。SDM845 没有 USB4/Thunderbolt 控制器，Type-C 只是 USB3 数据；出厂内核干脆 `CONFIG_PCI=n`（`tici_defconfig` 实测，连 `CONFIG_PCI_MSM=n`）。因此 GPU **永远不出现在 `/sys/bus/pci`**，一切访问都是上面的 USB-TLP/SRAM 用户态通道。

### 4d. 最终判定：2080 Ti on C3 = [普通 PCIe 枚举 + nvidia.ko + CUDA]？

**否（NO）。** 源码级依据链：

1. **物理层**：tici 无任何对外 PCIe 走线/连接器；chestnut 在 tici 上走的是 USB 协议而非 PCIe 隧道（§4b/4c）。
2. **内核层**：AGNOS `CONFIG_PCI=n` → 即使有设备也无 PCI 总线扫描；nvidia.ko 是 PCI 驱动，无从绑定。
3. **版本层**：NVIDIA open 模块要求 ≥4.15（`research2-nvidia-driver.md` §3b），而 SDM845 BSP 止于 4.9（§1a）——不存在同时满足两边的现成内核。
4. **用户态层**：tinygrad 唯一的"USB GPU"后端是 AMD `USBIface`；NV 侧 `ops_nv.py:556 PCIIface` 要求真实 sysfs PCI 枚举（继承 `PCIIfaceBase`→`System.pci_probe_device`），且其设备 ID 掩码 `(0xff00,(0x2200,…,0x2f00))` **不含 Turing 0x1Exx**——即"照抄 AMD 思路给 NV 写个 USBIface"也没有现成代码，等于从零开发（还要解决 GSP 固件加载过 700MB/s SRAM 窗口的带宽问题）。

**现实路径排序**（供 lead 决策）：
- A. **AMD GPU + chestnut + tinygrad USBIface**：comma 官方已产品化（blog.comma.ai/chestnut，RX 9060 ready-to-drive $799）——唯一已被源码与产品双重验证的路；
- B. 2080 Ti on C3：无可行工程路径（除非自研 NV-over-USB 用户态后端，风险/工作量级别 ≈ 重写一个驱动栈）;
- C. "升级内核到 ≥4.15 再插 nvidia.ko"：被 §1a（无 BSP）+ §2（三大件断裂）双重否决。

---

## 5. 引用文件/提交清单

**本地克隆**
- `/mnt/d/dev/egpu/repos/sunnypilot`（sunnypilot，commit `25c2504`）：
  - `openpilot/system/hardware/chestnut/flash.py`（VID/PID、LTSSM 0xB450、usb-storage unbind）
  - `openpilot/common/hardware/usb.py`（CHESTNUT_USB_IDS）
  - `openpilot/selfdrive/modeld/modeld.py`（ChestnutState、`iface.pci_dev.usb`）
  - `openpilot/system/hardware/tici/agnos.json`（分区清单 xbl…boot/system）
  - `msgq_repo/msgq/visionipc/visionbuf_ion.cc`（ION_IOC_* 全套）
  - `openpilot/system/camerad/cameras/spectra.cc`（qcom_cam-req-mgr / cam_sync）
- `/mnt/d/dev/egpu/repos/tinygrad`（commit `138fb4a78`）：
  - `tinygrad/runtime/support/system.py`（PCIDevice sysfs 路径 L158-224；USBPCIDevice L226-240）
  - `tinygrad/runtime/support/usb.py`（USB3/libusb）
  - `tinygrad/runtime/ops_amd.py`（USBIface L910-933、is_usb L944）
  - `tinygrad/runtime/ops_nv.py`（PCIIface L556-573，ID 掩码无 Turing）
  - `tinygrad/runtime/ops_cuda.py`（CUDAAllocator L67-72，cuMemAlloc_v2）
  - `tinygrad/runtime/ops_qcom.py`（L347 /dev/kgsl-3d0；无 dmabuf）
- `/mnt/d/dev/egpu/research2-nvidia-driver.md`（队友：最低内核 4.15、timer_setup 证据）
- `/tmp/opencode/work/agnos-builder/`（commaai/agnos-builder master tarball）：`.gitmodules`、`build_kernel.sh`、`Dockerfile.agnos`、`userspace/{base_setup,services,openpilot_dependencies}.sh`、`userspace/root/usr/lib/aarch64-linux-gnu/`（adreno/fastrpc 闭源库清单）

**远程（2026-08-25 实测）**
- `raw.githubusercontent.com/commaai/agnos-kernel-sdm845/master/Makefile`（4.9.103）及 `arch/arm64/configs/tici_defconfig`（CONFIG_PCI=n、ION/KGSL/ADSPRPC/BINDER/QCOM_WATCHDOG_V2=y）
- `git.codelinaro.org` API：`clo/la/kernel/msm-{4.9,4.14,4.19,5.4}` 分支列表 + `arch/arm64/{configs/vendor,dts/qcom}` tree/raw 探测（正文 §1a 表）
- `raw.githubusercontent.com/torvalds/linux/master/arch/arm64/boot/dts/qcom/sdm845.dtsi`（fastrpc/adsp_pas/apps_smmu/pcie_phy 节点）
- `linux-msm.github.io/mainline-status/soc/sdm845`（组件主线化版本表）
- `gitlab.com/sdm845-mainline/linux`（分支至 6.18-dev）
- `github.com/tinygrad/asm2464pd-firmware` README + `USBGPU.md`（TLP 寄存器表、VID 174C:2464→ADD1:0001、SRAM 700MB/s）
- `blog.comma.ai/chestnut`、`comma.ai/shop/chestnut`（产品形态、RX 9060 kit）
