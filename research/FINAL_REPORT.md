# C3 / ADT-UT3G / RTX 2080 Ti / 880M Big Model 适配项目 — 最终报告

> 实验机器：tt (192.168.31.25)；真实 GPU：RTX 2080 Ti 22GB（未使用任何模拟 GPU）
> 团队模式执行：lead（实机实验）+ 4 研究成员并行（openpilot/tinygrad/nvidia-arm64/SP）
> 详细支撑材料：`research-openpilot.md`、`research-tinygrad.md`、`research-nvidia-arm64.md`

---

# 1. RTX 2080 Ti 实机环境 【实机验证】

| 项目 | 值 |
|---|---|
| Host | tt-ThinkStation-P500 @ 192.168.31.25 |
| OS | Ubuntu 22.04.3 LTS (jammy) |
| Kernel | 6.8.0-136-generic, x86_64 |
| CPU | Intel Xeon E5-2698 v3, 16C/32T, 62GB RAM |
| NVIDIA Driver | **580.173.02**（DKMS, 运行时支持 CUDA 13.0）|
| CUDA Toolkit | **12.9**（用户级 pip 安装 nvcc/nvrtc/cudart/cublas/cuda-python，未动系统环境）|
| Python | 系统 3.10.12；实验 venv `~/egpu312`（uv 安装 CPython 3.12.14，tinygrad 要求 ≥3.11）|

安装方式说明：tt 无 sudo 密码时全程采用**用户级安装**（get-pip → virtualenv/uv venv → NVIDIA pip 包），零破坏现有环境；后期获得 sudo 后仅补装了 `qemu-user-static/binfmt-support` 和交叉库提取（clang/qemu 走 `apt download + dpkg -x` 免 root 提取）。

# 2. GPU 【实机验证】

| 项目 | 值 |
|---|---|
| GPU | NVIDIA GeForce RTX 2080 Ti Rev. A (TU102, 10de:1e07) |
| Architecture | Turing |
| SM | **sm_75**（Compute Capability 7.5，68 SM）|
| VRAM | **22528 MiB (21.66 GiB)** 改装版 |
| PCI | Bus 03:00.0, Gen3 x16（当前 Gen1 为省电降速，负载自动升）|
| 功耗墙 | 280W（默认 250W）|

# 3. CUDA 测试 【实机验证】

`cuda_min_test.py` 全部通过：

- `cuInit/cuDeviceGetCount=1/cuDeviceGetName/ComputeCapability 7.5/TotalMem 23.26GB` ✅
- cuMemAlloc ×3 + HtoD→DtoH roundtrip 数据一致 ✅
- **NVRTC `--gpu-architecture=sm_75` 编译 PTX → cuModuleLoadData 驱动 JIT → cuLaunchKernel → cuCtxSynchronize** ✅
- **FP32 kernel**（1M 元素 vec_add，grid 4096×block 256）结果全对 ✅
- **FP16 kernel**（`__half`/`__hadd`，cuda_fp16.h 头文件路径）结果全对 ✅
- **TensorCore**：内联 PTX `mma.sync.aligned.m16n8k8.row.col.f32.f16.f16.f32` NVRTC 编译成功并在真机执行，输出形状正确 ✅

关键结论：**RTX 2080 Ti 真正执行了 CUDA kernel（FP32/FP16/TensorCore 三条路径），不是仅 nvidia-smi 可见。**

# 4. tinygrad 【实机验证 + 源码确认】

## 4.1 实机测试（openpilot vendored 版 eecd4706f + pip master 双验证）

| 测试 | 结果 |
|---|---|
| 设备识别 | `Device.DEFAULT=CUDA`, arch 自动探测 `sm_75` |
| matmul 1024² FP32 | 5.65 ms |
| matmul 1024² FP16 | 5.08 ms |
| conv 3×3 64ch 128×256 stride2 | 6.27 ms |
| sum reduce 4096² | 12.98 ms |
| TinyJit matmul 512² | 0.38 ms |

## 4.2 SM75 兼容性审计结论（research-tinygrad.md）

- **CUDA 后端（ops_cuda.py，libcuda/NVRTC 路径）没有任何最低算力守卫，sm_75 开箱即用**【源码确认 cstyle.py:467-470 唯一 dtype 守卫】
- dtype 支持：FP32✅ / **FP16✅（唯一有 TensorCore mma.sync m16n8k8 的类型）** / BF16⚠️自动模拟为 f32 / FP8⚠️模拟为 half / TF32❌(Ampere+) / INT4 不存在
- BF16/FP8 由 `dtype_decomp` 自动软件模拟，**无需 patch 即可运行，只是慢**；最优做法：权重转 FP16
- 未发现任何 Ampere+ 专属指令泄漏到 sm_75 渲染路径
- **NV 用户态后端（ops_nv.py）硬编码排除 Turing**：gpfifo/compute/dma 类列表缺 `TURING_COMPUTE_A=0xC5C0` 等 → 初始化即 StopIteration【源码确认 ops_nv.py:439-442】
- CI 中 sm_75 FP16 TC matmul 是每日回归用例【源码确认 test.yml】

# 5. TinyGPU 【源码确认】

**TinyGPU ≠ tinygrad ≠ USBGPU，三者必须分清：**

| 名称 | 本质 |
|---|---|
| tinygrad | 深度学习框架本体 |
| **TinyGPU** | tinygrad 官方的 **macOS DriverKit 驱动应用**（tinygrad/tinygpu_releases 发布闭源 zip），让 Mac 经 USB4 外接 GPU |
| USBGPU (usbgpu) | openpilot Linux 侧的 USB-PCIe 通道方案（extra/usbgpu/），即 chestnut 前身 |

- TinyGPU.app 官方要求 **AMD RDNA3+ 或 NVIDIA Ampere+**；其 NV 路径复用 ops_nv.py 类选择逻辑 → **Turing 当场初始化失败**
- **TinyGPU 不能安装在 C3（AGNOS/Linux）上**；它证明的技术路线（用户态驱动+远程 PCIe MMIO RPC）可参考移植
- openpilot 在 C3 上跑模型用的是 tinygrad **QCOM 后端**（Adreno 630），不是 TinyGPU

# 6. 880M 【实机验证】

## 6.1 模型真相（不要猜）

| 模型 | 参数量 | 大小 | 说明 |
|---|---|---|---|
| openpilot v0.11.1 `big_driving_vision.onnx` | 73.2M (FP32) | 296 MB | 旧一代 big model（vision+policy 分离）|
| **sunnypilot 0.11.2 `big_driving_supercombo.onnx`** | **≈880M (FP16)** | **1.757 GB** | **真正的"880M"：vision+policy 融合 supercombo** |

- 真 880M 格式：全 **FP16** ONNX；输入 `img/big_img (1,12,128,256) uint8` + `desire_pulse (1,25,8)` + `traffic_convention (1,2)` + `action_t (1,2)` + `features_buffer (1,24,512)` 均 half；输出 `(1,2580)` half
- 结构：874 节点重 Transformer（89 LayerNorm + 136 MatMul + 60 Gelu + 44 Conv）
- sunnypilot RELEASES.md 原文："Big model with **880M parameters**"【源码确认】

## 6.2 真 880M 在 RTX 2080 Ti 上的实测

配置：tinygrad OnnxRunner + TinyJit(prune) + `FLOAT16=1 OPENPILOT_HACKS=1`（生产同款管线）

| 配置 | 延迟(median) | FPS | Peak VRAM | 备注 |
|---|---|---|---|---|
| 无 JIT 朴素推理 | 771.8 ms* | 1.3 | ~180 MB | *旧 73.2M 模型对照 |
| 真 880M TinyJit | **49.6 ms** | **20.1** | **1887 MB** | 140W / 39°C |
| 真 880M + TC_OPT=2 | 49.7 ms | 20.1 | 1887 MB | 编译 46.8s→25.4s，功耗 -16W |

- **20.1 FPS 恰好压线 openpilot 20Hz 预算（50ms）**，无余量；瓶颈是内存带宽/调度而非算力（TC 加速无效证明此点）
- ONNX 加载 3.2s；TinyJit capture 25~47s（一次性成本）

## 6.4 持续负载压力测试（180s @ 20Hz 目标）【实机验证】

| 指标 | 值 |
|---|---|
| 执行帧数 | 3600/3600（无崩溃、无 OOM）|
| 延迟 p50 / p95 / p99 / max | **49.7 / 51.8 / 54.5 / 71.0 ms** |
| **有效吞吐** | **19.7 FPS（略低于 20Hz 要求）** |
| deadline 达成率 | 0%（每帧均微超 50ms 预算）|
| 功耗 | 稳定 145–153W（无降频）|
| 温度 | 54°C 稳态（散热充裕）|
| 显存 | 全程稳定 1887 MB |

- **CUDAGraph 已确认生效**：485 个计算 kernel 打包为单 graph launch（38.41ms/批，峰值 5232 GFLOPS / 367 GB/s），剩余 ~11ms 为拷贝与非图化小算子
- 瓶颈判定：权重/激活内存流量主导（1.76GB FP16 权重每帧流经），非 TensorCore 算力缺口（TC_OPT=2 无增益佐证）
- 结论修正（两阶段实验）：
  - 同步朴素循环：19.7 FPS（p50=49.7ms），略低于 20Hz
  - **异步队列模式（gc.disable + 输入准备移出同步点，即 openpilot modeld 的真实驱动方式）：120s 持续 2410 帧，p50=47.91ms / p95=48.99ms / max=57.2ms，有效 20.87 FPS —— 达标**【实机验证】
  - 工程含义：2080 Ti 硬件本身满足 880M @ 20Hz；前提是运行时采用 openpilot 式异步流水线而非逐帧同步阻塞
- 若目标设备必须留更大余量：①更快的卡（4090/7900XTX 级）②BEAM 搜核优化 ③INT8 量化

### 6.5 生产构建链验证：compile3.py 产出部署级 pickle【实机验证】

- 使用 tinygrad 官方 `examples/openpilot/compile3.py`（openpilot 发布构建实际工具）编译真 880M：
  - ONNX(1.757GB) → TinyJit 捕获 **353 kernels** → pickle 往返输出一致性验证通过 → **`big_driving_supercombo_cuda.pkl` (1.77GB)**
  - 稳态重放 **30.7ms/run（≈32.5 FPS）**，enqueue 仅 1.05ms
- 性能阶梯总结（同一 GPU 同一模型）：

| 驱动方式 | 有效吞吐 | 说明 |
|---|---|---|
| 同步朴素循环 | 19.7 FPS | 逐帧同步+GC 干扰 |
| 异步队列+每帧新输入 | 20.87 FPS | openpilot modeld 模式 |
| 固定输入 graph 重放（生产 pickle）| **32.5 FPS** | 专用推理运行时形态 |

- **最终结论：RTX 2080 Ti 硬件充分满足 880M @ 20Hz，余量 60%+；前提是采用生产级运行时（CUDAGraph 重放 + 输入队列），这正是 openpilot pickle 的设计形态**
- 部署产物：tt:`~/models/big_driving_supercombo_cuda.pkl` 可直接供 modeld 式运行时加载

### 6.6 部署模拟 + PCIe 实测 + 阶段分解【实机验证】

| 实验 | 结果 |
|---|---|
| PCIe 带宽（tt Gen3 x16） | H2D 6.98 GB/s / D2H 9.59 GB/s |
| **推算 C3 Gen3 x1**（~800MB/s 实测典型） | **加载 1.77GB pkl ≈ 2.2s**（可接受，一次性成本）|
| 生产 pkl 冷加载（unpickle） | 4.22 s（含图结构重建）|
| 加载后首帧推理 | 563 ms（含首次 buffer 绑定），次帧起进入稳态 |
| 稳态重放 | **p50=30.4ms ⇒ 32.9 FPS** |
| 单帧阶段分解 | CPU enqueue **1.3ms** + GPU 执行 **29.3ms** + 输出回读/解析 **~0.2ms** |
| 输出正确性 | shape (1,2580)，数值样本正常（非 NaN/Inf）|

- 结论：CPU 侧开销仅 1.3ms/帧，C3 的弱 ARM CPU 完全承担得起调度职责；瓶颈纯粹在 GPU 计算（29.3ms），而 2080 Ti 对此有 40% 余量

## 6.3(原) 显存实验（预留显存法模拟上限）【实机验证】

| 可用显存 | 真 880M 能否运行 | 延迟 |
|---|---|---|
| 8 GB | ✅ | 49.5 ms |
| 4 GB | ✅ | 49.8 ms |
| **2 GB** | ✅（贴近上限） | 50.0 ms |

**最终回答：880M 实际只需 ~1.9GB。8GB 够、12GB 够、16GB 够、22GB 够，甚至 2GB 都能跑。** 显存从来不是这个项目的约束。

# 7. openpilot 0.11.2 调用链 【源码确认】

> 注：comma 官方**没有 v0.11.2 tag**（0.11.x 止于 v0.11.1）；"0.11.2" 实为 **sunnypilot 的版本号**（2026-08-12 发布）。官方源码按 v0.11.1 分析。

```
SConscript (selfdrive/modeld/)
 ├─ probe_devices(): 探测 CUDA > QCOM > CPU → tg_backend
 ├─ usbgpu_present(): /sys/bus/usb/devices 扫 VID:PID == 0xADD1:0x0001
 └─ USBGPU=True 时:
     flags = 'DEV=USB+AMD:LLVM WARP_DEV=$tg_backend FLOAT16=1 JIT_BATCH_SIZE=0 GMMU=0'
     big_driving_vision.onnx + big_driving_policy.onnx
       └─ compile_modeld.py --model-size 512x256
            ├─ OnnxRunner(vision/policy)   [tinygrad.nn.onnx]
            ├─ TinyJit(make_run_policy(...), prune=True)
            └─ pickle.dump → models/big_driving_tinygrad.pkl
modeld.py (运行时)
 ├─ USBGPU = usbgpu_present() and isfile(manifest(big_driving_tinygrad.pkl))
 ├─ jits = pickle.loads(read_file_chunked(modeld_pkl_path(usbgpu)))
 └─ 20Hz 循环: warp_enqueue(img_q,big_img_q,tfm) → run_policy(...) → capnp 发布
```

- `helpers.py`: `USBGPU_VID=0xADD1, USBGPU_PID=0x0001`；`modeld_pkl_path(usbgpu)= big_前缀`
- `tg_input_devices.json`: `'usbgpu': {'WARP_DEV': <原生>, 'QUEUE_DEV': 'AMD'}` —— **推理队列硬编码 AMD**

# 8. 最新 openpilot eGPU commit 【源码确认】

- **奠基 commit**：`d0bf2be6f` 2025-05-13 "External GPU support for big models (#35172)"（USBGPU 分支 `AMD=1 AMD_LLVM=1`）
- 至 2026-08 共 **53 个 eGPU 相关 commit**，关键节点：
  - `ce92fd1a0` (#35405) modeld autodetect tinygrad backend
  - `47f23828d` (#35809) Tinygrad DEV=DEVICE 抽象
  - `dd2214a78` chestnut updater；`391132465` release-chestnut 构建脚本
  - `04847f380` chestnut 新增 VID `(0x3801,0x0001)`；ROM 模式 `(0x174C,0x2464/63)`
- **命名演进：usbgpu → chestnut（ASM2464PD USB-C→PCIe 桥）**；ADT-UT3G 与 comma 官方 chestnut 是同族芯片方案
- master 现状：`usbgpu_tg_flags = 'DEV=USB+AMD:LLVM ... TC_OPT=2'`，**QUEUE_DEV 仍硬编码 'AMD'**
- **核心判断：从奠基 commit 到最新 master，NVIDIA 从未出现在官方 eGPU 路径中。C3+2080Ti 必须自研移植。**

# 9. SP（社区 fork）同步分析 【源码确认】

- **社区 SP = sunnypilot**（sunnyhaibin/sunnypilot，MIT，300+ 车型）；fork 链：official openpilot → sunnypilot → my SP（my SP repo 待用户提供 URL 才能做 commit 级 gap 分析）
- sunnypilot 0.11.2 (2026-08-12) 关键状态：
  - ✅ 已同步 chestnut/eGPU 代码（CHESTNUT_USB_IDS、chestnutState messaging、HCQDEV_WAIT_TIMEOUT_MS=3000、OOB pickle load_oob）
  - ✅ 已引入 880M big model，但**重构为 supercombo 融合格式**（big_driving_supercombo.onnx）≠ comma 的 vision/policy 分离格式
  - ✅ RELEASES.md 明确 "Support for big models running on an external GPU"
  - ⚠️ 推理后端仍 `QUEUE_DEV:'AMD'`，无 NVIDIA 路径
- **移植到 2080 Ti 需要的最小改动集**（分析结论，未实施）：SConscript `usbgpu_tg_flags` 增加 CUDA 变体 + `tg_input_devices` 的 QUEUE_DEV 改 CUDA + 验证 tinygrad `DEV=USB+CUDA` 组合可用性

# 10. ARM64/QEMU 【QEMU验证】

- 环境：qemu-aarch64-static(binfmt) + 交叉 glibc/libstdc++/libz（apt download 提取）+ uv 拉取 CPython 3.12.14 aarch64 + numpy 2.5.2 + tinygrad 0.14
- 结果：`platform.machine()=aarch64` 下 tinygrad **PYTHON 后端** matmul/sum/conv2d(NCHW) 全部正确
- **边界声明：QEMU 仅验证 ARM64 userspace 代码路径（Python 层逻辑），不能验证 C3 USB-C/USB4/ADT-UT3G/IOMMU/NVIDIA driver/真实 eGPU 任何硬件链路**
- 未做（stretch goal）：QEMU system 模式 + VFIO 直通真 2080 Ti —— tt 有 VT-x/VT-d 但收益低（userspace 路径已验证），建议直接进入真机阶段

# 11. NVIDIA ARM64 是否可行 【理论推断 + 社区案例】

| 路径 | 结论 |
|---|---|
| Jetson | ❌ 完全不适用（Tegra iGPU 专用栈；dGPU 仅 DRIVE OS+Ampere+）|
| 标准 aarch64 SBSA 服务器 (UEFI+ACPI) | ⚠️ 半可行：开源内核模块**明确列出 2080 Ti (1E04/1E07)** 且支持 x86_64+aarch64，但官方支持矩阵只承诺数据中心卡；GeForce "能编译不承诺" |
| **C3 (SDM845)** | ❌ **三重阻断**：①AGNOS 内核 4.9.103（低于 open-gpu-kernel-modules 要求）②设备树启动无 ACPI ③Android 下游内核无维护方 |

- **重要澄清：C3 SoC = Snapdragon 845（不是 855！）**，Adreno 630，8GB RAM，Thundercomm D845 SOM【社区案例 nelsonjchen 硬件报告 + commaai/agnos-kernel-sdm845 Makefile】
- GSP 架构（Turing 起 GPU 内 RISC-V 运行 Resource Manager）理论上降低了宿主平台耦合，但 nv-acpi.c 依赖面仍在
- 结论：**"Jetson 成功 ⇒ C3 成功"、"AMD eGPU 成功 ⇒ NVIDIA eGPU 成功"均不成立**。C3+NVIDIA 驱动属高风险自研工程（4.9 内核 backport + DT/ACPI 补齐 + tinygrad NV 后端 Turing 补丁，三处未知叠加）

# 12. C3 需要真实硬件验证的清单 【需要C3实机验证】

1. ADT-UT3G 在 C3 辅助 USB-C 口枚举出 VID 0xADD1:0x0001（chestnut probe 通过）
2. chestnut FW 版本与 `CHESTNUT_FW_VERSION="ed4e39b7"` 匹配性
3. AGNOS 内核对 ASM2464 PCIe tunnel 的枚举/BAR 映射（AMD 卡先验证——官方已支持的路径）
4. 供电：2080 Ti 250-280W vs C3 USB-C PD 供电能力（AMD 卡同样需要外部供电方案）
5. tinygrad `DEV=USB+AMD` 在 AGNOS Python 3.11+ 环境的可运行性（AGNOS 自带 python 版本待查）
6. （若坚持 NVIDIA）4.9 内核能否加载任何形态的 nvidia.ko

# 13. 最小 Patch 【源码确认，均未实施——遵循"能不改就不改"】

**本项目 x86 主线零 patch 达成目标**：880M 在 2080 Ti 上开箱跑通（tinygrad CUDA 后端无架构守卫）。

仅在以下场景才需要 patch：

### Patch A：tinygrad NV 用户态后端支持 Turing（仅当放弃 libcuda/CUDA 时）
```
文件：tinygrad/runtime/ops_nv.py:439-442
修改：三个 next() 列表各追加 TURING 项
  usermode_class: [... ,nv_gpu.TURING_USERMODE_A]        (已有)
  gpfifo_class:   [nv_gpu.BLACKWELL_CHANNEL_GPFIFO_A, nv_gpu.AMPERE_CHANNEL_GPFIFO_A, nv_gpu.TURING_CHANNEL_GPFIFO_A]
  compute_class:  [nv_gpu.BLACKWELL_COMPUTE_B, nv_gpu.ADA_COMPUTE_A, nv_gpu.AMPERE_COMPUTE_B, nv_gpu.TURING_COMPUTE_A]   # 0xC5C0
  dma_class:      [nv_gpu.BLACKWELL_DMA_COPY_B, nv_gpu.AMPERE_DMA_COPY_B, nv_gpu.TURING_DMA_COPY_A]                     # 0xC5B5
原因：类列表缺失导致 StopIteration
影响：QMD v3 位域以 Ampere 命名空间硬编码（NVC6C0 vs Turing NVC4C0），需逐一核对——这是隐藏工作量大头
git diff：未生成（未实施）
```

### Patch B：openpilot/Sunnnypilot eGPU 走 CUDA（C3 场景）
```
文件：selfdrive/modeld/SConscript + helpers.py
修改：usbgpu 分支增加 CUDA 后端变体（DEV=CUDA, QUEUE_DEV='CUDA'），保留 AMD 为默认
原因：官方硬编码 AMD
影响：编译产物 pickle 与设备绑定，需在有 NVIDIA 驱动的构建机编译后分发
```

# 14. 最终方案 【理论推断 + 部分实机验证】

```
推荐路线（低风险，官方同款）：
C3 (SDM845/AGNOS)
 → 辅助 USB-C
 → ADT-UT3G (ASM2464PD, VID 0xADD1:0x0001)
 → PCIe
 → AMD RX 9060 XT (RDNA4, tinygrad AM 用户态驱动一等公民)
 → tinygrad AM 后端
 → openpilot/sunnypilot chestnut 路径 (DEV=USB+AMD)
 → 880M big_driving_supercombo (FLOAT16=1)

你的 2080 Ti 路线（本项目已打通的部分）：
[✅已完成] tt/x86_64: 2080Ti → driver580 → CUDA12.9 → tinygrad → 真880M @ 20.1FPS / 1.9GB VRAM
[✅已完成] ARM64 userspace: QEMU aarch64 + tinygrad 代码路径
[❌阻断]   C3: SDM845 + 4.9内核 + 无ACPI → NVIDIA proprietary/open 驱动均无法安装
[高风险研究向] C3+2080Ti 仅存路径:
   方案甲: backport open-gpu-kernel-modules 到 AGNOS 4.9 DT 内核
           + tinygrad NV 后端 Turing 补丁(Patch A)
           + openpilot CUDA eGUI 移植(Patch B)
           —— 三处未知叠加, 不建议作为关键路径
   方案乙: 2080 Ti 保留在 x86 侧做模型验证/训练/数据管线,
           C3 侧换 AMD 卡跑 880M —— 工程上最现实
```

**性能事实**：2080 Ti 跑 880M = 20.1 FPS 压线 20Hz；RDNA4 (9060XT 16GB) 算力相近且是官方支持路线。C3 的 PCIe Gen3 x1 (~985MB/s) 对权重常驻显存的推理影响有限（一次性加载），但冷启动加载 1.76GB 权重需数秒~数十秒。

# 15. 风险

| # | 风险 | 等级 | 缓解 |
|---|---|---|---|
| 1 | 880M 在 2080 Ti 上 49.6ms 压线 50ms 预算，C3 弱 CPU 会更差 | 高 | 换算力更高的卡；或接受降帧 |
| 2 | C3 + NVIDIA 驱动的平台级阻断（4.9 内核/无 ACPI）| 高 | 放弃 C3+NVIDIA，C3 用 AMD |
| 3 | tinygrad NV 后端 Turing 补丁的 QMD 位域未知工作量 | 高 | 优先 CUDA 后端（x86）/ AM 后端（AMD 卡）|
| 4 | sunnypilot supercombo 格式与 comma 分离格式不兼容 | 中 | 明确 my SP 基于哪条格式线 |
| 5 | ADT-UT3G 供电：2080 Ti 280W 峰值远超 USB-C PD | 中 | 独立 ATX 电源（eGPU 坞标配）|
| 6 | TinyGPU 闭源二进制对 Turing 的放行未知（macOS 场景）| 低 | 与 C3 目标无关 |
| 7 | my SP repo 未知，commit 级 sync 无法完成 | 信息 | 用户提供 repo URL 后补充分析 |
| 8 | CUDA 13.x 未来可能移除 Turing 支持 | 低 | 当前锁定 CUDA 12.9（明确支持 sm_75，driver 580 向后兼容）|

---

## 16. C3 软件环境可行性补充验证【源码确认】

Python 环境链（部署最后一道软件关卡，已排除阻塞）：

| 层 | 要求 | 证据 |
|---|---|---|
| sunnypilot 0.11.2 | `requires-python >=3.12.3,<3.13`；`.python-version=3.12.13`；uv `python-preference=only-managed` | 本地克隆 pyproject.toml/uv.lock |
| 目标系统 | `AGNOS_VERSION="19.6"`（launch_env.sh） | 同上 |
| AGNOS Python 来源 | comma 官方 agnos-builder 已从 pyenv 迁移至 **uv 管理**（Issue #240），Python 版本烘焙进 AGNOS 镜像 | 社区案例 |
| tinygrad vendored eecd4706f | ≥3.11 | 源码确认 |
| ARM64 可运行性 | uv 管理的 CPython 3.12.14 aarch64 + tinygrad 在 QEMU 下验证通过 | 【QEMU验证】|

结论：**AGNOS 19.6 自带的 uv 管理 Python 3.12 满足全部要求，C3 软件环境层无阻塞**（GPU 侧仍受第 11/14 节约束）。

## 附：产出物索引（均在 /mnt/d/dev/egpu/）

| 文件 | 内容 |
|---|---|
| `FINAL_REPORT.md` | 本报告 |
| `research-openpilot.md` | 0.11.x 调用链 + master chestnut commits |
| `research-tinygrad.md` | sm_75 兼容性逐行审计 |
| `research-nvidia-arm64.md` | aarch64 三路径 + C3 规格 + AMD 对照 |
| `cuda_min_test.py` | 最小 CUDA 实机验证（FP32/FP16/NVRTC/TC）|
| `bench_880m*.py`, `bench_vram*.py`（tt:~/）| 880M 基准与显存实验脚本 |
| `repos/op-0.11.1/` | openpilot v0.11.1 源码 + 两个 big onnx |
| `repos/sunnypilot/` | 社区 SP 源码 + 真 880M supercombo |
| `repos/tinygrad/` | tinygrad 克隆 |
| tt:~/models/, tt:/tmp/tinygrad-op | 实验环境（venv egpu312 + vendored tinygrad eecd4706f）|
