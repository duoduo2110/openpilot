#!/usr/bin/env python3
"""稳态吞吐: gc off + clone移出计时区(模拟队列重叠), 纯GPU推理吞吐"""
import os, sys, time, gc
os.environ["DEV"] = "CUDA"
os.environ["GMMU"] = "0"
os.environ["JIT_BATCH_SIZE"] = "0"
os.environ["TRACEMETA"] = "0"
sys.path.insert(0, "/home/tt/egpu-tools/tinygrad-op")
import numpy as np
import subprocess

def smi(k):
    return subprocess.check_output(["nvidia-smi", f"--query-gpu={k}", "--format=csv,noheader,nounits"]).decode().strip()

from tinygrad import Tensor, Device, dtypes, TinyJit
from tinygrad.helpers import Context
from tinygrad.nn.onnx import OnnxRunner

runner = OnnxRunner("/home/tt/models/big_driving_supercombo.onnx")
img0 = Tensor.randint(1,12,128,256, low=0,high=256, dtype="uint8", device="CUDA").realize()
big0 = Tensor.randint(1,12,128,256, low=0,high=256, dtype="uint8", device="CUDA").realize()
dp0 = Tensor.zeros(1,25,8, dtype=dtypes.half, device="CUDA").realize()
tc0 = Tensor.zeros(1,2, dtype=dtypes.half, device="CUDA").realize()
at0 = Tensor.zeros(1,2, dtype=dtypes.half, device="CUDA").realize()
fb0 = Tensor.zeros(1,24,512, dtype=dtypes.half, device="CUDA").realize()

@TinyJit
def pipe(img, big_img, dp, tc, at, fb):
    with Context(FLOAT16=1, OPENPILOT_HACKS=1, TC_OPT=2):
        out = next(iter(runner({"img": img, "big_img": big_img, "desire_pulse": dp,
                                "traffic_convention": tc, "action_t": at,
                                "features_buffer": fb}).values())).cast("float32")
    return out.realize()

def mkargs():
    return (img0.clone(), big0.clone(), dp0.clone(), tc0.clone(), at0.clone(), fb0.clone())

for i in range(3):
    pipe(*mkargs())
    Device[Device.DEFAULT].synchronize()

gc.disable(); gc.freeze()

DUR = 120.0
lat = []
start = time.perf_counter()
while time.perf_counter() - start < DUR:
    args = mkargs()                       # 准备阶段(模拟openpilot队列写入, 不计时)
    Device[Device.DEFAULT].synchronize()  # 确保clone完成再计时
    t1 = time.perf_counter()
    out = pipe(*args)
    Device[Device.DEFAULT].synchronize()
    lat.append((time.perf_counter()-t1)*1000)
wall = time.perf_counter() - start

lat = np.array(lat)
print(f"[opt2] wall={wall:.1f}s frames={len(lat)}")
print(f"[opt2] gpu latency: p50={np.percentile(lat,50):.2f} p95={np.percentile(lat,95):.2f} p99={np.percentile(lat,99):.2f} max={lat.max():.1f}")
print(f"[opt2] GPU-BOUND SUSTAINED FPS = {1000/np.percentile(lat,50):.2f}")
print(f"[opt2] power/temp/vram: {smi(chr(112)+ower.draw)}/{smi(temperature.gpu)}C/{smi(memory.used)}MB")
print("OPT2 DONE")
