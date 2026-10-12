"""
Copyright (c) 2026-, sunnypilot contributors.

本文件是 Part of sunnypilot，基于 MIT License 发布。

`backend.load()` 的离线单测（无网络、无 tinygrad、无真实工件）：

设计缺陷回归——「笔记本服务在 modeld 启动时不可达 → 本次点火周期内永远不再尝试
连接」。修好后 `load()` 分两段：

* 真失败（warp 工件缺失/metadata 不足）→ 返回 None，modeld 跑纯 small；
* 首次握手失败 → **仍返回未连接的远端对象**（client 未连接），交给 JoinedModel
  运行期按开关 + 退避重试。

因此这里断言：握手失败时 `load()` 返回可用对象且 `client` 未连接；工件失败时
返回 None；且这两个行为与配置里的 `enabled` 无关（enabled 由 JoinedModel 动态判）。
"""
from __future__ import annotations

import unittest
from unittest import mock

from openpilot.sunnypilot.accelerators.remote import backend
from openpilot.sunnypilot.accelerators.remote.client import RemoteLinkError
from openpilot.sunnypilot.accelerators.remote.status import RemoteStatus

_CFG = {
  'enabled': False,
  'address': '127.0.0.1:1',
  'warp_path': '/tmp/does-not-exist-remote_warp.pkl',
  'deadline_ms': 60,
  'pipeline_depth': 1,
  'payload_compression': False,
}


class _FakeRemoteModelState:
  """替身：不碰真实工件与 GPU，只记录 client/status 与 warmup。"""

  last = None

  def __init__(self, cam_w, cam_h, client, **kw):
    self.cam_w = cam_w
    self.cam_h = cam_h
    self.client = client
    self.status = kw.get('status')
    self.metadata_source = 'artifact_metadata'
    self.warms = 0
    _FakeRemoteModelState.last = self

  def warmup(self):
    self.warms += 1


class BackendLoadTests(unittest.TestCase):
  def setUp(self):
    self._prev_client = backend._CLIENT
    self._prev_status = backend._STATUS
    backend._CLIENT = None
    backend._STATUS = RemoteStatus()
    self.addCleanup(self._restore)

  def _restore(self):
    backend._CLIENT = self._prev_client
    backend._STATUS = self._prev_status
    _FakeRemoteModelState.last = None

  def _patch_cfg(self):
    return mock.patch.object(backend.config, 'load', lambda *a, **k: dict(_CFG))

  def test_load_never_connects_on_the_realtime_thread(self):
    """(a) `load()` 跑在 modeld 的 SCHED_FIFO 实时主线程上，绝不能自己做 connect。

    bulk 端点在主机消失时会把写阻塞在 ffs 写队列里（真机实测主线程 wchan 卡在
    `ffs_epfile_io`，整个控制循环连同相机一起停摆）。因此 `load()` 只**武装**链路
    层的后台重连监督器，由那条普通优先级守护线程去 connect + HELLO。
    """
    armed = []

    def _arm(self, flag=True, *, immediate=False):
      armed.append((bool(flag), bool(immediate)))

    with self._patch_cfg(), \
         mock.patch.object(backend, 'RemoteModelState', _FakeRemoteModelState), \
         mock.patch.object(backend.RemotePipelineClient, 'arm_reconnect', _arm), \
         mock.patch.object(backend.RemotePipelineClient, 'connect_and_hello',
                           side_effect=AssertionError('load() 不得在实时线程上握手')):
      acc = backend.load(1920, 1208, None)

    self.assertIsNotNone(acc)
    self.assertIsNotNone(acc.client)
    self.assertEqual(acc.warms, 1)
    self.assertIs(backend._CLIENT, acc.client)
    self.assertEqual(armed, [(True, True)], '必须武装监督器并立即触发一次重连（不等退避）')

  def test_reconnect_supervisor_failure_keeps_object_usable(self):
    """(b) 武装失败也不得让加速器消失：返回对象、warmup 已跑。"""
    with self._patch_cfg(), \
         mock.patch.object(backend, 'RemoteModelState', _FakeRemoteModelState), \
         mock.patch.object(backend.RemotePipelineClient, 'arm_reconnect',
                           side_effect=RemoteLinkError('connection refused')):
      acc = backend.load(1920, 1208, None)

    self.assertIsNotNone(acc)
    self.assertIsNotNone(acc.client)
    self.assertEqual(acc.warms, 1)
    self.assertIs(backend._CLIENT, acc.client)
    self.assertFalse(backend._STATUS.connected)
    self.assertIn('initial connect failed', backend._STATUS.last_error or '')

  def test_artifact_failure_returns_none(self):
    """真失败（工件/队列不可用）才返回 None —— modeld 仍安全跑纯 small。"""
    def boom(*args, **kwargs):
      raise RuntimeError('warp artifact missing')

    with self._patch_cfg(), mock.patch.object(backend, 'RemoteModelState', boom):
      self.assertIsNone(backend.load(1920, 1208, None))
    self.assertIn('load failed', backend._STATUS.last_error or '')

  def test_load_ignores_enabled_flag(self):
    """enabled=false 也必须构造加速器对象（开关改由 JoinedModel 运行期动态判）。"""
    with self._patch_cfg(), \
         mock.patch.object(backend, 'RemoteModelState', _FakeRemoteModelState), \
         mock.patch.object(backend.RemotePipelineClient, 'arm_reconnect',
                           lambda self, flag=True, *, immediate=False: None):
      acc = backend.load(1920, 1208, None)
    self.assertIsNotNone(acc)


if __name__ == '__main__':
  unittest.main()
