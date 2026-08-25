#!/usr/bin/env python3
"""880M 持续负载压力测试: 20Hz x 180s, 记录延迟分布/功耗/温度/显存"""
import os, sys, time, threading
os.environ["DEV"] = "CUDA"
os.environ["GMMU"] = "0"
os.environ["JIT_BATCH_SIZE"] = "0"
sys.path.insert(0, "/home/tt/egpu-tools/tinygrad-op")
import numpy as np
import subprocess

def smi(*keys):
    return subprocess.check_output(["nvidia-smi", f"--query-gpu={",".join(keys)}",
        "--format=csv,noheader,nounits"]).decode().strip()

from tinygrad import Tensor, Device, dtypes, TinyJit
from tinygrad.helpers import Context
from tinygrad.nn.onnx import OnnxRunner

T0 = time.perf_counter()
print(f"[cold] process start")
runner = OnnxRunner("/home/tt/models/big_driving_supercombo.onnx")
t_load = time.perf_counter() - T0
print(f"[cold] ONNX load: {t_load:.2f}s")

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

t_cap0 = time.perf_counter()
for i in range(3):
    pipe(img.clone(), big_img.clone(), dp.clone(), tc.clone(), at.clone(), fb.clone())
    Device[Device.DEFAULT].synchronize()
vram0 = smi("memory.used")
print(f"[cold] JIT capture+validate: {time.perf_counter()-t_cap0:.1f}s | VRAM {vram0} MB")

# 20Hz 调度器模拟: 目标每 50ms 一帧, 统计实际达成率
DUR = 180.0
PERIOD = 0.05
lat = []
missed = 0
power_log = []
temp_log = []
n_frames = int(DUR / PERIOD)
start = time.perf_counter()
for i in range(n_frames):
    target = start + (i+1)*PERIOD
    t1 = time.perf_counter()
    pipe(img.clone(), big_img.clone(), dp.clone(), tc.clone(), at.clone(), fb.clone())
    Device[Device.DEFAULT].synchronize()
    dt_ms = (time.perf_counter()-t1)*1000
    lat.append(dt_ms)
    now = time.perf_counter()
    if now > target:
        missed += 1
    if i % 60 == 0:
        power_log.append(smi("power.draw"))
        temp_log.append(smi("temperature.gpu"))
    sleep_t = target - time.perf_counter()
    if sleep_t > 0:
        time.sleep(sleep_t)
wall = time.perf_counter() - start

lat = np.array(lat)
print(f"\n[stress] wall={wall:.1f}s frames={len(lat)} target={n_frames}")
print(f"[stress] latency: p50={np.percentile(lat,50):.1f}ms p95={np.percentile(lat,95):.1f}ms p99={np.percentile(lat,99):.1f}ms max={lat.max():.1f}ms")
real_fps = len(lat)/wall
print(f"[stress] real FPS={real_fps:.1f} | missed deadlines={missed}/{n_frames} ({100*missed/n_frames:.1f}%)")
print(f"[stress] power samples: {power_log}")
print(f"[stress] temp samples: {temp_log}")
print(f"[stress] end VRAM: {smi(memory.used)} MB")
print("STRESS DONE")
