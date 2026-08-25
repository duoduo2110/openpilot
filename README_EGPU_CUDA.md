# egpu-cuda-research 分支 — C3 + RTX 2080 Ti + NVIDIA CUDA 研究与代码

> 基线：`dev`（3220669 Initial commit from openpilot）。**不修改 dev / egpu 分支。**
> 目标：C3 (SDM845 / AGNOS Linux 4.9.103) + RTX 2080 Ti (TU102, sm_75) 经 **原生 PCIe + nvidia.ko + libcuda** 运行 880M Big Model。

## 一、代码改动（selfdrive/modeld/SConscript）

新增 `EGPU_BACKEND` 环境开关（默认行为完全不变）：

```bash
EGPU_BACKEND=CUDA scons ...   # 强制 DEV=CUDA FLOAT16=1 TC_OPT=2 编译大模型
```

- dev 原有的 `if False: # 'CUDA' in available`（FIXME-SP）保持不动；本分支仅在显式设置
  `EGPU_BACKEND=CUDA` 时启用 CUDA 后端
- 依据：research/FINAL_REPORT_2.md 路线 γ2 —— AGNOS 内核重编(CONFIG_PCI=y+CONFIG_PCI_MSM=y,
  DT &pcie1 status="ok") + NVIDIA-Linux-aarch64-535.309.01.run（零补丁编译实证）

## 二、research/ — 两轮研究报告全集（11 份）

| 文件 | 内容 |
|---|---|
| **FINAL_REPORT_2.md** | 主报告：C3 驱动链源码级研究（问题 A-J + 三大核心问题 + 可行性评分）|
| FINAL_REPORT.md | 第一轮：x86 实机 880M 全链验证 |
| research2-c3-pcie.md | 内核 4.9.103 / DT PCIe 拓扑 / pci-msm.c 控制器逐行 |
| research2-smmu-dma.md | qsmmu-v500 / IOVA<4GB / MSI-IOVA / BAR 实测(304.5MB) |
| research2-nvidia-driver.md | TU102 首发 410.57；aarch64 .run 含 2080 Ti；R410-R550 支持内核≥3.10 |
| research2-agnos-upgrade.md | BSP 止步 4.9 / ION·KGSL·CamX 三大件 / chestnut=USB 用户态协议纠错 |
| research2-compile-experiment.md | 580 open +109 行移植实证 |
| research-mysp.md 等 | my SP 分析与其余第一轮报告 |

## 三、c3-bringup-kit/ — C3 到手开工套件

```
kernel/0001-K1-K2-kernel-build-fixes.patch   # python2 wrapper / GCC11 下游驱动修复
kernel/config-fragment                       # CONFIG_PCI=y CONFIG_PCI_MSM=y PCIEPORTBUS/MSI...
dt/pcie1-enable.patch                        # &pcie1 status="ok"（唯一可用口 Gen3/509MB）
sp-patches/0001-modeld-EGPU_BACKEND-cuda.patch
docs/BUILD_ON_C3.md                          # 五阶段真机步骤书 + 回退矩阵
scripts/*.py                                 # tt 上已验证的实验脚本
```

## 四、已实证关键事实

| 事实 | 验证方式 |
|---|---|
| 真 880M = supercombo.onnx 1.757GB FP16；2080Ti 上 30.4ms / 32.9FPS / VRAM 1.9GB | x86 实机 |
| tinygrad CUDA sm_75 零障碍（NVRTC/TinyJit/CUDAGraph 全过） | x86 实机 |
| R535.309.01 专有驱动零补丁编出全部五模块，vermagic=4.9.103+ SMP preempt aarch64 | 交叉编译实验 |
| 580 open +109 行同样可移植（备用路线） | 交叉编译实验 |
| chestnut 在 C3 = USB 用户态 TLP 协议 → AMD 栈不可迁移 NVIDIA | 源码链 |

## 五、快速开始
拿到 C3 后按 `c3-bringup-kit/docs/BUILD_ON_C3.md` 执行。

## 六、分支关系
| 分支 | 说明 | 本分支是否改动它 |
|---|---|---|
| dev | 默认基线（本分支基点）| ❌ 不动 |
| egpu | chestnut 架构对齐 | ❌ 不动 |
| **egpu-cuda-research** | 本分支：研究文档 + EGPU_BACKEND 开关 | — |
