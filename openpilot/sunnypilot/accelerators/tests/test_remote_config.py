"""
Copyright (c) 2026-, sunnypilot contributors.

本文件是 Part of sunnypilot，基于 MIT License 发布。

accelerator.json 配置读写的离线单测：默认回落、损坏容错、字段钳制、
原子写（tmp + os.replace）与失败清理。全部在临时目录里跑，不碰 /data。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from openpilot.sunnypilot.accelerators.remote import config


class ConfigTests(unittest.TestCase):
  def setUp(self):
    self.dir = tempfile.TemporaryDirectory()
    self.path = os.path.join(self.dir.name, 'sub', 'accelerator.json')
    self.addCleanup(self.dir.cleanup)

  def test_missing_file_returns_safe_defaults(self):
    cfg = config.load(self.path)
    self.assertFalse(cfg['enabled'])
    self.assertEqual(cfg['address'], '192.168.43.2:5202')
    self.assertEqual(cfg['deadline_ms'], 60)
    self.assertEqual(cfg['pipeline_depth'], 1)
    self.assertFalse(config.enabled(self.path))

  def test_save_load_roundtrip_creates_parent_dirs(self):
    ok = config.save({'enabled': True, 'address': '10.0.0.2:6000', 'deadline_ms': 75, 'pipeline_depth': 2},
                     self.path)
    self.assertTrue(ok)
    cfg = config.load(self.path)
    self.assertTrue(cfg['enabled'])
    self.assertEqual(cfg['address'], '10.0.0.2:6000')
    self.assertEqual(cfg['deadline_ms'], 75)
    self.assertEqual(cfg['pipeline_depth'], 2)
    self.assertTrue(config.enabled(self.path))
    self.assertEqual(config.deadline_s(self.path), 0.075)

  def test_payload_compression_defaults_off_and_roundtrips(self):
    self.assertFalse(config.load(self.path)['payload_compression'])
    self.assertTrue(config.save({'enabled': True, 'payload_compression': True}, self.path))
    self.assertTrue(config.load(self.path)['payload_compression'])
    # 非 bool 值回落安全默认（关闭）
    os.makedirs(os.path.dirname(self.path), exist_ok=True)
    with open(self.path, 'w', encoding='utf-8') as f:
      json.dump({'payload_compression': 'yes'}, f)
    self.assertFalse(config.load(self.path)['payload_compression'])

  def test_corrupt_json_returns_defaults(self):
    os.makedirs(os.path.dirname(self.path), exist_ok=True)
    with open(self.path, 'w', encoding='utf-8') as f:
      f.write('{not json at all')
    cfg = config.load(self.path)
    self.assertFalse(cfg['enabled'])
    self.assertEqual(cfg['deadline_ms'], 60)

  def test_non_dict_json_returns_defaults(self):
    os.makedirs(os.path.dirname(self.path), exist_ok=True)
    with open(self.path, 'w', encoding='utf-8') as f:
      json.dump([1, 2, 3], f)
    self.assertFalse(config.load(self.path)['enabled'])

  def test_field_clamping_and_type_fallback(self):
    os.makedirs(os.path.dirname(self.path), exist_ok=True)
    with open(self.path, 'w', encoding='utf-8') as f:
      json.dump({'enabled': 'yes', 'deadline_ms': 99999, 'pipeline_depth': 0,
                 'address': '   ', 'warp_path': 123}, f)
    cfg = config.load(self.path)
    self.assertFalse(cfg['enabled'])            # 非 bool → 默认 False
    self.assertEqual(cfg['deadline_ms'], 2000)  # 上限钳制
    self.assertEqual(cfg['pipeline_depth'], 1)  # 下限钳制
    self.assertEqual(cfg['address'], '192.168.43.2:5202')  # 空白 → 默认
    self.assertEqual(cfg['warp_path'], '/data/models/remote_warp.pkl')

  def test_unknown_fields_preserved(self):
    config.save({'enabled': True, 'signature': 'ab' * 32, 'future_knob': [1, 2]}, self.path)
    cfg = config.load(self.path)
    self.assertEqual(cfg['signature'], 'ab' * 32)
    self.assertEqual(cfg['future_knob'], [1, 2])

  def test_write_is_atomic_and_leaves_no_temp_files(self):
    self.assertTrue(config.save({'enabled': True}, self.path))
    self.assertTrue(config.save({'enabled': False}, self.path))
    leftovers = [n for n in os.listdir(os.path.dirname(self.path)) if n.startswith('.accelerator-')]
    self.assertEqual(leftovers, [])
    self.assertEqual(os.listdir(os.path.dirname(self.path)), ['accelerator.json'])

  def test_failed_replace_keeps_old_file_and_cleans_temp(self):
    self.assertTrue(config.save({'enabled': True, 'address': 'keep.me:1'}, self.path))
    with mock.patch.object(config.os, 'replace', side_effect=OSError('disk full')):
      self.assertFalse(config.save({'enabled': False}, self.path))
    # 旧文件原封不动
    self.assertEqual(config.load(self.path)['address'], 'keep.me:1')
    leftovers = [n for n in os.listdir(os.path.dirname(self.path)) if n.startswith('.accelerator-')]
    self.assertEqual(leftovers, [])

  def test_save_empty_dir_path_failure_is_silent(self):
    # 目标目录是一个文件时无法创建目录：必须返回 False 而不是抛异常
    blocker = os.path.join(self.dir.name, 'blocker')
    with open(blocker, 'w', encoding='utf-8') as f:
      f.write('x')
    self.assertFalse(config.save({'enabled': True}, os.path.join(blocker, 'acc.json')))

  def test_sanitize_rejects_bool_for_numeric_fields(self):
    cfg = config.sanitize({'deadline_ms': True, 'pipeline_depth': False})
    self.assertEqual(cfg['deadline_ms'], 60)
    self.assertEqual(cfg['pipeline_depth'], 1)


if __name__ == '__main__':
  unittest.main()
