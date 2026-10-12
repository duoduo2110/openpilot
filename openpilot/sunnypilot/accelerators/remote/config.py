"""
Copyright (c) 2026-, sunnypilot contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

「外置大模型加速器」的配置读写。

C3 是 prebuilt 构建，改 params_keys.h 需要重编 C++，属高危操作，所以这里
不用 Params，改用 /data/models/accelerator.json 这个 JSON 文件；daemon（后续
lane）与 UI 通过它交换开关和参数。

三条硬性要求：
- 文件缺失、损坏、字段非法时一律回落到安全默认（enabled=False），绝不抛异常；
- 写入用「同目录临时文件 + os.replace」原子替换，避免读到半截 JSON；
- 未知字段保留，方便后续 lane 渐进式扩展而不丢用户配置。
"""
from __future__ import annotations

import json
import os
import tempfile

# 默认路径；测试/开发可用环境变量覆盖，避免污染 /data
CONFIG_PATH = os.environ.get('ACCELERATOR_CONFIG_PATH', '/data/models/accelerator.json')

# 仅这些字段会被校验与规整；其余原样保留
DEFAULTS: dict = {
  'enabled': False,
  'address': '192.168.43.2:5202',
  'warp_path': '/data/models/remote_warp.pkl',
  'deadline_ms': 60,
  'pipeline_depth': 1,
  # 上行 tensor 载荷 zlib 压缩开关。默认关闭：实测 C3 小核 deflate 比链路省下的
  # 时间更贵（详见压缩评估），仅在明确需要时由配置打开。
  'payload_compression': False,
  # 传输模式：'tcp'（默认，行为与历史一致）、'udp'（可选的大帧低延迟分片传输）
  # 或 'bulk'（可选的 vendor bulk / FunctionFS 直连）。非法值一律回落 'tcp'。
  'transport': 'tcp',
  # 远程模型调试日志开关。默认关闭：关闭时链路每帧只有一次缓存布尔判断，
  # 不写盘、不入队；打开后关键事件异步写入 /data/accelerator/remote_debug.log。
  'debug_log': False,
}

# 传输模式白名单；其余取值（含非字符串）回落默认
_VALID_TRANSPORTS = ('tcp', 'udp', 'bulk')

# 数值字段的合法区间：越界即钳到边界（而不是丢弃整份配置）
_LIMITS = {
  'deadline_ms': (5, 2000),
  'pipeline_depth': (1, 4),
}


def resolve_path(path: str | None = None) -> str:
  """本次读写使用的配置文件路径。"""
  return path or CONFIG_PATH


def _clamp(value, low, high):
  return max(low, min(high, value))


def sanitize(raw) -> dict:
  """把任意来源的 dict 规整成一份可用配置；损坏输入回落默认，绝不抛异常。"""
  cfg = dict(DEFAULTS)
  if not isinstance(raw, dict):
    return cfg

  # 保留未知字段（后续 lane 的扩展字段不会因本次规整而丢）
  for key, value in raw.items():
    if key not in DEFAULTS:
      cfg[key] = value

  enabled = raw.get('enabled')
  cfg['enabled'] = enabled if isinstance(enabled, bool) else bool(DEFAULTS['enabled'])

  compression = raw.get('payload_compression')
  cfg['payload_compression'] = compression if isinstance(compression, bool) else bool(DEFAULTS['payload_compression'])

  debug_log = raw.get('debug_log')
  cfg['debug_log'] = debug_log if isinstance(debug_log, bool) else bool(DEFAULTS['debug_log'])

  transport = raw.get('transport')
  if isinstance(transport, str) and transport.strip().lower() in _VALID_TRANSPORTS:
    cfg['transport'] = transport.strip().lower()
  else:
    cfg['transport'] = DEFAULTS['transport']

  address = raw.get('address')
  cfg['address'] = address if isinstance(address, str) and address.strip() else DEFAULTS['address']

  warp_path = raw.get('warp_path')
  cfg['warp_path'] = warp_path if isinstance(warp_path, str) and warp_path.strip() else DEFAULTS['warp_path']

  for key, (low, high) in _LIMITS.items():
    value = raw.get(key, DEFAULTS[key])
    if isinstance(value, bool) or not isinstance(value, (int, float)):
      value = DEFAULTS[key]
    cfg[key] = int(_clamp(int(value), low, high))

  return cfg


def load(path: str | None = None) -> dict:
  """读取配置；缺失/损坏/无权限都回落到安全默认（enabled=False）。"""
  try:
    with open(resolve_path(path), encoding='utf-8') as f:
      raw = json.load(f)
  except (OSError, ValueError):
    return dict(DEFAULTS)
  return sanitize(raw)


def save(cfg: dict, path: str | None = None) -> bool:
  """原子写入配置；成功返回 True，任何 IO 失败返回 False（不抛异常）。"""
  target = resolve_path(path)
  data = sanitize(cfg)
  directory = os.path.dirname(target) or '.'
  tmp_path = None
  try:
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix='.accelerator-', suffix='.json', dir=directory)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
      json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
      f.flush()
      os.fsync(f.fileno())
    os.replace(tmp_path, target)
    tmp_path = None
    return True
  except (OSError, ValueError, TypeError):
    return False
  finally:
    if tmp_path is not None:
      try:
        os.unlink(tmp_path)
      except OSError:
        pass


def enabled(path: str | None = None) -> bool:
  """开关，供 UI/门面高频轮询：只读一次文件，绝不抛异常。"""
  return bool(load(path)['enabled'])


def address(path: str | None = None) -> str:
  return str(load(path)['address'])


def deadline_s(path: str | None = None) -> float:
  return float(load(path)['deadline_ms']) / 1000.0


def pipeline_depth(path: str | None = None) -> int:
  return int(load(path)['pipeline_depth'])


def transport(path: str | None = None) -> str:
  """传输模式：'tcp'（默认）、'udp' 或 'bulk'；配置缺失/损坏一律回落 'tcp'。"""
  return str(load(path)['transport'])


def debug_log(path: str | None = None) -> bool:
  """远程模型调试日志开关；缺失/损坏一律回落 False（默认不写盘）。"""
  return bool(load(path)['debug_log'])
