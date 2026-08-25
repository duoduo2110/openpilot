# tinygrad CUDA 后端对 NVIDIA Turing (sm_75 / RTX 2080 Ti) 兼容性审计报告

> 审计对象：github.com/tinygrad/tinygrad @ commit `138fb4a783d82f4e877ad2fe3692aaf8d1de2e46`（2026-08-16，master）
> 本地克隆：`/tmp/opencode/tinygrad`（完整历史 14334 commits）
> 结论速览：**CUDA 后端（ops_cuda.py，走 libcuda）没有任何最低算力守卫，sm_75 可直接运行**；**NV 驱动级后端（ops_nv.py，绕过 CUDA 直连 ioctl）明确不支持 Turing**；**TinyGPU 是 tinygrad 官方的 macOS DriverKit eGPU 驱动应用，其 NV 路径要求 Ampere+，2080 Ti 需要打补丁**。

---

## 1. 两条 NVIDIA 后端：必须先分清

tinygrad master 有两条完全独立的 NVIDIA 路径（`docs/runtime.md:7` 与 CUDA 行）：

| 后端 | 文件 | 原理 | 官方支持范围 |
|---|---|---|---|
| **CUDA** | `tinygrad/runtime/ops_cuda.py` | 通过 libcuda（CUDA Driver API ctypes 绑定） | "NVIDIA GPU with CUDA support"（无架构限制） |
| **NV** | `tinygrad/runtime/ops_nv.py` | 用户态 ioctl 直连内核驱动（/dev/nvidiactl + UVM），完全绕过 libcuda | **"Ampere/Ada/Blackwell series GPUs"** |

`docs/runtime.md` 原文：
```
| [NV] ... | Ampere/Ada/Blackwell series GPUs.
| [CUDA] ... | NVIDIA GPU with CUDA support |
```

**RTX 2080 Ti 走 CUDA 后端开箱即用；走 NV 后端无法初始化（见 §4）。**

---

## 2. CUDA 后端：架构检测与编译链

### 2.1 架构检测（动态，无守卫）

`tinygrad/runtime/ops_cuda.py:106`：
```python
check(cuda.cuDeviceComputeCapability(ctypes.byref(major := ctypes.c_int()), ctypes.byref(minor := ctypes.c_int()), device_id))
```
`tinygrad/runtime/ops_cuda.py:121-122`：
```python
super().__init__(device, CUDAAllocator(self), [CUDARenderer, PTXRenderer, NVCCRenderer], CUDAProgram,
                 None if MOCKGPU else CUDAGraph, arch=f"sm_{major.value}{minor.value}")
```
- 2080 Ti 上 `major=7, minor=5` → `arch="sm_75"`，直接传给渲染器/编译器。
- **整个 `__init__` 中没有对 major/minor 的任何下限检查**——不存在 sm_80/sm_86 之类的排除逻辑。这是本次审计最关键的结论。

### 2.2 编译方式：默认 NVRTC 运行时编译，产出 PTX

渲染器选择在 `CUDARenderer.__init__`，`tinygrad/renderer/cstyle.py:400-405`：
```python
def __init__(self, target:Target, use_nvcc=False):
    super().__init__(target)
    from tinygrad.runtime.support.compiler_cuda import NVRTCCompiler, NVCCCompiler
    iface, dev, arch = target.interface, target.device, target.arch
    self.compiler = (NVCCCompiler if use_nvcc else NVRTCCompiler)(arch, ptx=iface.startswith("MOCK") or dev == "CUDA", cache_key=dev.lower())
    self.tensor_cores = tc.get_cuda(arch)
```

NVRTC 编译器 `tinygrad/runtime/support/compiler_cuda.py:44-57`：
```python
class NVRTCCompiler(Compiler):
  def __init__(self, arch:str, ptx=True, cache_key:str="cuda"):
    self.ptx, self.arch, self.compile_options = ptx, arch, [f'--gpu-architecture={arch}']
    ...
    if (nvrtcMajor.value, nvrtcMinor.value) >= (12, 4): self.compile_options.append("--minimal")
  def compile(self, src:str) -> bytes:
    nvrtc_check(nvrtc.nvrtcCreateProgram(...))
    nvrtc_check(nvrtc.nvrtcCompileProgram(prog, len(self.compile_options), ...), prog)
    data = _get_bytes(prog, nvrtc.nvrtcGetPTX if self.ptx else nvrtc.nvrtcGetCUBIN, ...)
```

- **默认路径：CUDA C 源码 → NVRTC（`--gpu-architecture=sm_75`）→ PTX 文本 → `cuModuleLoadData` 由驱动 JIT 成 SASS**。
  加载点 `tinygrad/runtime/ops_cuda.py:43-44`：
  ```python
  self.module = cuda.CUmodule()
  status = cuda.cuModuleLoadData(ctypes.byref(self.module), obj.lib)
  ```
- **nvcc 只是备选**：`NVCCRenderer`（cstyle.py:472-473）→ `NVCCCompiler`（compiler_cuda.py:60-71）调外部 `nvcc -arch=sm_75 -ptx/-cubin`。仅当显式选 `DEV=CUDA:NVCC` 渲染器时使用。macOS TinyGPU 的 NV 路径因无 NVRTC 才依赖 Docker 内 nvcc（见 §6）。
- PTX 版本号：MOCK/CUDA-PTX 接口用的文本替换编译器 `PTXCompiler`（compiler_cuda.py:73-79）：
  ```python
  return src.replace("TARGET", self.arch).replace("VERSION", "8.7" if (ver:=int(self.arch[3:]))>=120 else ("7.8" if ver>=89 else "7.5")).encode()
  ```
  即 **sm_75 对应 PTX ISA 7.5**（mma.sync m16n8k8 自 PTX ISA 7.0 引入，覆盖无忧）。真实硬件走 NVRTC 时由 NVRTC 自动选择匹配的 PTX 版本。

### 2.3 关键代码位置索引

| 内容 | 位置 |
|---|---|
| CUDADevice 初始化 / 算力查询 | `tinygrad/runtime/ops_cuda.py:97-122` |
| CUDAProgram（cuModuleLoadData / cuLaunchKernel） | `tinygrad/runtime/ops_cuda.py:37-65` |
| NVRTC/NVCC/PTX 编译器 | `tinygrad/runtime/support/compiler_cuda.py:44-92` |
| CUDARenderer（CUDA C 生成 + mma 内联汇编） | `tinygrad/renderer/cstyle.py:395-470` |
| PTXRenderer（纯 PTX 生成，NV 后端用） | `tinygrad/renderer/ptx.py:137-233` |
| dtype 支持守卫 | `tinygrad/renderer/cstyle.py:467-470`、`ptx.py:232-233` |
| TensorCore 按架构分派 | `tinygrad/codegen/opt/tc.py:94-98` |

---

## 3. dtype 支持矩阵（sm_75 视角）

### 3.1 硬性守卫（唯一一处）

`tinygrad/renderer/cstyle.py:467-470`（CUDARenderer.supported_dtypes）：
```python
def supported_dtypes(self):
  ver = int(self.target.arch[3:])
  return {d for d in super().supported_dtypes() if (d != dtypes.half or ver >= 53) and (d != dtypes.bfloat16 or ver >= 80)
          and (d not in dtypes.fp8_ocp or ver >= 89) and d not in dtypes.fp8_fnuz}
```
即：FP16 需 ≥53 ✓；**BF16 需 ≥80 ✗**；**FP8(OCP e4m3/e5m2) 需 ≥89 ✗**；fnuz 变体全平台排除。

### 3.2 不支持 ≠ 报错：自动软件模拟回退

`tinygrad/codegen/decomp/dtype.py:196-206`：
```python
def do_dtype_decomps(sink:UOp, ctx:tuple[set[DType], Renderer]) -> UOp:
  def _should_emulate(dt): return dt in EMULATED_DTYPES.tolist(dtypes) or dt not in ctx[1].supported_dtypes()
  ...
  for fr in sorted(filter(_should_emulate, ctx[0])):
    to = dtypes.int if fr == dtypes.long else dtypes.half if not _should_emulate(dtypes.half) and fr in dtypes.fp8s else dtypes.float
    if DEBUG >= 2: print(f"emulating {fr} as {to}")
```
- **BF16 on sm_75 → 按 f32 模拟**（位运算转换 + f32 计算，`pm_float_decomp`/`cast_float_to_bf16`，cstyle.py:92-103）；
- **FP8 on sm_75 → 按 half 模拟**（half 在 sm_75 受支持）。
- 结论：bf16 权重的模型（如多数 LLM）**能在 2080 Ti 上跑，但无原生 BF16 Tensor Core，速度显著慢于 FP16 路径**。最优做法是推理前把权重转 FP16。

### 3.3 完整矩阵

| dtype | sm_75 原生支持 | Tensor Core (WMMA/mma.sync) | 说明 |
|---|---|---|---|
| FP32 | ✅ | ❌（TF32 TC 仅 sm_80+） | 纯 ALU/CUDA core |
| FP16 | ✅（ver≥53） | ✅ **唯一有 TC 的类型** | 见 §3.4 |
| BF16 | ⚠️ 自动模拟为 f32 | ❌（TC 需 m16n8k16/bf16，sm_80+） | cstyle.py:469 守卫 + decomp 回退；无需补丁即可运行，想原生需大改 |
| TF32 | ❌（非存储类型） | ❌（`cuda_8168_tf32` 仅在 cuda_sm80 列表） | `docs/env_vars.md:41`："ALLOW_TF32 … on Ampere or newer GPUs"；`codegen/opt/postrange.py:231` 默认跳过 f32 输入 TC |
| FP8 (e4m3/e5m2) | ⚠️ 自动模拟为 half | ❌（需 sm_89+，tc.py:96 `cuda_81632_f8`） | cstyle.py:443 `#include <cuda_fp8.h>` 仅在支持时才走到 |
| INT8 | ✅ 普通 ALU | ❌ tinygrad 的 CUDA TC 列表根本没有 int8（AMD 才有 `(int8,int32)` TC，tc.py:107） | 硬件本身支持 m8n8k16 s8，但 tinygrad 未实现 |
| INT4 | — | — | **tinygrad 全库不存在 int4 dtype**（dtype.py:157 `all = floats + ints + (bool,)`），无从谈起 |

### 3.4 sm_75 的 Tensor Core：恰好是指定的那条指令

`tinygrad/codegen/opt/tc.py:94-98`：
```python
cuda_sm75: list[TensorCore] = cuda_8168_f16
cuda_sm80: list[TensorCore] = cuda_81616 + cuda_8168_f16 + cuda_8168_tf32
cuda_sm89: list[TensorCore] = cuda_sm80 + cuda_81632_f8

def get_cuda(arch): return cuda_sm89 if (ver:=int(arch[3:])) >= 89 else cuda_sm80 if ver >= 80 else cuda_sm75 if ver >= 75 else []
```
`cuda_8168_f16`（tc.py:87-90）：`dims=(8,16,8)`，输入 half、输出 f32/f16。渲染成内联 PTX（cstyle.py:456-463）：
```python
asm("mma.sync.aligned.m{M}n{N}k{K}.row.col.{dt_map_out[dtype_out]}.{dt_map_in[dtype_in]}.{dt_map_in[dtype_in]}.{dt_map_out[dtype_out]}" ...)
```
→ 即 **`mma.sync.aligned.m16n8k8.row.col.f32.f16.f16.f32`** —— 正是 Turing 原生支持的 WMMA 指令（任务书预期一致）。PTXRenderer 同样分派（ptx.py:147）：
```python
self.tensor_cores = PTXRenderer.tc_sm80 if (ver:=int(target.arch[3:])) >= 80 else tc.cuda_sm75 if ver >= 75 else []
```
且 ptx.py:148-149 对 ver<80 把 half 的 `MAX/EXP2` 上浮 f32（Turing SM 无对应 half 指令的规避），说明作者显式处理过 Turing 差异。

### 3.5 Turing 不兼容指令排查结论

- CUDA 后端生成的全部指令集 = CStyleLanguage 基础 ALU 映射 + `hsin/hexp2/hlog2/hsqrt/htrunc/hrcp`（cuda_fp16.h intrinsics，Kepler+ 可用）+ 上述 mma.sync。**未发现任何 Ampere+ 专属指令**（无 async copy、无 bf16 asm、无 tf32 asm、无 fp8 asm——它们都在 supported_dtypes/TC 分派层就被挡掉了）。
- 共享内存上限 `shared_max = 49152`（cstyle.py:398）= Turing 每 block 上限，匹配。

---

## 4. NV 驱动级后端：Turing 被硬编码排除（精确行号）

`tinygrad/runtime/ops_nv.py:439-442`（NVKIface.setup_usermode）：
```python
self.usermode_class:int = next(c for c in [nv_gpu.HOPPER_USERMODE_A, nv_gpu.TURING_USERMODE_A] if c in self.nvclasses)
self.gpfifo_class:int   = next(c for c in [nv_gpu.BLACKWELL_CHANNEL_GPFIFO_A, nv_gpu.AMPERE_CHANNEL_GPFIFO_A] if c in self.nvclasses)
self.compute_class:int  = next(c for c in [nv_gpu.BLACKWELL_COMPUTE_B, nv_gpu.ADA_COMPUTE_A, nv_gpu.AMPERE_COMPUTE_B] if c in self.nvclasses)
self.dma_class:int      = next(c for c in [nv_gpu.BLACKWELL_DMA_COPY_B, nv_gpu.AMPERE_DMA_COPY_B] if c in self.nvclasses)
```
autogen 定义（`tinygrad/runtime/autogen/nv_570.py`）：
```
TURING_USERMODE_A        = 0xC461   ← 在 usermode 回退列表里 ✓
TURING_CHANNEL_GPFIFO_A  = 0xC46F   ← 不在 gpfifo 列表 ✗（列表只有 AMPERE 0xC56F/BLACKWELL）
TURING_COMPUTE_A         = 0xC5C0   ← 不在 compute 列表 ✗
TURING_DMA_COPY_A        = 0xC5B5   ← 不在 dma 列表 ✗（且 Turing 只有 _A 变体）
```
Turing GPU 不暴露 AMPERE_COMPUTE_B 类 → `next()` 抛 StopIteration → **NVDevice 无法初始化**。这就是官方文档只写 "Ampere/Ada/Blackwell"、以及 GitHub issue 里 "turing is not supported" 的代码级根因。

若要给 NV 后端补 Turing（工作量评估）：
1. 三个类列表各加一个常量（机械改动）；
2. QMD 已有 pre-Blackwell 分支（ops_nv.py:297-301 `qmd_major_version:3`），但字段前缀硬编码 `NVC6C0_QMDV03_00`（Ampere 命名空间），Turing 为 `NVC4C0`，位域布局需逐一核对（ops_nv.py:51）；
3. `sass_version`/`sm_version` 解析（ops_nv.py:631-632）理论通用，需实测；
4. Blackwell 特判（cbuf0 index 223 等，ops_nv.py:284,292-293）不影响 Turing。

---

## 5. 内核生成管线（UOps → CUDAProgram）

完整链路（均给出落点）：

1. **AST → SINK/LINEAR**：`tinygrad/codegen/__init__.py:456-481`（`do_to_program`，含 BEAM 搜索入口）。
2. **LINEAR → SOURCE**：渲染器把 UOps 打成 CUDA C 字符串（`do_render`，__init__.py:450；CStyleLanguage._render，cstyle.py:200-254）。WMMA 节点在此展开成 `__WMMA_...` 内联函数（cstyle.py:456-463 生成的 mma.sync asm 包装）。
3. **SOURCE → BINARY**：`do_compile`（__init__.py:440-444）→ `NVRTCCompiler.compile_cached`（带磁盘缓存，device.py:306-311）→ **PTX bytes**。
4. **BINARY → TinyELF**：`tinygrad/uop/ops.py:1197` `TinyELF(self.src[3].arg, self.arg.function_name, self.arg.target, sig)`。
5. **TinyELF → CUDAProgram**：`ops_cuda.py:38-49`，`cuModuleLoadData` 让**驱动 JIT** PTX→SASS（2080 Ti 的驱动本就负责该架构，JIT 必然成功）；`cuFuncSetAttribute` 设置动态共享内存（L50）。
6. **启动**：`cuLaunchKernel`（ops_cuda.py:65），参数经 `encode_args`（L18-24）打包。

Turing 兼容性风险点审查结果：管线中唯一的架构相关分支就是 §2.2 的 `--gpu-architecture` 和 §3 的 dtype/TC 分派，**其余部分架构无关**。

## 6. git 历史 + CI + Issue 证据

### 6.1 Turing TC 支持的由来
commit `d5a646d49` **"CUDA Turing TC (#8597)"**（2025-01-14，co-author geohot）：
- 此前 `arch < 80 → tensor_cores = []`（无任何 TC）；
- 该 PR 加入 `tc_sm75 = tc_8168_f16`（m16n8k8 f16），并在 `.github/workflows/test.yml` 增加 CI 项。
diff 关键行（旧 → 新）：
```python
- self.tensor_cores, self.arch = CUDARenderer.tensor_cores if int(arch[3:]) >= 80 else [], arch
+ self.tensor_cores, self.arch = CUDARenderer.tc_sm80 if int(arch[3:]) >= 80 else CUDARenderer.tc_sm75 if int(arch[3:]) >= 75 else [], arch
```

### 6.2 CI 持续回归测试 sm_75
`.github/workflows/test.yml:108-117`（当前 master 仍在跑）：
```yaml
parallel -k --link --tagstring '[{1}]' '{2} python3 ./extra/gemm/simple_matmul.py' \
  ::: metal gfx950 gfx1100 ... sm_75 sm_80_half sm_80_tf32 \
  ::: ... 'DEV=PYTHON::sm_75 HALF=1' 'DEV=PYTHON::sm_80 HALF=1' 'DEV=PYTHON::sm_80 ALLOW_TF32=1'
```
`SHOULD_USE_TC=1` —— **sm_75 的 FP16 Tensor Core matmul 是每日 CI 用例**（CPU 模拟 PTX 语义执行）。`tinygrad/runtime/ops_python.py:218` 的映射表 `"CUDA_SM75":"sm_75"` 也印证这是一等公民目标。

### 6.3 GitHub Issues
- **#15413**（2026-03-22）"RTX 2080 Ti support for USB GPU on Mac mini M4"：用户在 Mac 上经 USB4 显卡坞用 2080 Ti 跑 `extra/usbgpu`（NV 后端）。维护者 nimlgen 2026-04-21 关闭并回复：**"turing is not supported"**。注意范围：这判的是 **NV/usbgpu/TinyGPU 这条驱动级路径**，不是 CUDA 后端。
- #9065 等 CUDA 报错 issue 均与架构无关（CUDAGraph 参数问题等）。
- 未发现任何 "CUDA 后端拒绝 sm_75" 的 issue——与代码结论一致。

---

## 7. TinyGPU 到底是什么（勿与 tinygrad / USBGPU 混淆）

**TinyGPU = tinygrad 官方（the tiny corp）发布的 macOS DriverKit 驱动应用**，让 Mac 通过 USB4/雷电外接 AMD/NVIDIA GPU 并由 tinygrad 直接驱动。不是 geohot 个人仓库，也不是 tinygrad 本体，更不是 USBGPU（usbgpu 是 Linux 侧的 USB PCIe 通道方案，extra/usbgpu/）。

- **发布仓库**：`https://github.com/tinygrad/tinygpu_releases`（仅放 release zip）。下载逻辑硬编码在 `tinygrad/runtime/support/system.py:414-425`：
  ```python
  APP_PATH = "/Applications/TinyGPU.app/Contents/MacOS/TinyGPU"
  commit = "c0d024f9ff0e1dc8fdf217f255da7101d91e8323"
  system(f"ditto -xk {fetch(f'https://github.com/tinygrad/tinygpu_releases/raw/{commit}/TinyGPU.zip', name=app_name)} /Applications")
  ```
- **工作方式**：TinyGPU.app 以 macOS 驱动扩展（DriverKit）接管 Thunderbolt/USB4 挂载的 PCIe 设备，并起一个用户态 server；tinygrad 通过 Unix socket 与之通信（system.py:427-437，`APLRemotePCIDevice`，socket 默认 `temp("tinygpu.sock")`），把 MMIO/内存操作 RPC 过去。安装入口 `extra/setup_tinygpu_osx.sh`。
- **与 openpilot 的关系**：openpilot vendor 了整个 tinygrad（`tinygrad_repo/`），其中带同一份 `docs/tinygpu.md`（如 hwh-kavin/openpilot sp-master260612 分支可见）。openpilot 的 880M 视觉模型经 tinygrad 的 modeld 编译运行；TinyGPU 是这条链在 **Mac + 外接 GPU** 上的硬件通道，不改变模型代码。
- **GPU 要求**（docs/tinygpu.md 原文）："A supported GPU (**AMD RDNA3+ or NVIDIA Ampere+**)"；编译器方面 NV 路径用 Docker 里的 arm64 nvcc 12.8 交叉出 cubin（`extra/setup_nvcc_osx.sh`），因为 macOS 没有 libcuda/NVRTC。
- **对 2080 Ti 的含义**：TinyGPU 的 NV 路径复用 ops_nv.py 的类选择逻辑 → **Turing 当场初始化失败（§4）**。要让 2080 Ti 走 TinyGPU，必须给 tinygrad 的 NV 后端打 Turing 补丁（加 TURING_COMPUTE_A/GPFIFO/DMA 三常量 + 核对 QMD v3 位域），且 TinyGPU.app 闭源二进制是否放行 Turing 设备 ID 还需实测。**这是整条 "2080 Ti → tinygrad → TinyGPU → 880M" 链路的最大技术风险点**；相比之下 tinygrad CUDA 后端本身对 sm_75 零障碍（Linux/Windows 有 NVIDIA 驱动的场景可直接用 CUDA 后端验证模型逻辑）。

---

## 8. 最终结论（对照任务目标）

1. **CUDA 后端编译方式**：默认 NVRTC 运行时编译出 PTX（`--gpu-architecture=sm_75`），`cuModuleLoadData` 驱动 JIT；nvcc 仅备选渲染器。（cstyle.py:404, compiler_cuda.py:44-71, ops_cuda.py:44）
2. **默认 PTX/compute 目标**：跟随设备实际算力动态生成 `sm_75`；无固定默认值；MOCK 路径 PTX ISA 7.5。（ops_cuda.py:106,122; compiler_cuda.py:78）
3. **是否存在排除 sm_75 的最低算力守卫**：**CUDA 后端没有**（唯一守卫是 dtype 级：BF16≥80、FP8≥89，cstyle.py:467-470）。**NV 驱动级后端有**——compute class 列表缺 TURING_COMPUTE_A，初始化即失败（ops_nv.py:441）。
4. **dtype 支持**：FP16✅(含 mma.sync.m16n8k8 TC)、FP32✅、INT8✅(ALU)；BF16/FP8 自动降级 f32/half 模拟可跑但慢；TF32 TC 与 INT8 TC、INT4 不存在/不支持。零补丁可运行；追求性能建议统一 FP16。
5. **Turing 不兼容指令**：未发现——所有 Ampere+ 专属能力在渲染前已被 dtype/TC 分派层剔除。
6. **TinyGPU**：tinygrad 官方 macOS DriverKit eGPU 应用（tinygrad/tinygpu_releases），包装的是 NV 驱动级后端，官方要求 Ampere+；openpilot 经 vendored tinygrad 使用它作为 Mac 外接 GPU 通道。**2080 Ti 需补丁才可能走通此路**。

### 附：环境事实备忘
- CUDA 13.x 起 sm_75（Turing）成为受支持的最低架构（Maxwell/Pascal/Volta 被移除），2080 Ti 处于现代 CUDA 支持地板之上，NVRTC/驱动链无 EOL 风险。
- tinygrad NV 后端 autogen 覆盖驱动 570/580/610（ops_nv.py:20,394-395）；NVIDIA 580 是 Maxwell/Pascal/Volta 的末代分支，Turing 不受影响。
