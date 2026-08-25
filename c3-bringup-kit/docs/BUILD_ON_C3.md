# C3 + RTX 2080 Ti 开工指南（拿到真机后照此执行）

> 前置阅读：`FINAL_REPORT_2.md`（机制与证据）。本套件假设目标 = 原生 PCIe 直连路线（γ2）。
> 硬件前提：SDM845 pcie1 差分对已物理引出至 GPU（M.2 转接 / 板级改装，参考 nelsonjchen NVMe 改装）。
> ⚠️ ADT-UT3G 的 USB 通道对 NVIDIA 无现成软件栈（见 FINAL_REPORT_2 §11/§12）。

## 阶段 0：备份与回滚准备
```bash
# C3 当前 boot/system 分区镜像备份（bricked 时可还原）
adb shell su -c "dd if=/dev/block/bootdevice/by-name/boot_a of=/data/boot_backup.img"
```

## 阶段 1：重编 AGNOS 内核
```bash
git clone https://github.com/commaai/agnos-kernel-sdm845 && cd agnos-kernel-sdm845
git apply c3-bringup-kit/kernel/0001-K1-K2-kernel-build-fixes.patch   # 仅构建宿主需要；若在 aarch64 原生构建可跳过 K1
export ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu-
make tici_defconfig
merge fragment:  c3-bringup-kit/kernel/config-fragment               # CONFIG_PCI=y CONFIG_PCI_MSM=y 等
make -j$(nproc)
# 产物: arch/arm64/boot/Image-dtb → 用 agnos-builder mkbootimg 打包刷入
```
验证点：启动后 `ls /sys/bus/pci/devices/` 应非空（pcie1 枚举）。

## 阶段 2：DT 启用 pcie1
`c3-bringup-kit/dt/pcie1-enable.patch` 已含于阶段 1 的 Image-dtb（同一棵 DT 编译）。
上电时序要求：GPU 供电先于 RC probe（无热插拔）；必要时外部 ATX 先上电再开机。

## 阶段 3：驱动部署（零补丁）
```bash
# 从本 kit 或 nvidia 官方下载（aarch64）
sh NVIDIA-Linux-aarch64-535.309.01.run -x --target nvdrv
cd nvdrv
IGNORE_MISSING_MODULE_SYMVERS=1 make -j$(nproc) module ARCH=arm64 \
  CC=$CROSS-gcc ... SYSOUT=SYSSRC=<内核树>     # 已在 tt 实证 EXIT=0 五模块全过
sudo insmod nvidia.ko && sudo insmod nvidia-uvm.ko    # drm/modeset 可选(无头不需要)
# GSP 固件:
sudo mkdir -p /lib/firmware/nvidia/535.309.01/
sudo cp firmware/gsp_tu10x.bin /lib/firmware/nvidia/535.309.01/
```
验证点：`dmesg | grep -i nvrm` 无 SMMU fault；`nvidia-smi` 出卡。
⚠️ 若 SMMU 报翻译错误：优先检查 PCIe TBU GDSC（hlos1_vote_aggre_noc_mmu_pcie_tbu_gdsc）；
   兜底方案 = DT 删除 iommu-map 条目走 bypass（research2-smmu-dma.md §1.5）。

## 阶段 4：CUDA 用户态
```bash
# libcuda.so.535.309.01 来自解包目录 → /usr/lib/aarch64-linux-gnu/
# NVRTC/cudart: 从 sbsa repo 装 cuda-nvrtc/cuda-cudart (glibc≥2.17, AGNOS 满足)
cd mysp (dev-sp-egpu-cuda 分支)
EGPU_BACKEND=CUDA scons ...   # 构建 big_driving_tinygrad.pkl (sm_75 CUDA kernels)
```
验证点：pkl 内核为 CUDA target；modeld 启动读 tg_input_devices.json 中 QUEUE_DEV='CUDA'。

## 阶段 5：端到端
```bash
# 合成帧冒烟: VisionIpcServer 发 1928x1208 NV12 → modeld → modelV2 消息
# 之后接 camerad 实时管线
```

## 回退矩阵
| 症状 | 动作 |
|---|---|
| lspci 空 | PERST/GPIO102 时序、供电轨、链路训练(LTSSM)日志 pci-msm.c:3926 |
| insmod 失败 vermagic | 内核须为同一路径构建的 4.9.103（LOCALVERSION=""）|
| NVRM: SMMU fault | TBU GDSC / iommu-map-mask / S1_BYPASS 三选一排查 |
| GSP 加载超时 | firmware_class.path=/firmware/image 特殊路径适配 |
