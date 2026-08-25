#!/usr/bin/env python3
"""部署模拟: 全新进程加载生产pkl + PCIe带宽 + 阶段分解"""
import os, sys, time
os.environ["DEV"] = "CUDA"
os.environ["GMMU"] = "0"
os.environ["JIT_BATCH_SIZE"] = "0"
sys.path.insert(0, "/home/tt/egpu-tools/tinygrad-op")
import numpy as np
import subprocess

def smi(k):
    return subprocess.check_output(["nvidia-smi", f"--query-gpu={k}", "--format=csv,noheader,nounits"]).decode().strip()

print("=== [1] PCIe bandwidth test ===")
from cuda.bindings import driver
import ctypes
driver.cuInit(0)
dev = driver.cuDeviceGet(0)[1]
ctx = driver.cuCtxCreate(0, dev)[1]
SZ = 64*1024*1024
host_buf = np.ones(SZ, dtype=np.uint8)
dA = int(driver.cuMemAlloc(SZ)[1])
for _ in range(3): driver.cuMemcpyHtoD(dA, host_buf.ctypes.data, SZ)  # warmup
t0=time.perf_counter()
N=20
for _ in range(N): driver.cuMemcpyHtoD(dA, host_buf.ctypes.data, SZ)
h2d = SZ*N/1e9/(time.perf_counter()-t0)
t0=time.perf_counter()
for _ in range(N): driver.cuMemcpyDtoH(host_buf.ctypes.data, dA, SZ)
d2h = SZ*N/1e9/(time.perf_counter()-t0)
print(f"H2D: {h2d:.2f} GB/s | D2H: {d2h:.2f} GB/s")
print(f"[推算] C3 PCIe Gen3 x1 (~985MB/s理论, ~800MB/s实测典型): 加载1.77GB pkl 约 {1770/800:.1f}s")

print("\n=== [2] 生产pkl冷启动加载(modeld启动模拟) ===")
from tinygrad import Tensor, Device, dtypes
import pickle
T0=time.perf_counter()
with open("/home/tt/models/big_driving_supercombo_cuda.pkl","rb") as f:
    jit_run = pickle.load(f)
t_load = time.perf_counter()-T0
print(f"pkl unpickle: {t_load:.2f}s")

rng = np.random.default_rng(100)
def mk(shp, dt):
    a = rng.standard_normal(shp).astype(np.float32)*8
    return Tensor(a, device="NPY") if dt==dtypes.float else Tensor(a.astype(np.uint8), device="CUDA")
inputs = dict(
    img=mk((1,12,128,256), dtypes.uint8),
    big_img=mk((1,12,128,256), dtypes.uint8),
    desire_pulse=mk((1,25,8), dtypes.float),
    traffic_convention=mk((1,2), dtypes.float),
    action_t=mk((1,2), dtypes.float),
    features_buffer=mk((1,24,512), dtypes.float))
# 保持 compile3 语义: img 在 CUDA, 其余留在 NPY(JIT 内部拷贝)
pass

T0=time.perf_counter()
out = jit_run(**inputs)
Device[Device.DEFAULT].synchronize()
t_first = time.perf_counter()-T0
print(f"first inference after load: {t_first*1000:.1f} ms")
print(f"out shape: {out.shape}, sample: {out.flatten()[:4].numpy()}")

lat=[]
for _ in range(30):
    t1=time.perf_counter()
    out = jit_run(**inputs)
    Device[Device.DEFAULT].synchronize()
    lat.append((time.perf_counter()-t1)*1000)
lat=np.array(lat)
print(f"steady replay: p50={np.percentile(lat,50):.1f}ms min={lat.min():.1f} => {1000/np.percentile(lat,50):.1f} FPS")

print("\n=== [3] 阶段分解(单帧内) ===")
# 输入已在显存: 分解为 enqueue(CPU提交) vs GPU执行 vs 输出回读
t1=time.perf_counter(); out = jit_run(**inputs); t_enq=(time.perf_counter()-t1)*1000
t1=time.perf_counter(); Device[Device.DEFAULT].synchronize(); t_sync=(time.perf_counter()-t1)*1000
arr = out.numpy()
t_d2h=(time.perf_counter()-t1)*1000
print(f"enqueue(CPU提交): {t_enq:.2f} ms | GPU执行(sync等待): {t_sync:.2f} ms | 输出D2H+解析: {t_d2h:.2f} ms")
print(f"VRAM: {smi(memory.used)} MB")
print("DEPLOY DONE")
