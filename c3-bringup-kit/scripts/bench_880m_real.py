#!/usr/bin/env python3
"""真 880M big_driving_supercombo.onnx (1.76GB FP16) 在 RTX 2080 Ti 上基准"""
import os, sys, time
os.environ["DEV"] = "CUDA"
os.environ["GMMU"] = "0"
os.environ["JIT_BATCH_SIZE"] = "0"
sys.path.insert(0, "/tmp/tinygrad-op")
import numpy as np
import subprocess

def gpu_used_mb():
    return int(subprocess.check_output(["nvidia-smi","--query-gpu=memory.used","--format=csv,noheader,nounits"]).decode().strip())

from tinygrad import Tensor, Device, dtypes, TinyJit
from tinygrad.helpers import Context
from tinygrad.nn.onnx import OnnxRunner

print(f"GPU before: {gpu_used_mb()} MB")
t0 = time.perf_counter()
runner = OnnxRunner("/home/tt/models/big_driving_supercombo.onnx")
print(f"ONNX load: {(time.perf_counter()-t0)*1000:.0f} ms")

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

t0 = time.perf_counter()
for i in range(3):
    out = pipe(img.clone(), big_img.clone(), dp.clone(), tc.clone(), at.clone(), fb.clone())
    Device[Device.DEFAULT].synchronize()
cap = time.perf_counter()-t0
print(f"capture+validate: {cap:.1f}s | VRAM after capture: {gpu_used_mb()} MB")

times=[]
for i in range(20):
    t1=time.perf_counter()
    out = pipe(img.clone(), big_img.clone(), dp.clone(), tc.clone(), at.clone(), fb.clone())
    Device[Device.DEFAULT].synchronize()
    times.append((time.perf_counter()-t1)*1000)
times.sort()
print(f"REAL-880M: median {times[10]:.1f} ms | min {times[0]:.1f} | max {times[-1]:.1f} | ~{1000/times[10]:.1f} FPS @20Hz-budget-{50}ms")
print(f"VRAM steady: {gpu_used_mb()} MB")
import subprocess as sp
print("power/temp:", __import__("subprocess").check_output(["nvidia-smi","--query-gpu=power.draw,temperature.gpu","--format=csv,noheader"]).decode().strip())
print("DONE")
