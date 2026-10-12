"""
回归测试：防止「点开设置 → 模型」UI 崩溃类 bug 再次引入。

真实事故
--------
`openpilot/selfdrive/ui/sunnypilot/layouts/settings/models.py` 曾写：

    selfdrive_state_received = "selfdriveState" in ui_state.sm and ui_state.sm.alive["selfdriveState"]

cereal 的 ``SubMaster`` **没有实现 ``__contains__``**，Python 的 ``in`` 会退化成
``sm[0]`` → ``KeyError: 0``，于是点开「设置 → 模型」页面直接崩溃。
另外该 helper 一度漏写 ``@staticmethod``，又引发 ``TypeError``。

本文件从两个层面加守卫：
1. 静态守卫（不依赖 pyray/gui，任何环境都能跑）：AST 扫描 sunnypilot 全部 .py，
   禁止对 SubMaster 使用 ``"<service>" in <sm>`` / ``not in``。
2. 行为守卫（可导入该模块时）：用只提供 ``seen``/``alive`` 字典、且
   ``__getitem__``/``__contains__`` 故意抛错的假 SubMaster 调用被测方法，
   证明实现走的是只读字典路径而不是 ``in`` / ``[0]``。
"""

import ast
import types
import unittest
from pathlib import Path

# .../openpilot/selfdrive/ui/sunnypilot/tests/test_*.py
_TESTS_DIR = Path(__file__).resolve().parent
SUNNYPILOT_ROOT = _TESTS_DIR.parent          # .../openpilot/selfdrive/ui/sunnypilot
REPO_ROOT = _TESTS_DIR.parents[4]            # .../<repo root>


def _smish(node: ast.AST) -> bool:
  """右操作数是否疑似 cereal SubMaster（变量名 sm / *_sm / *submaster*）。"""
  if isinstance(node, ast.Attribute):
    name = node.attr.lower()
  elif isinstance(node, ast.Name):
    name = node.id.lower()
  else:
    return False
  return name == "sm" or name.endswith("_sm") or "submaster" in name


class TestNoSubMasterContainsGuard(unittest.TestCase):
  """静态守卫：源码里不允许出现 `x in <SubMaster>` 这类危险写法。"""

  def _scan(self):
    offenders = []
    for path in sorted(SUNNYPILOT_ROOT.rglob("*.py")):
      try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
      except SyntaxError:
        continue
      for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
          continue
        for op, comparator in zip(node.ops, node.comparators):
          if isinstance(op, (ast.In, ast.NotIn)) and _smish(comparator):
            rel = path.relative_to(REPO_ROOT)
            offenders.append(f"{rel}:{node.lineno} -> `{ast.unparse(node)}`")
    return offenders

  def test_no_membership_test_against_submaster(self):
    """SubMaster 没有 __contains__，`in`/`not in` 会退化成 sm[0] -> KeyError: 0。

    正确写法示例：``sm.seen.get("selfdriveState", False)`` /
    ``sm.alive.get("selfdriveState", False)``。
    注：注释与字符串字面量里的说明文字不会被 AST 识别为 Compare，天然排除。
    """
    offenders = self._scan()
    self.assertEqual(
      offenders, [],
      "检测到对 SubMaster 的危险成员判断（会触发 KeyError: 0 导致 UI 崩溃）：\n  "
      + "\n  ".join(offenders),
    )

  def test_guard_detects_known_bad_snippet(self):
    """自检：守卫本身能识别历史事故写法，避免守卫失效而“永远通过”。"""
    bad = ast.parse('received = "selfdriveState" in ui_state.sm\n')
    found = any(isinstance(n, ast.Compare) for n in ast.walk(bad))
    self.assertTrue(found)


# ---------------------------------------------------------------------------
# 行为守卫：仅在能成功导入被测模块时运行（缺 pyray 等 GUI 依赖则跳过）。
# ---------------------------------------------------------------------------
try:
  from openpilot.selfdrive.ui.sunnypilot.layouts.settings.models import ModelsLayout
  from openpilot.selfdrive.ui.ui_state import ui_state

  _IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - 取决于运行环境
  ModelsLayout = None
  ui_state = None
  _IMPORT_ERROR = exc


class _FakeSubMaster:
  """最小假 SubMaster：只暴露 seen/alive 只读字典。

  - ``__getitem__`` 抛 ``KeyError(0)``：精确复刻真实崩溃（``sm[0]``）。
  - ``__contains__`` 抛 ``AssertionError``：一旦实现误用 ``in`` 立即暴露。
  """

  def __init__(self, seen=None, alive=None, enabled=True):
    self.seen = dict(seen or {})
    self.alive = dict(alive or {})
    self.updated = {}
    self.valid = {}
    self._enabled = enabled

  def __getitem__(self, key):
    if key == "selfdriveState":
      return types.SimpleNamespace(enabled=self._enabled)
    raise KeyError(0)

  def __contains__(self, key):
    raise AssertionError("SubMaster 不支持 `in`，实现不得使用成员判断")


@unittest.skipIf(ModelsLayout is None, f"无法导入 models 模块（可能缺少 pyray/GUI 依赖）：{_IMPORT_ERROR}")
class TestAcceleratorSwitchBehavior(unittest.TestCase):
  """行为守卫：假 sm 下调用被测方法必须不抛异常且返回 bool。"""

  def setUp(self):
    self._orig_sm = ui_state.sm
    self._orig_is_offroad = ui_state.is_offroad

  def tearDown(self):
    ui_state.sm = self._orig_sm
    ui_state.is_offroad = self._orig_is_offroad

  def _install(self, sm, offroad):
    ui_state.sm = sm
    ui_state.is_offroad = lambda: offroad

  def test_engaging_no_selfdrive_state_returns_true(self):
    """未收到 selfdriveState（seen/alive 为空）时保守视为已启用，且不抛异常。"""
    self._install(_FakeSubMaster(), offroad=True)
    result = ModelsLayout._accelerator_engaging()
    self.assertIsInstance(result, bool)
    self.assertTrue(result)

  def test_engaging_driving_disengaged_returns_false(self):
    self._install(_FakeSubMaster(seen={"selfdriveState": True}, alive={"selfdriveState": True}, enabled=False),
                  offroad=False)
    result = ModelsLayout._accelerator_engaging()
    self.assertIsInstance(result, bool)
    self.assertFalse(result)

  def test_engaging_driving_engaged_returns_true(self):
    self._install(_FakeSubMaster(seen={"selfdriveState": True}, alive={"selfdriveState": True}, enabled=True),
                  offroad=False)
    self.assertTrue(ModelsLayout._accelerator_engaging())

  def test_switch_allowed_onroad_matches_engaging(self):
    self._install(_FakeSubMaster(seen={"selfdriveState": True}, alive={"selfdriveState": True}, enabled=False),
                  offroad=False)
    self.assertIsInstance(ModelsLayout._accelerator_switch_allowed(), bool)
    self.assertTrue(ModelsLayout._accelerator_switch_allowed())

    self._install(_FakeSubMaster(seen={"selfdriveState": True}, alive={"selfdriveState": True}, enabled=True),
                  offroad=False)
    self.assertFalse(ModelsLayout._accelerator_switch_allowed())

  def test_switch_allowed_offroad_is_true(self):
    """offroad 时始终允许切换，即便 sm 状态未知。"""
    self._install(_FakeSubMaster(), offroad=True)
    self.assertIs(ModelsLayout._accelerator_switch_allowed(), True)


if __name__ == "__main__":
  unittest.main()
