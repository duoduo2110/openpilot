# my SP（onemiless/openpilot dev-sp-egpu）同步分析报告

> 克隆位置：`/mnt/d/dev/egpu/repos/mysp-dev-sp-egpu`
> 分支：`dev-sp-egpu`；HEAD = `4deff70`【源码确认】

---

## 1. Fork 链与基线

```
commaai/openpilot v0.11.1 (master 69e2c321e49760e52f7983eaa0a5f77cb95de637, 2026-06-02)
        ↓ sunnypilot 同步
sunnypilot 2026.002.000 (2026-06-28)
        ↓ onemiless 维护
my SP dev-sp-egpu (HEAD 4deff70, 2026-08-25)
```

- CHANGELOG.md 头部明确："sunnypilot Version 2026.002.000 ... Synced with commaai's openpilot (v0.11.1)"【源码确认】
- 注意：此基线比第一轮分析的 `sunnypilot/sunnypilot master`(25c2504) 略旧，但包含完整 chestnut/eGPU/880M 支持

## 2. C3XL Hardware Profile 架构（SP 特有）

- `hardware_profile` 根文件 = `c3xl`（profile 选择标记）
- `docs/adr/0002-isolate-c3xl-compatibility.md`：C3XL 兼容性通过 **Hardware Profile + Panda Startup 适配器**隔离，非全局设备类型硬编码；行为参照 `mr-one/openpilot:c3xl-dev`
- 关键代码：`openpilot/sunnypilot/hardware/profile.py`（get_hardware_profile）、`c3xl_probe.py`、`agnos.py`（AGNOS manifest 校验按 profile 分支）、`panda.py`

## 3. SP 特有 eGPU/GPU 相关 commit（dev-sp-egpu 分支）

| commit | 内容 | 意义 |
|---|---|---|
| `6b5219b` | **usbgpu: isolate UT3G dual runtime identity** | 支持自定义 **UT3G V1.6 双固件适配器 VID:PID = ADD1:0002**（manufacturer "tiny", product "*-UT3G-DUAL"），与官方 chestnut(ADD1:0001) 并存运行时身份；官方 flasher 域隔离（不会用 ed4e39b7 覆盖双固件）；ROM 态 174C:2463/2464 保持官方恢复行为 |
| `b4ea522` | build: tinygrad submodule 切到 **onemiless/tinygrad** 维护 fork | `.gitmodules` url 从 sunnypilot/tinygrad → onemiless/tinygrad |
| `4deff70` | modeld: big model 缺失时跳过 USBGPU 构建 | SConscript 条件化 |
| `1720958` / `3505688` | eGPU HUD 状态面板/UI | 只读状态模型 `openpilot/selfdrive/ui/egpu_status.py` |

## 4. tinygrad fork 的修改（onemiless/tinygrad @ 7de76ad7）

tinygrad submodule pin = `7de76ad7f409b0ee4bc375d39ea90c789f69c145`：
```
amd: recognize UT3G dual USB runtime ID
USB_AMD_IDS = ((0xADD1,0x0001), (0x3801,0x0001), (0xADD1,0x0002))
```
仅一处功能改动：`tinygrad/runtime/ops_amd.py` 把自定义双固件适配器加入 **AMD 后端** USB 枚举列表。**仍为 AMD-only，无任何 NVIDIA/CUDA 代码**。

## 5. NVIDIA/CUDA 现状判定

- 全树 grep nvidia/CUDA/cuda：仅 metadrive 模拟器引用，**无任何 NVIDIA eGPU 支持**
- `QUEUE_DEV='AMD'`、`DEV=USB+AMD:LLVM` 与上游 sunnypilot 一致
- **结论**：my SP 在 GPU 后端层面 = sunnypilot 原样 + UT3G 双固件传输层适配。要让 2080 Ti 跑起来，需新增：
  1. `SConscript`：usbgpu 分支增加 CUDA 后端变体（或探测到 NVIDIA 时切换）
  2. `tg_input_devices.json`：usbgpu 的 `QUEUE_DEV: 'CUDA'`
  3. tinygrad fork：无需改（CUDA 后端 sm_75 已在第一轮实机验证）
  4. 编译机需 NVRTC（已在 tt 验证可行）

## 6. 与本轮 NVIDIA 驱动链研究的衔接

my SP 已完成的部分（可直接复用）：
- ✅ UT3G 双固件传输层（ADD1:0002 运行时识别）
- ✅ eGPU 状态面板
- ✅ 880M big model 构建管线（supercombo）

缺失部分 = 本轮研究主题：
- ❌ NVIDIA kernel module（AGNOS 4.9 内核兼容性——编译实验进行中）
- ❌ aarch64 libcuda/NVRTC 用户态
- ❌ SConscript CUDA 后端分支

---

*报告完成时间：第二轮研究启动日。后续若 NVIDIA 内核链打通，Patch 落点即第 5 节清单。*
