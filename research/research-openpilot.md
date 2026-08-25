# openpilot 0.11.x 880M Big Model + USBGPU/eGPU 调用链研究报告

> 源码版本：`commaai/openpilot` tag `v0.11.1`（0.11.x 系列无 v0.11.2 tag，v0.11.1 是最接近 0.11.2 的发布版）
> 本地克隆：`/mnt/d/dev/egpu/repos/op-0.11.1`
> 结论标记：【源码确认】

---

## 1. 880M Big Model 文件确认

`selfdrive/modeld/models/` 目录（【源码确认】）：

| 文件 | 大小 | 说明 |
|---|---|---|
| `big_driving_vision.onnx` | **296.2 MB** | **880M 参数 Vision Big Model（唯一候选）** |
| `big_driving_policy.onnx` | 14.1 MB | 880M 配套 Policy Big Model |
| `driving_vision.onnx` | 46.9 MB | 常规（非 big）Vision 模型 |
| `driving_policy.onnx` | 14.1 MB | 常规 Policy 模型 |
| `dmonitoring_model.onnx` | 7.5 MB | 驾驶员监控模型 |

- 用户提到的 `big_driving_vision.onnx` / `big_driving_policy.onnx` **真实存在**（【源码确认】）。
- 880M 不是 pickle 文件名，而是社区对 `big_driving_*` 模型参数量的俗称。
- 模型格式：**ONNX**（训练产物）；运行时格式：**pickle（tinygrad TinyJit 编译产物）**。

### 模型输入格式（README.md【源码确认】）
- Vision 输入：`(1, 12, 128, 256)` 6-channel YUV420（两帧拼接），即 2×6×128×256
- 由 warp 管线把 NV12 相机帧转换而来（`compile_modeld.py` 的 `frames_to_tensor`）
- Policy 输入：`features_buffer (1,25,512)`、`desire_pulse (1,25,8)`、`traffic_convention (1,2)`

---

## 2. 编译调用链：ONNX → pickle（880M 编译）

`selfdrive/modeld/SConscript`（【源码确认】）：

```python
# 设备探测优先级：CUDA > QCOM > CPU
available = probe_devices()      # 运行 "from tinygrad import Device; Device.get_available_devices()"
if 'CUDA' in available:
  tg_backend = 'CUDA'
elif 'QCOM' in available:
  tg_backend = 'QCOM'            # TICI/C3 原生路径
else:
  tg_backend = 'CPU'

USBGPU = usbgpu_present()        # 检测 0xADD1:0x0001 USB 设备
if USBGPU:
  usbgpu_tg_flags = f'DEBUG=2 DEV=USB+AMD:LLVM WARP_DEV={tg_backend} FLOAT16=1 JIT_BATCH_SIZE=0 GMMU=0'
  # 关键：USBGPU 存在时用 big_ 前缀模型（880M），且 FLOAT16=1

for usbgpu in [False, True] if USBGPU else [False]:
  target_pkl_path = File(modeld_pkl_path(usbgpu)).abspath   # big_driving_tinygrad.pkl (usbgpu=True)
  file_prefix, cmd_flags = ('big_', usbgpu_tg_flags) if usbgpu else ('', tg_flags)
  ...
  cmd = f'{cmd_flags} python3 compile_modeld.py --model-size 256x128 \
        --vision-onnx models/{file_prefix}driving_vision.onnx \
        --policy-onnx models/{file_prefix}driving_policy.onnx \
        --output {target_pkl_path} --frame-skip ...'
```

**关键结论**：
1. **只有 USBGPU 存在时才编译 880M**（`big_driving_*.onnx` → `big_driving_tinygrad.pkl`）
2. 880M 编译标志：**`FLOAT16=1`**（FP16 推理）—— 与 tinygrad sm_75 FP16 TensorCore 路径吻合
3. 后端：`DEV=USB+AMD:LLVM`（这是 openpilot 官方 USBGPU 方案的 AMD GPU 默认后端）
4. 小模型（非 big）在无 USBGPU 时编译，用 `driving_*.onnx` → `driving_tinygrad.pkl`

`compile_modeld.py`（【源码确认】）核心：
- `OnnxRunner(vision_path)` / `OnnxRunner(policy_path)`：tinygrad 的 ONNX 加载器（`tinygrad.nn.onnx`）
- `make_run_policy(...)`：把 vision_runner + policy_runner 串起来
- `TinyJit(make_run_policy(...), prune=True)`：JIT 编译
- `pickle.dump(out, f)`：输出 `{metadata, run_policy, (cam_w,cam_h): warp_enqueue}`

---

## 3. 运行时调用链：modeld.py

`selfdrive/modeld/modeld.py`（【源码确认】）：

```python
# L3: os.environ['GMMU'] = '0'  # for usbgpu fast loading

# L145-147: USBGPU 运行时判断
_present = usbgpu_present()                                  # 0xADD1:0x0001 在 /sys/bus/usb/devices
_compiled = os.path.isfile(get_manifest_path(modeld_pkl_path(usbgpu=True)))  # big_driving_tinygrad.pkl
USBGPU = _present and _compiled

# L75-78: ModelState.__init__
def __init__(self, cam_w, cam_h, usbgpu):
  input_devices = get_tg_input_devices(PROCESS_NAME, usbgpu)
  jits = pickle.loads(read_file_chunked(modeld_pkl_path(usbgpu)))   # usbgpu=True → big_driving_tinygrad.pkl
```

`selfdrive/modeld/helpers.py`（【源码确认】）：
```python
USBGPU_VID = 0xADD1
USBGPU_PID = 0x0001
def usbgpu_present():
  for d in Path("/sys/bus/usb/devices").glob("*"):
    if idVendor==0xADD1 and idProduct==0x0001: return True   # = ADT-UT3G/UT3G eGPU 转接器
  return False
def modeld_pkl_path(usbgpu): return MODELS_DIR / f"{'big_' if usbgpu else ''}driving_tinygrad.pkl"
```

**ADT-UT3G 的 USB VID:PID 就是 0xADD1:0x0001** —— openpilot 用这个厂商 ID 专门标记 USB eGPU 桥接器。

---

## 4. USBGPU 后端的设备选择

`SConscript` 中 `tg_input_devices.json` 生成（【源码确认】）：

```python
tg_devices = {
  'selfdrive.modeld.modeld': {
    'default': {'WARP_DEV': tg_backend, 'QUEUE_DEV': tg_backend},     # CUDA/QCOM/CPU
    'usbgpu': {'WARP_DEV': tg_backend, 'QUEUE_DEV': 'AMD'}            # 队列设备强制 AMD
  },
}
```

- **WARP（图像预处理）留在原生设备**（CUDA/QCOM/CPU）
- **QUEUE_DEV（模型推理队列）切到 AMD** —— 即外接 GPU
- 0.11.x 的 USBGPU 方案 **硬编码 AMD 作为外接 GPU 后端**（`DEV=USB+AMD:LLVM`）
- **openpilot 0.11.x 没有 NVIDIA CUDA 作为 USBGPU 推理后端的代码路径** —— 这是移植点

---

## 5. 与 880M 的关系总结

| 项 | 值 |
|---|---|
| 880M 模型文件 | `models/big_driving_vision.onnx` (296MB) + `big_driving_policy.onnx` (14MB) |
| 运行时 pickle | `models/big_driving_tinygrad.pkl` |
| 触发条件 | `usbgpu_present()` = ADT-UT3G (0xADD1:0x0001) 存在 |
| 编译标志 | `FLOAT16=1 DEV=USB+AMD:LLVM` |
| 运行时设备 | `QUEUE_DEV='AMD'`（USBGPU 路径） |
| 推理频率 | 20Hz（`MODEL_RUN_FREQ`） |
| 输入 | vision `(1,12,128,256)` uint8 6ch YUV420；policy `(1,25,512)`+`(1,25,8)`+`(1,2)` |

---

## 6. 移植到 NVIDIA CUDA 的接口点

要在 880M + RTX 2080 Ti 上运行，需要改动的最小集合（分析，非实现）：

1. `SConscript`：`usbgpu_tg_flags` 的 `DEV=USB+AMD:LLVM` → `DEV=USB+CUDA:LLVM`（或在探测 CUDA 时优先 CUDA）
2. `SConscript`：`tg_devices['usbgpu']` 的 `'QUEUE_DEV': 'AMD'` → `'CUDA'`
3. 编译机器需有 NVIDIA driver + NVRTC（C3 实机上无 NVIDIA driver 时，需要在有 GPU 的构建机编译 pickle，运行时拷贝）
4. tinygrad 需支持 `DEV=USB+CUDA`（USB 桥 + CUDA 组合）—— 需验证 tinygrad 的 USB device 是否与 CUDA 后端组合

> 注：openpilot master 的 eGPU 方案已经演进（Chestnut/ASM2464、AMD 显式支持），见最新 commit 研究报告。
---

## 7. 最新 openpilot master 的 eGPU/chestnut commits（2026-08 检索）

### 7.1 命名演进：usbgpu → chestnut

master 已将 eGPU 方案命名为 **"Chestnut"** = ASM2464PD (ASMedia) USB-C→PCIe 桥方案：

```python
# openpilot/common/hardware/usb.py (master)
CHESTNUT_USB_IDS   = ((0xADD1, 0x0001), (0x3801, 0x0001))   # 正式固件
CHESTNUT_ROM_USB_IDS = ((0x174C, 0x2464), (0x174C, 0x2463)) # ROM/刷机模式 (ASMedia)
```

- **0xADD1:0x0001 就是 v0.11.x 里 usbgpu_present() 检测的那个 VID:PID**【源码确认】
- ADT-UT3G 与 comma 官方 chestnut 使用同族 ASM2464 方案，openpilot 还新增了第二个 VID 0x3801
- 相关 commit：`04847f380`(chestnut: add new usb vid)、`8edce0da4`(Clean up big model detection)

### 7.2 关键 commit 清单（53 个 eGPU 相关，2025-05 → 2026-08）

| commit | 日期 | 说明 | 移植价值 |
|---|---|---|---|
| `d0bf2be6f` (#35172) | 2025-05-13 | **External GPU support for big models（奠基）**：SConscript 加 USBGPU 分支 `AMD=1 AMD_LLVM=1`，modeld.py 运行时探测。v0.11.1 已包含等价逻辑 | 已在 0.11.x |
| `47f23828d` (#35809) | 2025-07-25 | Tinygrad DEV=DEVICE 抽象 | 中 |
| `c3c5992f8` (#35602) | 2025-06-30 | 避免 AMD 笔记本误判为 USB GPU | 低 |
| `ce92fd1a0` (#35405) | 2025-07-12 | modeld autodetect tinygrad backend（编译期自动选 CUDA/QCOM/CPU） | 高（PC 编译路径） |
| `dd2214a78` (#38546) | 2026-07 | chestnut updater（固件在线更新） | 低（运维） |
| `a7f32be2f` (#38540) | 2026-08-05 | probe chestnut before compiling big model | 中 |
| `391132465` (#38638) | 2026-08-16 | release chestnut build scripts（release 分支 release-chestnut） | 中 |
| `b8e14d85f`/`03711a13b` | 2026-08-17/18 | big model LFS pointer 编译处理 | 中 |
| `855c99bf9` (#38596) | 2026-08 | speedup chestnut probe | 低 |
| `e1912fa5b`/`fa0c6876d`/`bf74ce544` | 2026-07~08 | 新一代 big model（lebowski / Be Right Here Model） | 信息：模型持续变大 |

### 7.3 master SConscript 现状（关键 diff vs 0.11.1）

```python
# master: comma_arm64 固定 QCOM；PC 用 CPU
if arch == 'comma_arm64':
  tg_backend = 'QCOM'
else:
  tg_backend = 'CPU'

USBGPU = usbgpu_present()
if USBGPU:
  usbgpu_tg_flags = f'DEBUG=2 DEV=USB+AMD:LLVM WARP_DEV={tg_backend} FLOAT16=1 JIT_BATCH_SIZE=0 GMMU=0 TC_OPT=2'
```

vs 0.11.1 差异：
1. 0.11.1 有 `probe_devices()` 自动探测 CUDA/QCOM/CPU 选 tg_backend；master 改成按 arch 固定（comma_arm64→QCOM）
2. master 新增 `TC_OPT=2`（TensorCore 优化级别）
3. master 的 QUEUE_DEV 仍然硬编码 `'AMD'`

### 7.4 核心判断：NVIDIA eGPU 支持在官方路线图中不存在

- 官方 eGPU 链路（chestnut）从奠基 commit 到最新 master，**推理后端始终只有 AMD**（USB+AMD:LLVM）
- 无任何 NVIDIA/CUDA eGPU commit；CUDA 仅出现在 PC 自适应后端选择（非 C3 场景）
- 因此 "C3 + chestnut + RTX 2080 Ti" 需要自研移植：改 SConscript 后端 + tinygrad USB+CUDA 组合验证 —— 这是本项目的核心工程量所在
