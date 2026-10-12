"""
Copyright (c) 2026-, sunnypilot contributors.

本文件是 Part of sunnypilot，基于 MIT License 发布。

modeld seam 的静态断言（只读源码 + AST，不需要真机）：
* `accelerators.prepare()` 必须出现在 `config_realtime_process` 之前；
* `accelerators.load(...)` 必须出现在 small 模型构造之后；
* SubMaster 已订阅 `selfdriveState`（门控需要）；
* `CHESTNUT or ACCEL` 时启用 chestnutState / ChestnutState；
* 原 Chestnut 永久降级 latch 逻辑保留（仍以 ChestnutActive 为条件）。
"""
from __future__ import annotations

import ast
import pathlib
import unittest

MODELD_PATH = pathlib.Path(__file__).resolve().parents[4] / 'openpilot' / 'sunnypilot' / 'modeld_v2' / 'modeld.py'


def _call_lineno(tree: ast.AST, attr_chain: str) -> list[int]:
  """返回匹配 `a.b.c(...)` 调用的行号列表。"""
  parts = attr_chain.split('.')
  lines: list[int] = []
  for node in ast.walk(tree):
    if not isinstance(node, ast.Call):
      continue
    cur = node.func
    names: list[str] = []
    ok = True
    while isinstance(cur, ast.Attribute):
      names.append(cur.attr)
      cur = cur.value
    if isinstance(cur, ast.Name):
      names.append(cur.id)
    else:
      ok = False
    if ok and list(reversed(names)) == parts:
      lines.append(node.lineno)
  return lines


class ModeldSeamTests(unittest.TestCase):
  @classmethod
  def setUpClass(cls):
    cls.source = MODELD_PATH.read_text(encoding='utf-8')
    cls.tree = ast.parse(cls.source)

  def test_prepare_before_realtime(self):
    prepare = _call_lineno(self.tree, 'accelerators.prepare')
    realtime = _call_lineno(self.tree, 'config_realtime_process')
    self.assertEqual(len(prepare), 1, 'prepare() 应恰好调用一次')
    self.assertEqual(len(realtime), 1)
    self.assertLess(prepare[0], realtime[0], 'prepare() 必须在 config_realtime_process 之前')

  def test_load_after_small_model(self):
    load = _call_lineno(self.tree, 'accelerators.load')
    self.assertTrue(load, '应调用 accelerators.load')
    small_lines = [n.lineno for n in ast.walk(self.tree)
                   if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'small_model' for t in n.targets)
                   and 'ModelState(' in ast.get_source_segment(self.source, n.value)]
    self.assertTrue(small_lines, '应构造 small_model')
    first_small = min(small_lines)
    for line in load:
      self.assertGreater(line, first_small, 'accelerators.load 必须在 small 模型构造之后')

  def test_joined_model_wired(self):
    self.assertIn('JoinedModel(', self.source)
    self.assertIn('model.update_runtime(sm, meta_main.frame_id, meta_main.timestamp_eof', self.source)

  def test_publish_context_wired_for_delayed_publish(self):
    """延迟一帧发布：发布必须优先使用输出所属帧的上下文，其次才回落当前帧。"""
    self.assertIn("getattr(model, 'output_context', None)", self.source)
    self.assertIn("publish_context['timestamp_eof']", self.source)
    self.assertIn("publish_context['lat_action_t']", self.source)
    self.assertIn("publish_context['v_ego']", self.source)
    self.assertIn("'camera_frame_id': frame_id", self.source)

  def test_selfdrivestate_subscribed(self):
    positions = [node.lineno for node in ast.walk(self.tree)
                 if isinstance(node, ast.Call) and _call_lineno(self.tree, 'SubMaster')]
    self.assertTrue(positions)
    sm_lines = [node.lineno for node in ast.walk(self.tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'SubMaster']
    self.assertTrue(sm_lines)
    subscribed = any('"selfdriveState"' in self.source.splitlines()[line - 1] for line in sm_lines)
    self.assertTrue(subscribed, 'SubMaster 必须订阅 selfdriveState')

  def test_chestnut_state_enabled_for_accel(self):
    self.assertIn('(CHESTNUT or ACCEL)', self.source)
    self.assertIn('ChestnutState(pm, model.chestnut) if (CHESTNUT or ACCEL) else None', self.source)

  def test_original_latch_preserved(self):
    self.assertIn('params.get_bool("ChestnutActive")', self.source)
    self.assertIn('"ChestnutModelError"', self.source)


if __name__ == '__main__':
  unittest.main()
