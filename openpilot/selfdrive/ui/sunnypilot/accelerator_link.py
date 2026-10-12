"""外置大模型链路配置与状态展示。

本模块只做三件事：读写 `accelerator.json`、后台轮询 `accelerator_status.json`
并给两个调用方提供内存快照、以及 `AcceleratorLinkPanel` 的绘制。

**判定逻辑一律不在这里。** 新鲜度、三态文案、恢复遥测、延迟/帧数的
取值规则全部由 `accelerator_link_data` 判定（`panel_display()` /
`link_mode_text()`），本模块只负责取字典和画。理由是这台机器上这块 UI
出过三类错：把没人维护的状态文件旧值当现状渲染、把 bulk 模式下根本不存在的
`192.168.43.2:5202` 摆在界面上、把「没测到」的 `0.0` 延迟显示成 `0 ms`。
判据只有一份，这类错就没地方再长出来。
"""
import json
import os
import tempfile
import threading
import time

import pyray as rl

from openpilot.system.ui.lib.application import FontWeight
from openpilot.system.ui.widgets import Widget
from openpilot.system.ui.widgets.label import gui_label
from openpilot.selfdrive.ui.sunnypilot.accelerator_link_data import (LATENCY_BUDGET_MS, NO_VALUE, PANEL_HINT,
                                                                     TONE_LOCAL, TONE_RECONNECTING, TONE_RECOVERED,
                                                                     TONE_STALE, latency_metric_text, latency_over_budget,
                                                                     normalize_config, normalize_status, panel_display,
                                                                     panel_status_text, parse_json_object)


CONFIG_PATH = "/data/models/accelerator.json"
STATUS_PATH = "/data/models/accelerator_status.json"
POLL_INTERVAL = 0.2


def _read_file(path: str) -> dict:
  try:
    with open(path, "rb") as f:
      return parse_json_object(f.read())
  except (OSError, ValueError):
    return {}


def read_config() -> dict:
  return normalize_config(_read_file(CONFIG_PATH))


def write_config_field(key: str, value) -> None:
  """通过同目录临时文件和 rename 原子更新配置的单个字段；失败由调用方呈现。"""
  config = read_config()
  config[key] = value
  directory = os.path.dirname(CONFIG_PATH)
  fd, temporary_path = tempfile.mkstemp(prefix=".accelerator.", suffix=".tmp", dir=directory)
  try:
    with os.fdopen(fd, "w", encoding="utf-8") as f:
      json.dump(config, f, ensure_ascii=False, separators=(",", ":"))
      f.flush()
      os.fsync(f.fileno())
    os.replace(temporary_path, CONFIG_PATH)
  except Exception:
    try:
      os.unlink(temporary_path)
    except OSError:
      pass
    raise


def write_enabled(enabled: bool) -> None:
  write_config_field("enabled", bool(enabled))


def write_debug_log(enabled: bool) -> None:
  """切换远程模型调试日志（accelerator.json 的 debug_log）；默认关闭。"""
  write_config_field("debug_log", bool(enabled))


class AcceleratorLinkMonitor:
  """后台低频读取 JSON，渲染帧只读取内存快照。"""
  def __init__(self):
    self._lock = threading.Lock()
    self._config = normalize_config(None)
    self._status = normalize_status(None)
    self._headline = ""
    self._thread = threading.Thread(target=self._poll, name="accelerator-link-monitor", daemon=True)
    self._thread.start()

  def _poll(self):
    while True:
      config = normalize_config(_read_file(CONFIG_PATH))
      status = normalize_status(_read_file(STATUS_PATH))
      # 标题文案在此算一次（墙钟口径与 freshness 一致），渲染帧只读字符串。
      # 设置项说明行与面板标题共用这一个字符串：两行紧挨着，说法必须一致，
      # 绝不能一个说"已恢复"另一个说"本地兜底"。
      try:
        headline = panel_status_text(status, time.time())[0]  # noqa: TID251
      except Exception:
        headline = ""
      with self._lock:
        self._config = config
        self._status = status
        self._headline = headline
      time.sleep(POLL_INTERVAL)

  def snapshot(self) -> tuple[dict, dict]:
    with self._lock:
      return dict(self._config), dict(self._status)

  def headline(self) -> str:
    with self._lock:
      return self._headline


_monitor: AcceleratorLinkMonitor | None = None
_monitor_lock = threading.Lock()


def get_accelerator_snapshot() -> tuple[dict, dict]:
  global _monitor
  if _monitor is None:
    with _monitor_lock:
      if _monitor is None:
        _monitor = AcceleratorLinkMonitor()
  return _monitor.snapshot()


def get_accelerator_panel_headline() -> str:
  """面板标题文案（`已恢复 · 外置运行` / `重连中（退避 Ns）` / `本地兜底` / `数据陈旧 · N 前` / 空串=无数据）。

  设置项说明行用它（「当前：xxx。」），必须与紧邻下方面板的标题**同一句话**：
  用户不该在同一个页面上同时看到互相打架的说法。

  和 `get_accelerator_snapshot()` 一样自行惰性起监控线程，不依赖调用顺序 ——
  否则谁先调谁拿到空串，标题会莫名其妙地少一句。
  """
  get_accelerator_snapshot()
  if _monitor is None:
    return ""
  return _monitor.headline()


class AcceleratorLinkPanel(Widget):
  """链路诊断面板：新鲜度优先，标题一眼给出「已恢复 / 重连中 / 本地兜底」。

  三条硬约束，都是这台机器上真实踩出来的：

  * **不新鲜就别谈链路状态。** 状态文件只由 modeld 侧 ≤1Hz 落盘，一旦 modeld
    没跑（停车、写盘失败、链路进程被换掉），它就停在最后一次的旧值上；旧值里的
    「就绪待切换 / 帧 0/0 / 延迟 0.0 ms」和链路正常长得一模一样。所以新鲜度统一
    由 `panel_display()` 判，陈旧时整块灰显、数值 `--`、曲线清空。
  * **不渲染 address。** bulk（FunctionFS）模式下根本没有 IP/TCP 端点，
    `192.168.43.2:5202` 只是配置里的历史默认值，摆出来只会误导。
  * **判定只有一处。** 标题文案、色标、四档延迟、帧数、计数、曲线序列全部来自
    `accelerator_link_data.panel_display()`；本类只负责把字典画出来，不重写
    任何解析，也不用 `in` 去猜状态文件里有什么键（历史上在这里抛过 `KeyError: 0`）。
  """

  HEIGHT = 612
  BG = rl.Color(32, 36, 42, 255)
  MUTED = rl.Color(160, 169, 179, 255)
  TRACK = rl.Color(75, 82, 91, 255)
  GOOD = rl.Color(98, 205, 154, 255)
  WARNING = rl.Color(245, 185, 73, 255)
  BAD = rl.Color(235, 95, 95, 255)
  # 陈旧：整体压暗 + 低饱和琥珀标识（比灰醒目，又不像故障红灯那样刺眼）
  DIM = rl.Color(126, 133, 143, 255)
  STALE = rl.Color(214, 180, 116, 255)
  # 标题高亮底条（低透明度）：只在「已恢复 / 重连中」铺一条，让结论跳出来
  GOOD_WASH = rl.Color(98, 205, 154, 48)
  WARN_WASH = rl.Color(245, 185, 73, 48)
  # 曲线纵轴满量程（毫秒），与预算刻度共用同一把尺子
  SCALE_MS = 120.0

  def __init__(self):
    super().__init__()
    self.set_rect(rl.Rectangle(0, 0, 0, self.HEIGHT))
    config, status = get_accelerator_snapshot()
    self.config = config if isinstance(config, dict) else {}
    self.status = status if isinstance(status, dict) else {}
    self.write_error: str | None = None

  def update_snapshot(self, config, status, write_error: str | None):
    self.config = config if isinstance(config, dict) else {}
    self.status = status if isinstance(status, dict) else {}
    self.write_error = write_error

  def set_parent_rect(self, parent_rect: rl.Rectangle) -> None:
    super().set_parent_rect(parent_rect)
    self._rect.width = parent_rect.width

  def _label(self, x, y, width, text, size=23, color=rl.WHITE, align=rl.GuiTextAlignment.TEXT_ALIGN_LEFT):
    gui_label(rl.Rectangle(x, y, width, size + 10), text, font_size=size, color=color,
              font_weight=FontWeight.NORMAL, alignment=align)

  def _state_color(self, state):
    """`TONE_STATE` 兜底时的原色映射（与历史配色一致）。"""
    if state == "running" or state == "ready":
      return self.GOOD
    if state == "joining" or state == "retrying":
      return self.WARNING
    return self.BAD if state == "unavailable" else self.MUTED

  def _headline_style(self, tone, state):
    """`tone` → `(文字色, 底条色或 None)`。判据来自数据层，这里只做配色。"""
    if tone == TONE_RECOVERED:
      return self.GOOD, self.GOOD_WASH
    if tone == TONE_RECONNECTING:
      return self.WARNING, self.WARN_WASH
    if tone == TONE_STALE:
      return self.STALE, None
    if tone == TONE_LOCAL:
      return self.MUTED, None
    return self._state_color(state), None

  def _render_latency(self, label, value, x, y, width, dim=False):
    """一档延迟：文字 + 预算刻度 + 填充条。`value` 为 None（没测到）→ 只画刻度。"""
    self._label(x, y, 72, label, 20, self.MUTED)
    over = latency_over_budget(value)
    text = NO_VALUE if value is None else f"{latency_metric_text(value)} ms"
    self._label(x + 76, y, 96, text, 20, self.DIM if dim else (self.BAD if over else rl.WHITE))
    bar_x, bar_y, bar_w = x + 180, y + 9, max(40, width - 180)
    rl.draw_rectangle_rounded(rl.Rectangle(bar_x, bar_y, bar_w, 8), 0.5, 8, self.TRACK)
    budget_x = bar_x + bar_w * min(LATENCY_BUDGET_MS / self.SCALE_MS, 1.0)
    rl.draw_line(int(budget_x), int(bar_y - 5), int(budget_x), int(bar_y + 13), self.WARNING)
    if value is None:
      return
    fill_w = bar_w * min(max(value, 0.0) / self.SCALE_MS, 1.0)
    rl.draw_rectangle_rounded(rl.Rectangle(bar_x, bar_y, max(2, fill_w), 8), 0.5, 8,
                              self.DIM if dim else (self.BAD if over else self.GOOD))

  def _render_graph(self, series, empty_hint, x, y, width, height, dim=False):
    """最近延迟曲线。`series` 已是纯数值序列（数据层滤掉了非正延迟的点），
    这里只做缩放与连线，因此不存在任何字典下标访问。"""
    rl.draw_rectangle_rounded(rl.Rectangle(x, y, width, height), 0.08, 8, rl.Color(24, 28, 34, 255))
    budget_y = y + height * (1.0 - LATENCY_BUDGET_MS / self.SCALE_MS)
    rl.draw_line(int(x + 10), int(budget_y), int(x + width - 10), int(budget_y), self.WARNING)
    self._label(x + 12, y + 2, width - 24, f"{LATENCY_BUDGET_MS:g} ms 预算", 17, self.WARNING)
    if not series:
      self._label(x + 12, y + height / 2 - 14, width - 24, empty_hint, 21, self.MUTED,
                  rl.GuiTextAlignment.TEXT_ALIGN_CENTER)
      return
    points = []
    for index, value in enumerate(series):
      px = x + 12 + (width - 24) * index / max(1, len(series) - 1)
      py = y + height - 12 - (height - 24) * min(max(value, 0.0) / self.SCALE_MS, 1.0)
      points.append((int(px), int(py), value))
    healthy, sick = (self.DIM, self.DIM) if dim else (self.GOOD, self.BAD)
    for first, second in zip(points, points[1:], strict=False):
      over = latency_over_budget(first[2]) or latency_over_budget(second[2])
      rl.draw_line_ex(rl.Vector2(first[0], first[1]), rl.Vector2(second[0], second[1]), 3,
                      sick if over else healthy)
    for px, py, value in points:
      rl.draw_circle(px, py, 3, sick if latency_over_budget(value) else healthy)

  def _render(self, rect):
    x, y, width = rect.x, rect.y, rect.width
    inner = width - 48
    # 状态文件里的 ts 是墙钟 epoch 秒，新鲜度必须与写盘方同一时钟域（不是 monotonic）
    data = panel_display(self.config, self.status, time.time())  # noqa: TID251
    if not isinstance(data, dict) or data.get("tone") is None:
      # panel_display 自身已全程兜底并保证键齐全；这里只是最后一道保险，
      # 绝不让缺键冒到绘制代码里。
      data = panel_display({}, {})
    fresh = data.get("fresh") is True
    dim = not fresh
    body = self.DIM if dim else rl.WHITE

    rl.draw_rectangle_rounded(rect, 0.035, 8, self.BG)
    self._label(x + 24, y + 15, inner, "链路运行状态", 28)

    # 标题：唯一给「已恢复 / 重连中 / 本地兜底」上色的地方，绿/琥珀各铺一条底条
    headline = str(data.get("headline") or NO_VALUE)
    text_color, wash = self._headline_style(data.get("tone"), data.get("state"))
    if wash is not None:
      rl.draw_rectangle_rounded(rl.Rectangle(x + 18, y + 43, width - 36, 40), 0.5, 10, wash)
    self._label(x + 28, y + 50, inner, headline, 24, text_color)

    # 明细行：state 标签 + 自动恢复遥测（重连过几次、上次什么时候接回来的）
    detail = str(data.get("state_text") or NO_VALUE)
    note = data.get("note")
    if note:
      detail = f"{detail}  ·  {note}"
    self._label(x + 24, y + 86, inner, detail, 18, self.MUTED if fresh else self.DIM)

    # 错误优先；没有错误就把"断链会自动恢复"这件事直接说清楚
    error = self.write_error or data.get("last_error")
    if error:
      # 换行压平（这块只有一行高），超长截断
      message = str(error).replace("\n", " ")[:96]
      self._label(x + 24, y + 112, inner, f"错误：{message}", 17, self.BAD)
    else:
      self._label(x + 24, y + 112, inner, PANEL_HINT, 17, self.MUTED)

    self._label(x + 24, y + 140, inner, "延迟分布", 20, self.MUTED)
    pair = data.get("latency_pair") or NO_VALUE
    self._label(x + 128, y + 142, inner - 104, f"p50/p99  {pair}", 18, body,
                rl.GuiTextAlignment.TEXT_ALIGN_RIGHT)
    for offset, (label, key) in enumerate((("p50", "p50_ms"), ("p95", "p95_ms"),
                                          ("p99", "p99_ms"), ("最大", "max_ms"))):
      self._render_latency(label, data.get(key), x + 24, y + 169 + offset * 35, inner, dim)
    self._label(x + 24, y + 313, inner,
                f"成功帧  {data.get('frames') or NO_VALUE}    超时  {data.get('timeouts') or NO_VALUE}"
                + f"    失败  {data.get('failures') or NO_VALUE}", 19, body)
    self._label(x + 24, y + 344, inner,
                f"退避  {data.get('backoff_text') or NO_VALUE}    延迟预算  {LATENCY_BUDGET_MS:g} ms",
                18, self.MUTED)

    self._label(x + 24, y + 373, inner, "最近延迟", 20, self.MUTED)
    self._render_graph(data.get("series") or [], str(data.get("empty_hint") or NO_VALUE),
                       x + 24, y + 403, inner, 170, dim)

    source_text = "外置笔记本运行" if data.get("config_enabled") is True else "本地运行"
    self._label(x + 24, y + 578, inner,
                f"配置目标：{source_text}    当前生效：{data.get('active_text') or NO_VALUE}",
                17, self.MUTED if fresh else self.DIM)
