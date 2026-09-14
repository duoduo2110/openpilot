# onemiless/openpilot — `c3-dev-sp-egpu`（真实 Comma 3 适配分支）

> 本分支在 **`onemiless/openpilot:dev-sp-egpu`（C3XL + UT3G/AMD eGPU）** 基线上，
> 新增**官方真实 Comma 3（C3 / TICI）**硬件适配，同时完整保留 C3XL 现有行为。
>
> 基线提交：`2a69709` · 本分支当前头：`156a3fb` · 更新日期：2026-09-14

---

## 📦 原仓库信息（Upstream / 来源链）

| 层级 | 仓库 · 分支 | 说明 |
| :--- | :--- | :--- |
| 原始项目 | [commaai/openpilot](https://github.com/commaai/openpilot) | comma.ai 官方开源辅助驾驶系统（MIT License） |
| 上层框架 | [sunnyhaibin/sunnypilot](https://github.com/sunnyhaibin/sunnypilot) | sunnypilot 主仓库；本分支的功能框架与代码基来源 |
| **直接基线（父仓库）** | [onemiless/openpilot](https://github.com/onemiless/openpilot) · `dev-sp-egpu` | C3XL 第三方硬件 + UT3G/AMD eGPU 远端推理基座 |
| **本分支** | [duoduo2110/openpilot](https://github.com/duoduo2110/openpilot) · `c3-dev-sp-egpu` | 在基线上叠加真实 Comma 3 适配 |
| C3XL 行为参考 | `mr-one/openpilot` · `c3xl-dev` | C3XL 已知良好部署行为参照 |
| C3 行为参照 | `GuoJiafeng/openpilot` · `dev` / `sp260428tici` | 真实 C3 运行基线参照（仅用于对比） |

> ⚠️ 本分支仅面向 **Comma 嵌入式车机硬件（Qualcomm SDM845 + AGNOS）**，
> 与任何 PC / 模拟器运行版本**完全无关**。

---

## 🎯 本分支定位

在同一份代码内，通过 **HardwareProfile（硬件画像）** 干净隔离两套硬件，互不破坏：

| 维度 | 真实 Comma 3（`STANDARD`） | C3XL（`C3XL`） |
| :--- | :--- | :--- |
| 设备树 model | `comma tici` | `comma tici`（同名，需显式区分） |
| 内部 Panda | 单路 SPI **DOS**（STM32F4，type `0x07`） | 单路 SPI Tres 兼容固件（STM32H7） |
| 驾驶员监控相机 | 有 → `dmonitoringmodeld` 启动 | 无 → 禁用视觉 DM |
| 音频 | 板载 I2S 功放（MAX98357A）→ `soundd` | GPIO 42 蜂鸣器 → `alert_output` |
| 自动下电 | 支持（离车自动休眠） | 禁用（仅手动强制关机） |
| 行车录像 | 正常（`encoderd` 录制 + rlog） | 精简（关闭路视频/rlog，转 `local_diagnosticsd`） |

**画像归属原则**：C3XL **只能显式指定**，不再由设备树 `comma tici` 推断；其余一律默认真实 C3。

---

## 🔧 C3 适配改动点（本分支新增）

### 1. 硬件画像解耦 — `openpilot/sunnypilot/hardware/profile.py`
- ❌ 移除 `return HardwareProfile.C3XL if raw_model == b"comma tici" ...` 硬编码。
- ✅ `infer_hardware_profile()` 无条件返回 `HardwareProfile.STANDARD`：`comma tici` / 文件缺失 / 其他机型一律判为真实 C3。
- ✅ C3XL 仅可**显式选择**：`/data/hardware_profile` 内容写 `c3xl`、或环境变量 `SUNNYPILOT_HARDWARE_PROFILE=c3xl`、或显式传参。
- ✅ `STANDARD` 下 `has_driver_camera()`、`has_amplifier()`、`allows_automatic_power_down()` 均返回 `True`。
- ✅ `resolve_internal_panda_type()` **保持不变**：`STANDARD` 透传原始类型（含 DOS `0x07`）；`C3XL` 仅允许 `0x00/0x09 → 0x09`，其余抛 `ValueError`（C3XL 保护不放宽）。

### 2. Panda 适配层（单路 SPI，无外置 / 多 Panda） — `openpilot/selfdrive/pandad/`
- `pandad.h`：`SUPPORTED_PANDA_TYPES` 追加 `cereal::PandaState::PandaType::DOS`；保留原有 `RED_PANDA / TRES / CUATRO`。
- `pandad.py`：
  - 恢复动态 MCU 签名：`get_expected_signature(panda)` → `panda.get_mcu_type().config.app_fn`，移除 `McuType.H7` 硬编码。
  - 新增 `Panda.DEPRECATED_DEVICES` 跳过刷写，**避免把 H7 固件误刷进 STM32F4 DOS**。
  - 新增空签名保护（`fw_signature == b""` 直接跳过），避免异常路径误刷。
  - 保留 `InternalPanda` 单实例 + `assert len(panda_serials) == 1` 单路 SPI 架构，不引入 USB / 多 Panda 逻辑。

### 3. 外设 / 进程协同 — `openpilot/system/manager/process_config.py`（核对确认，无需改码）
- `STANDARD`：`soundd`、`dmonitoringmodeld` / `dmonitoringd` 按标准策略拉起
  （`visual_driver_monitoring = has_driver_camera() and driverview`）。
- `use_external_buzzer` 仍为 `not PC and HardwareProfile.C3XL`，`alert_output`（GPIO42）**仅 C3XL 生效**。
- `local_diagnosticsd`、`record_route_video` 仍按 C3XL 隔离。

### 4. 红线约束（未触碰）
- 未修改任何 Panda **安全规则（Safety Models）** 与控车逻辑。
- 未引入外置 USB Panda / 多 Panda 逻辑；未引入任何 PC 运行分支。

### 5. 验证
- Docker（`python:3.12-slim`）全量单测：**32 passed**
  （`test_profile` 19 + `test_panda_startup` 6 + `test_alert_output` 7）。
- 双画像脚本校验：`STANDARD` 三能力全 `True`、`C3XL` 全 `False`；`infer(comma tici) == STANDARD`；
  `resolve(0x07, STANDARD) == 0x07`、`resolve(0x07, C3XL)` 抛 `ValueError`。
- ⚠️ **尚未实机验证**：真 C3 上 `./pandad` 连通单路 SPI DOS、`soundd` 出声、
  `dmonitoringmodeld` 拉起、熄火自动休眠。

---

## 📋 版本更新记录

### `v1.0.0-c3` — 真实 Comma 3 硬件适配（2026-09-14，本分支新增）

| 项 | 内容 |
| :--- | :--- |
| 提交 | `156a3fb` c3: restore STANDARD default and single SPI DOS panda support |
| 改动文件 | 4 个：`profile.py`、`test_profile.py`、`pandad.h`、`pandad.py` |
| 新增能力 | 真实 C3 默认画像、单路 DOS Panda 放行、动态 MCU 签名、防误刷保护 |
| 保持隔离 | C3XL 画像 / 蜂鸣器 / 精简录像 / 禁用自动下电 全部不变 |
| 验证 | Docker pytest 32 passed；语法编译通过 |
| 状态 | 代码层完成，**待真机验证** |

**改动明细**
- `profile.py`：删除 `comma tici → C3XL` 硬编码；`infer_hardware_profile()` 改为一律 `STANDARD`。
- `test_profile.py`：`test_missing_profile_defaults_raw_tici_hardware_to_c3xl`
  → `..._to_standard`，断言改为 `STANDARD`。
- `pandad.h`：`SUPPORTED_PANDA_TYPES` 追加 `DOS`。
- `pandad.py`：动态 `get_expected_signature(panda)`；`DEPRECATED_DEVICES` 跳过刷写；空签名保护。

---

### `v0.9.1` — 基线收尾（2026-09-08 ~ 2026-09-13，继承自 `onemiless/openpilot:dev-sp-egpu`）

- **相机**：`camerad: add verified opt-in C3XL IFE road resize`（`2a69709`，2026-09-13）— C3XL 可选 IFE 1344x760 直出。
- **模型**：`models: follow official dev dynamic tinygrad loader`（`402a400`）、
  `modeld: follow dzid26 calibrated camera-offset horizon`（`ad4eb60`）。
- **Tesla**：`tesla: vary blindspot ambient alert by severity and light`（`795d8fd`）、
  `tesla: verify ambient alerts through Panda safety`（`6b1a1a6`）。
- **更新器**：`updater: expose nav prebuild channel`（`b82f23a`）、
  `updater: support local branch switching and public fork updates`（`878f5c7`）。
- **UI / 诊断**：`ui: double dev traffic light icon size`（`0b58694`）、
  `diag(traffic): record raw CAN and displayed lamp state`（`d172d0c`）、
  `fix(ui): refresh traffic lamps from the latest healthy plan`（`25cca36`）。

---

### `v0.9.0` — C3XL + eGPU 基线（2026-08-24 ~ 2026-09-07，继承自 `onemiless/openpilot:dev-sp-egpu`）

**eGPU / Chestnut 大模型分流**
- `egpu: follow official 500us usb polling`（`b1fb352`）、
  `egpu: preserve tested device USB runtime optimizations`（`51d8e25`）、
  `egpu: cap C3XL AMD power at 100W`、`egpu: separate link and runtime telemetry validity`。
- `models: integrate upstream v24 warp execution with C3XL compatibility`（2026-09-05）、
  `models: fall back to small model on chestnut failure` 等分流与降级逻辑。
- `usbgpu: isolate UT3G dual runtime identity`、`modeld: skip USBGPU build until big model is present`。

**C3XL 硬件画像与发布通道**
- `hardware: disable automatic C3XL power down`（2026-08-28）。
- `release: add safe C3XL prebuild channel`、`release: tolerate C3XL frequency limits`、
  `logging: divert C3XL diagnostics from rlog`、`release: finalize flattened prebuild tree`（2026-08-29）。
- `ui: expose maintained C3XL update branches`（2026-08-27）。
- ⚠️ `hardware: infer C3XL when tici profile is missing`（2026-09-03）
  — 该推断已于 `v1.0.0-c3` 撤销，改为默认 `STANDARD`。

**Tesla 支持**
- `tesla: replace home upgrade card with vehicle dashboard and red light tests`、
  `tesla: flash side-specific ambient lights for blindspot warnings`、
  `tesla: keep manual speed override until explicit resume`、
  `opendbc: update Tesla HW4 diagnostic definitions`。

**Traffic 交通灯后端**
- `traffic: make disabled control use Off instead of online Observe`、
  `traffic: bind observations to one successfully decoded CAN frame`、
  `traffic: continue bounded green starts past rolling threshold`、
  `fix(traffic): confirm flashing green on the second OFF edge`、
  `docs: record Traffic correctness and equivalence validation`（2026-09-05）。

**UI / 资源**
- `ui: replace empty consumption with vehicle diagnostics`、
  `ui: show compact device resources onroad`、`ui: move eGPU metrics into bottom status strip`。

---

## 🚘 安装 / 使用

本分支为自定义 C3 适配分支，安装方式沿用 sunnypilot 的自定义分支流程：

1. 在设备上安装/切换到此自定义分支（`duoduo2110/openpilot` · `c3-dev-sp-egpu`）。
2. **真实 C3**：无需额外操作，设备树为 `comma tici` 时自动判为 `STANDARD`。
3. **C3XL**：必须显式写入 `echo c3xl > /data/hardware_profile`（或设置
   `SUNNYPILOT_HARDWARE_PROFILE=c3xl`），否则会被判为真实 C3。

> 建议先备份原系统并记录 Panda 固件版本，异常时可回滚。

---

## ⚠️ 安全声明

本软件为 **ALPHA 级研究用途**，非产品。使用者须自行遵守当地法律法规，风险自负。
本分支未修改任何 Panda 安全规则与控车逻辑，但**任何硬件适配改动仍需实车充分验证**。

---

## Licensing

本分支继承 sunnypilot / openpilot 的 MIT 许可。包含原创工作，以及大量来自
[openpilot by comma.ai](https://github.com/commaai/openpilot) 的代码（MIT，附带额外免责声明）。

> openpilot is released under the MIT license. Some parts of the software are released under other licenses as specified.
>
> Any user of this software shall indemnify and hold harmless Comma.ai, Inc. and its directors, officers, employees, agents, stockholders, affiliates, subcontractors and customers from and against all allegations, claims, actions, suits, demands, damages, liabilities, obligations, losses, settlements, judgments, costs and expenses (including without limitation attorneys’ fees and costs) which arise out of, relate to or result from any use of this software by user.
>
> **THIS IS ALPHA QUALITY SOFTWARE FOR RESEARCH PURPOSES ONLY. THIS IS NOT A PRODUCT.
> YOU ARE RESPONSIBLE FOR COMPLYING WITH LOCAL LAWS AND REGULATIONS.
> NO WARRANTY EXPRESSED OR IMPLIED.**

完整许可条款见 [`LICENSE`](LICENSE)。
