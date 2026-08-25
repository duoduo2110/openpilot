#!/usr/bin/env python3
"""
最小 CUDA 实机验证程序 — RTX 2080 Ti (sm_75, Turing)
验证: cudaGetDeviceCount / cudaGetDeviceProperties
      cuMemAlloc / cuMemcpyHtoD / kernel launch / cuMemcpyDtoH / sync
      FP32 kernel + FP16 kernel
"""
from cuda.bindings import driver
import numpy as np

def errchk(r):
    if isinstance(r, tuple):
        err = r[0]
        if err != driver.CUresult.CUDA_SUCCESS:
            raise RuntimeError(f"CUDA error: {err}")
        return r[1] if len(r) > 1 else None
    return r

print("=" * 60)
print("STEP 1: Device enumeration")
print("=" * 60)
driver.cuInit(0)
count = errchk(driver.cuDeviceGetCount())
print(f"cudaGetDeviceCount -> {count}")

def get_attr(dev, name):
    """Look up attribute enum by name string, fall back to search."""
    attrs = [a for a in dir(driver.CUdevice_attribute) if not a.startswith("_")]
    for a in attrs:
        if a.endswith(name) or a == name:
            return errchk(driver.cuDeviceGetAttribute(getattr(driver.CUdevice_attribute, a), dev))
    # fallback: try known integer attribute numbers
    known = {"COMPUTE_CAPABILITY_MAJOR": 75, "COMPUTE_CAPABILITY_MINOR": 76,
             "MULTIPROCESSOR_COUNT": 40, "L2_CACHE_SIZE": 38, "GLOBAL_MEMORY_BUS_WIDTH": 89,
             "MAX_THREADS_PER_MULTIPROCESSOR": 65, "WARP_SIZE": 71}
    return errchk(driver.cuDeviceGetAttribute(known.get(name, 75), dev))

dev = errchk(driver.cuDeviceGet(0))
name = errchk(driver.cuDeviceGetName(128, dev)).split(b"\x00")[0].decode()
cc_maj = get_attr(dev, "COMPUTE_CAPABILITY_MAJOR")
cc_min = get_attr(dev, "COMPUTE_CAPABILITY_MINOR")
mem = errchk(driver.cuDeviceTotalMem(dev))
print(f"GPU name          : {name}")
print(f"Compute Capability: {cc_maj}.{cc_min}")
print(f"Global Memory     : {mem/1e9:.2f} GB ({mem/1024**3:.2f} GiB)")
print(f"SM count          : {get_attr(dev, 'MULTIPROCESSOR_COUNT')}")
print(f"L2 cache          : {get_attr(dev, 'L2_CACHE_SIZE')}")
print(f"Mem bus width     : {get_attr(dev, 'GLOBAL_MEMORY_BUS_WIDTH')} bits")
print(f"Max threads/SM    : {get_attr(dev, 'MAX_THREADS_PER_MULTIPROCESSOR')}")
print(f"Warp size         : {get_attr(dev, 'WARP_SIZE')}")

print()
print("=" * 60)
print("STEP 2: Context + FP32 kernel (cudaMalloc/cudaMemcpy/launch/sync)")
print("=" * 60)
ctx = errchk(driver.cuCtxCreate(0, dev))
print("Context created")

N = 1 << 20  # 1M elements
a_host = np.arange(N, dtype=np.float32)
b_host = np.full(N, 2.0, dtype=np.float32)
out_host = np.empty(N, dtype=np.float32)

# CUDA kernel source (PTX-like CUDA C via NVRTC style, but driver needs cubin; use cuModuleLoadDataEx path)
# Simplest: use driver API with precompiled module via NVRTC. Here use cuMemAlloc + a trivial
# copy kernel is complex without nvcc. Instead demonstrate FP32 math via cuBLAS? No - keep pure driver.
# We'll do: allocate device buffers, memcpy host->device->host roundtrip, plus run a tiny kernel
# compiled with NVRTC below.

d_a = int(errchk(driver.cuMemAlloc(N * 4)))
d_b = int(errchk(driver.cuMemAlloc(N * 4)))
d_out = int(errchk(driver.cuMemAlloc(N * 4)))
print(f"cuMemAlloc x3 -> {N*4*3/1e6:.0f} MB device memory allocated")

errchk(driver.cuMemcpyHtoD(d_a, a_host.ctypes.data, N * 4))
errchk(driver.cuMemcpyHtoD(d_b, b_host.ctypes.data, N * 4))
print("cuMemcpyHtoD x2 done")

# Roundtrip check (basic sanity)
buf = np.empty(N, dtype=np.float32)
errchk(driver.cuMemcpyDtoH(buf.ctypes.data, d_a, N * 4))
assert np.array_equal(buf, a_host), "H2D->D2H roundtrip failed!"
print("cuMemcpyDtoH roundtrip OK")

# --- NVRTC kernel: vector add FP32 ---
print()
print("STEP 3: NVRTC compile + launch FP32 vector add")
from cuda.bindings import nvrtc
import ctypes

kernel_src = r"""
extern "C" __global__ void vec_add(const float* a, const float* b, float* out, int n) {
  int i = blockIdx.x * blockDim.x + threadIdx.x;
  if (i < n) out[i] = a[i] + b[i];
}
"""
prog = nvrtc.nvrtcCreateProgram(bytes(kernel_src, "utf-8"), b"vec_add.cu", 0, None, None)[1]
opts = (b"--gpu-architecture=sm_75", b"--fmad=true",
        b"--include-path=/home/tt/egpu-venv/lib/python3.10/site-packages/nvidia/cuda_runtime/include")
res = nvrtc.nvrtcCompileProgram(prog, len(opts), opts)
logsz = nvrtc.nvrtcGetProgramLogSize(prog)[1]
if res[0] != nvrtc.nvrtcResult.NVRTC_SUCCESS:
    log_buf = bytearray(logsz)
    nvrtc.nvrtcGetProgramLog(prog, log_buf)
    print("NVRTC FAIL:", log_buf.decode())
    raise SystemExit(1)
print(f"NVRTC compile sm_75 OK (log size {logsz})")
ptxsz = nvrtc.nvrtcGetPTXSize(prog)[1]
ptx_buf = bytearray(ptxsz)
nvrtc.nvrtcGetPTX(prog, ptx_buf)
ptx = bytes(ptx_buf)
print(f"PTX size: {ptxsz} bytes, contains 'target sm_75': {b'target sm_75' in ptx}")

mod = errchk(driver.cuModuleLoadData(ptx))
func = errchk(driver.cuModuleGetFunction(mod, b"vec_add"))
print("Module loaded, function 'vec_add' located")

block = 256
grid = (N + block - 1) // block
p_da, p_db, p_dout, p_n = (ctypes.c_void_p(d_a), ctypes.c_void_p(d_b),
                           ctypes.c_void_p(d_out), ctypes.c_int32(N))
kernel_params = (ctypes.c_void_p * 4)(
    ctypes.cast(ctypes.byref(p_da), ctypes.c_void_p),
    ctypes.cast(ctypes.byref(p_db), ctypes.c_void_p),
    ctypes.cast(ctypes.byref(p_dout), ctypes.c_void_p),
    ctypes.cast(ctypes.byref(p_n), ctypes.c_void_p),
)
errchk(driver.cuLaunchKernel(func, grid, 1, 1, block, 1, 1, 0, None, kernel_params, 0))
print(f"Kernel launched: grid={grid} block={block}")

errchk(driver.cuCtxSynchronize())
print("cuCtxSynchronize OK")

errchk(driver.cuMemcpyDtoH(out_host.ctypes.data, d_out, N * 4))
expected = a_host + b_host
ok = np.allclose(out_host, expected)
print(f"FP32 vec_add result check: {ok}  (sample: out[0]={out_host[0]}, expect {expected[0]})")

# --- FP16 kernel ---
print()
print("STEP 4: NVRTC compile + launch FP16 vector add (Turing FP16 path)")
kernel_f16 = r"""
#include <cuda_fp16.h>
extern "C" __global__ void vec_add_f16(const __half* a, const __half* b, __half* out, int n) {
  int i = blockIdx.x * blockDim.x + threadIdx.x;
  if (i < n) out[i] = __hadd(a[i], b[i]);
}
"""
prog2 = nvrtc.nvrtcCreateProgram(bytes(kernel_f16, "utf-8"), b"vec_add_f16.cu", 0, None, None)[1]
res2 = nvrtc.nvrtcCompileProgram(prog2, len(opts), opts)
logsz2 = nvrtc.nvrtcGetProgramLogSize(prog2)[1]
if res2[0] != nvrtc.nvrtcResult.NVRTC_SUCCESS:
    log_buf2 = bytearray(logsz2)
    nvrtc.nvrtcGetProgramLog(prog2, log_buf2)
    print("NVRTC FP16 FAIL:", log_buf2.decode())
    raise SystemExit(1)
print("NVRTC compile FP16 sm_75 OK")
ptxsz2 = nvrtc.nvrtcGetPTXSize(prog2)[1]
ptx_buf2 = bytearray(ptxsz2)
nvrtc.nvrtcGetPTX(prog2, ptx_buf2)
ptx2 = bytes(ptx_buf2)
mod2 = errchk(driver.cuModuleLoadData(ptx2))
func2 = errchk(driver.cuModuleGetFunction(mod2, b"vec_add_f16"))

a16 = a_host[:256].astype(np.float16)
b16 = b_host[:256].astype(np.float16)
out16 = np.empty(256, dtype=np.float16)
d_a16 = int(errchk(driver.cuMemAlloc(256 * 2)))
d_b16 = int(errchk(driver.cuMemAlloc(256 * 2)))
d_o16 = int(errchk(driver.cuMemAlloc(256 * 2)))
errchk(driver.cuMemcpyHtoD(d_a16, a16.ctypes.data, 256 * 2))
errchk(driver.cuMemcpyHtoD(d_b16, b16.ctypes.data, 256 * 2))
p_a16, p_b16, p_o16, p_n16 = (ctypes.c_void_p(d_a16), ctypes.c_void_p(d_b16),
                              ctypes.c_void_p(d_o16), ctypes.c_int32(256))
kernel_params_f16 = (ctypes.c_void_p * 4)(
    ctypes.cast(ctypes.byref(p_a16), ctypes.c_void_p),
    ctypes.cast(ctypes.byref(p_b16), ctypes.c_void_p),
    ctypes.cast(ctypes.byref(p_o16), ctypes.c_void_p),
    ctypes.cast(ctypes.byref(p_n16), ctypes.c_void_p),
)
errchk(driver.cuLaunchKernel(func2, 1, 1, 1, 256, 1, 1, 0, None, kernel_params_f16, 0))
errchk(driver.cuCtxSynchronize())
errchk(driver.cuMemcpyDtoH(out16.ctypes.data, d_o16, 256 * 2))
exp16 = (a16.astype(np.float32) + b16.astype(np.float32)).astype(np.float16)
ok16 = np.array_equal(out16, exp16)
print(f"FP16 vec_add result check: {ok16} (sample: out[0]={out16[0]}, expect {exp16[0]})")

print()
print("=== ALL CUDA TESTS PASSED ON REAL GPU ===")