"""
Copyright (c) 2026-, sunnypilot contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

「外置大模型加速器」门面：把 C3 本地的大模型策略推理放到网络对端的机器上跑。

选路规则（后续 lane 生效）：`if accelerators.ready(): 用远端 elif ...: 用本地`。
本文件是裁剪版门面——每个函数都是对 remote.backend 的薄调用，在任何设备上都安全：
功能关闭只花一次 JSON 读；依赖缺失（例如没有 numpy）时返回负默认，**绝不抛异常**。

present()/ready()/enabled()/progress() 会被 UI 以 5Hz 轮询，必须便宜。
本 lane 不含 modeld seam 与 cereal 订阅，也不新增 Params key（配置走 JSON 文件）。
"""
from __future__ import annotations

import threading

# 不在模块层导入重依赖：manager、UI 都可能 import 本包
_resolved = None
_resolve_lock = threading.Lock()


def _backend():
  global _resolved
  if _resolved is None:
    with _resolve_lock:
      if _resolved is None:
        backend = _NoBackend
        try:
          from openpilot.sunnypilot.accelerators.remote import backend as _real
          backend = _real
        except ModuleNotFoundError as e:
          # 只吞掉「外部依赖缺失」（numpy 等）；自家代码导入失败必须暴露
          if (e.name or '').split('.')[0] not in ('numpy',):
            raise
        _resolved = backend
  return _resolved


class _NoBackend:
  """依赖缺失时 backend 的替代品：全部返回负默认。"""

  @staticmethod
  def installed() -> bool:
    return False

  @staticmethod
  def enabled() -> bool:
    return False

  @staticmethod
  def present() -> bool:
    return False

  @staticmethod
  def ready() -> bool:
    return False

  @staticmethod
  def unavailable_reason() -> str | None:
    return None

  @staticmethod
  def prepare() -> bool:
    return False

  @staticmethod
  def load(cam_w: int, cam_h: int, small):
    return None

  @staticmethod
  def progress() -> dict | None:
    return None

  @staticmethod
  def report_progress(stage: str, frac: float, msg: str = '') -> None:
    return None

  @staticmethod
  def clear_progress() -> None:
    return None

  @staticmethod
  def shutdown(reason: str = '', timeout: float = 25.0) -> None:
    return None

  @staticmethod
  def status_snapshot() -> dict:
    return {}


def installed() -> bool:
  """客户端层依赖是否齐全？UI 是否值得提供这个选项。"""
  return _backend().installed()


def present() -> bool:
  """是否配置了加速器目标。便宜、与链路状态无关。"""
  return _backend().present()


def ready() -> bool:
  """大模型现在能跑吗（已连接并握手）？只读状态，不发起连接。"""
  return _backend().ready()


def unavailable_reason() -> str | None:
  """用户开了开关却跑不了的原因（供 offroad 告警）。未开启时为 None。"""
  backend = _backend()
  reason = getattr(backend, 'unavailable_reason', None)
  return reason() if callable(reason) else None


def prepare() -> bool:
  """本进程是否应准备构造加速器：依赖在位 + 地址可解析。

  **不看 enabled()**：开关由 JoinedModel 运行期动态读取，才能在 modeld 已运行时
  切换「外置/内置」而无需重启进程。
  """
  return _backend().prepare()


def load(cam_w: int, cam_h: int, small):
  """接入加速器；prepare() 未通过时返回 None，失败也返回 None（照跑 small）。"""
  return _backend().load(cam_w, cam_h, small)


def enabled() -> bool:
  """用户是否开启了加速器（只看配置，不看链路）。"""
  return _backend().enabled()


def progress() -> dict | None:
  """{stage, frac, msg} 或 None；来自 UI 线程的轮询，任何情况下都不得抛出。"""
  try:
    return _backend().progress()
  except Exception:
    return None


def report_progress(stage: str, frac: float, msg: str = '') -> None:
  """上报进度，绝不抛异常（可能从 except 分支调用）。"""
  try:
    _backend().report_progress(stage, frac, msg)
  except Exception:
    pass


def clear_progress() -> None:
  try:
    _backend().clear_progress()
  except Exception:
    pass


def status_snapshot() -> dict:
  """链路状态快照（帧计数、延迟分位、big-active），供后续 lane 接 UI/日志。"""
  try:
    return _backend().status_snapshot()
  except Exception:
    return {}


def shutdown(reason: str = '', timeout: float = 25.0) -> None:
  """设备下电前断开链路，在 timeout 内尽力而为。"""
  if not enabled():
    return
  try:
    _backend().shutdown(reason, timeout)
  except Exception:
    pass
