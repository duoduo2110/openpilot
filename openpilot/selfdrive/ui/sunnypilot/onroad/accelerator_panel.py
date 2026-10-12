"""Compact status panel for the optional external accelerator.

新鲜度是这个面板的第一语义：状态文件 `/data/models/accelerator_status.json`
只由 modeld 侧的 status_writer 以 ≤1Hz 落盘，一旦 modeld 没跑（停车、写盘失败、
链路进程被换掉），文件就停在最后一次的旧值上。旧值里的「就绪待切换 / 帧 0/0 /
延迟 0.0 ms」看起来跟链路正常一模一样，所以这里必须显式算 `now - ts`：超阈值
（`STALE_AFTER_S`）或 `ts` 缺失/`state` 非法时，只呈现陈旧标识并整体灰显，
所有数值换成 `--`，绝不把旧值当现状。
"""
import json
import threading
import time

import pyray as rl

from openpilot.selfdrive.ui.sunnypilot.accelerator_link_data import (NO_DATA_TEXT, NO_VALUE, frame_text,
                                                                     freshness, latency_text,
                                                                     link_mode_text)
from openpilot.system.ui.lib.application import FontWeight, gui_app
from openpilot.system.ui.lib.text_measure import measure_text_cached
from openpilot.system.ui.widgets import Widget


CONFIG_PATH = "/data/models/accelerator.json"
STATUS_PATH = "/data/models/accelerator_status.json"
# 行车界面的短标签：首行前面已经带「外置/本地」前缀，不必再重复"外置"二字。
# 键集合必须与 accelerator_link_data.VALID_STATES 一致（那里才是 state 合法性的
# 判据），这里只负责 key → 文案。
STATE_LABELS = {
  "joining": "连接中", "running": "运行中", "retrying": "重试中",
  "unavailable": "不可用", "ready": "就绪待切换",
}
# 读盘节流（秒）。陈旧文案里的「N 分钟前」要能持续走动，2Hz 足够且不刷盘
REFRESH_INTERVAL = 0.5
TEXT_SIZE = 20
LINE_HEIGHT = 29
PADDING = 12
MIN_WIDTH = 366
MAX_WIDTH = 560
RIGHT_MARGIN = 24
TOP_OFFSET = 125


def _read_json(path):
  try:
    with open(path) as f:
      value = json.load(f)
    return value if isinstance(value, dict) else None
  except (OSError, ValueError, TypeError):
    return None


def _is_number(value):
  """有限且量级合理的实数（bool 不算数）。字面量比较天然排除 NaN/inf。"""
  return (isinstance(value, (int, float)) and not isinstance(value, bool)
          and -1e18 < value < 1e18)


def _count(value):
  """计数型字段 → int；非数值 → None（畸形值不当成 0 次超时）。"""
  return int(value) if _is_number(value) else None


def _seconds(value):
  """秒数型字段 → float；非数值 → None（保持 0.1s 精度，不被 _count 截断）。"""
  return float(value) if _is_number(value) else None


def _unavailable(marker=NO_DATA_TEXT):
  """陈旧/无数据时的显示值：只给标识，数值一律 `--`。"""
  return {"fresh": False, "marker": marker or NO_DATA_TEXT, "source": NO_VALUE, "state": NO_VALUE,
          "frames": NO_VALUE, "latency": NO_VALUE, "timeouts": NO_VALUE, "timeouts_value": None, "retry": ""}


def format_accelerator_data(config, status, now=None):
  """Return display values, or None when the feature is off.

  只有 `config.enabled` 非真才返回 None（不画面板）。状态文件缺失或畸形一律返回
  「无数据」灰显值，而不是 None——功能开着却没有数据，本身就该被看见。
  整个函数体被 try/except 包住，任何字段缺失/类型异常都不会抛到 UI 线程。
  """
  if not isinstance(config, dict) or not config.get("enabled"):
    return None
  try:
    fresh, marker = freshness(status, now)
    if not fresh:
      return _unavailable(marker)
    data = status if isinstance(status, dict) else {}
    big_active = data.get("big_active")
    timeouts = _count(data.get("timeouts"))
    backoff = _seconds(data.get("backoff_s")) if data.get("state") == "retrying" else None
    # 链路三态文案（重连中(退避 Ns) / 已恢复·外置运行 / 本地兜底）由
    # accelerator_link_data 统一判定，这里只取结果，不另写一份判据。
    mode = link_mode_text(data, now)
    return {
      "fresh": True,
      "marker": "",
      "source": mode or ("外置" if big_active is True else "本地" if big_active is False else NO_VALUE),
      "state": STATE_LABELS.get(data.get("state"), NO_VALUE),
      "frames": frame_text(data.get("frames_ok"), data.get("frames_sent")),
      "latency": latency_text(data.get("p50_ms"), data.get("p99_ms")),
      "timeouts": str(timeouts) if timeouts is not None else NO_VALUE,
      "timeouts_value": timeouts,
      "retry": f"退避 {backoff:.1f}s" if backoff is not None else "",
    }
  except Exception:
    return _unavailable()


class AcceleratorPanel(Widget):
  TEXT = rl.Color(245, 245, 245, 235)
  # 陈旧/无数据：整体压暗，让"这里没有可用数据"一眼可辨
  DIM = rl.Color(132, 139, 149, 235)
  # 陈旧标识用低饱和琥珀：比灰醒目，又不至于像故障红灯那样刺眼
  STALE = rl.Color(226, 176, 84, 235)
  ALARM = rl.Color(255, 90, 90, 255)

  def __init__(self):
    super().__init__()
    self._font = gui_app.font(FontWeight.MEDIUM)
    self._data = None
    self._lock = threading.Lock()
    self._last_read = 0.0

  def update(self):
    # Disk IO is rate-limited and done off the UI thread.
    now = time.monotonic()
    if now - self._last_read < REFRESH_INTERVAL:
      return
    self._last_read = now
    threading.Thread(target=self._refresh, daemon=True).start()

  def _refresh(self):
    # ts 是墙钟时间（epoch 秒），新鲜度必须用 time.time()，不是上面的 monotonic
    data = format_accelerator_data(_read_json(CONFIG_PATH), _read_json(STATUS_PATH), time.time())  # noqa: TID251
    with self._lock:
      self._data = data

  def _lines(self, data):
    """三行固定版式；陈旧时首行换成陈旧标识，数值全部 `--`。"""
    if data.get("fresh"):
      tail = f"  {data.get('retry')}" if data.get("retry") else ""
      return [f"{data.get('source')}  {data.get('state')}",
              f"帧 {data.get('frames')}  延迟 {data.get('latency')}",
              f"超时 {data.get('timeouts')}{tail}"]
    return [str(data.get("marker") or NO_DATA_TEXT),
            f"帧 {data.get('frames')}  延迟 {data.get('latency')}",
            f"超时 {data.get('timeouts')}"]

  def _render(self, rect: rl.Rectangle):
    with self._lock:
      data = self._data
    if not data:
      return
    fresh = bool(data.get("fresh"))
    lines = self._lines(data)
    # timeouts_value 只可能是 int/None；过一道 _count 让畸形值也进不到比较运算
    timeouts_value = _count(data.get("timeouts_value")) or 0
    # 版式宽度按最长一行兜底（陈旧文案可能比正常数值长），右边缘保持不变
    text_width = _count(max(measure_text_cached(self._font, line, TEXT_SIZE).x for line in lines))
    width = min(MAX_WIDTH, max(MIN_WIDTH, (text_width or 0) + PADDING * 2))
    x, y = rect.x + rect.width - RIGHT_MARGIN - width, rect.y + TOP_OFFSET
    height = LINE_HEIGHT * len(lines) + PADDING * 2
    rl.draw_rectangle_rounded(rl.Rectangle(x, y, width, height), 0.12, 6, rl.Color(0, 0, 0, 170))
    for index, line in enumerate(lines):
      color = self.TEXT if fresh else self.DIM
      if not fresh and index == 0:
        color = self.STALE
      elif fresh and index == 2 and timeouts_value > 0:
        color = self.ALARM
      measure_text_cached(self._font, line, TEXT_SIZE)
      rl.draw_text_ex(self._font, line, rl.Vector2(x + PADDING, y + PADDING + index * LINE_HEIGHT),
                      TEXT_SIZE, 0, color)
