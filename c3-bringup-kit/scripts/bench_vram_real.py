#!/usr/bin/env python3
"""真880M显存上限模拟: 预留显存压缩可用空间, 实测能否运行"""
import os, sys, time
os.environ["DEV"] = "CUDA"
os.environ["GMMU"] = "0"
os.environ["JIT_BATCH_SIZE"] = "0"
sys.path.insert(0, "/tmp/tinygrad-op")
import numpy as np
import subprocess
from cuda.bindings import driver

def gpu_used_mb():
    return int(subprocess.check_output(["nvidia-smi","--query-gpu=memory.used","--format=csv,noheader,nounits"]).decode().strip())

driver.cuInit(0)
dev = driver.cuDeviceGet(0)[1]
ctx = driver.cuCtxCreate(0, dev)[1]
TOTAL_MB = 22528

from tinygrad import Tensor, Device, dtypes, TinyJit
from tinygrad.helpers import Context
from tinygrad.nn.onnx import OnnxRunner

def build_and_run():
    runner = OnnxRunner("/home/tt/models/big_driving_supercombo.onnx")
    img = Tensor.randint(1,12,128,256, low=0,high=256, dtype="uint8", device="CUDA").realize()
    big_img = Tensor.randint(1,12,128,256, low=0,high=256, dtype="uint8", device="CUDA").realize()
    dp = Tensor.zeros(1,25,8, dtype=dtypes.half, device="CUDA").realize()
    tc = Tensor.zeros(1,2, dtype=dtypes.half, device="CUDA").realize()
    at = Tensor.zeros(1,2, dtype=dtypes.half, device="CUDA").realize()
    fb = Tensor.zeros(1,24,512, dtype=dtypes.half, device="CUDA").realize()
    @TinyJit
    def pipe(img, big_img, dp, tc, at, fb):
        with Context(FLOAT16=1, OPENPILOT_HACKS=1, TC_OPT=2):
            out = next(iter(runner({"img": img, "big_img": big_img, "desire_pulse": dp,
                                    "traffic_convention": tc, "action_t": at,
                                    "features_buffer": fb}).values())).cast("float32")
        return out.realize()
    for i in range(3):
        pipe(img.clone(), big_img.clone(), dp.clone(), tc.clone(), at.clone(), fb.clone())
        Device[Device.DEFAULT].synchronize()
    times=[]
    for i in range(10):
        t1=time.perf_counter()
        pipe(img.clone(), big_img.clone(), dp.clone(), tc.clone(), at.clone(), fb.clone())
        Device[Device.DEFAULT].synchronize()
        times.append((time.perf_counter()-t1)*1000)
    times.sort()
    return times[5]

for limit_gb in [8, 4, 2]:
    target_used = TOTAL_MB - limit_gb*1024
    need = max(0, target_used - gpu_used_mb())
    hog_ptr = None
    if need > 0:
        err, hog_ptr = driver.cuMemAlloc(need*1024*1024)
        driver.cuMemsetD8(hog_ptr, 1, need*1024*1024)
    try:
        used_before = gpu_used_mb()
        med = build_and_run()
        print(f"limit={limit_gb}GB | free_before={limit_gb}GB | OK median={med:.1f}ms | total_used={gpu_used_mb()}MB")
    except Exception as e:
        print(f"limit={limit_gb}GB | FAILED: {type(e).__name__}: {str(e)[:150]}")
    finally:
        if hog_ptr: driver.cuMemFree(hog_ptr); hog_ptr=None
print("VRAM EXPERIMENT DONE")
