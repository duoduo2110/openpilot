"""外置大模型配置与状态 JSON 的纯解析逻辑（不依赖 raylib）。"""
import json
import math
import time


CONFIG_DEFAULTS = {
  "enabled": False,
  "address": "192.168.43.2:5202",
  "warp_path": "/data/models/remote_warp.pkl",
  "deadline_ms": 60,
  "pipeline_depth": 1,
  "debug_log": False,
}
LATENCY_BUDGET_MS = 60.0
STATE_LABELS = {
  "joining": "连接中",
  "running": "外置运行中",
  "retrying": "重试中",
  "unavailable": "不可用",
  "ready": "就绪待切换",
}
# state 的合法取值集合（`STATE_LABELS` 的键）。UI 侧只按 key 校验有效性，
# 不依赖具体文案，避免各处维护两份标签表时漏改。
VALID_STATES = frozenset(STATE_LABELS)

# 状态文件的新鲜度阈值（秒）。写入方 status_writer 至多每 1s 落盘一次
# （WRITE_INTERVAL），5s ≈ 5 个写入周期，对停车、写盘失败、modeld 没跑这些
# 场景都算宽松。超过它说明「已经没人维护这个文件了」——此时文件里的
# state/帧数/延迟全是旧值，绝不能当成当前链路状态呈现。
STALE_AFTER_S = 5.0
# 无数值时的统一占位符，区别于真实的 0
NO_VALUE = "--"
NO_DATA_TEXT = "无数据"


def _finite_float(value):
  """有限浮点数或 None。

  bool、超大整数（`float()`/`math.isfinite()` 对它抛 OverflowError）、
  NaN/inf、非数值一律 None —— 状态文件是外部写入的，任何畸形内容都不能
  让 UI 线程崩掉。
  """
  if isinstance(value, bool) or not isinstance(value, (int, float)):
    return None
  try:
    number = float(value)
  except (TypeError, ValueError, OverflowError):
    return None
  return number if math.isfinite(number) else None


def _count(value):
  """计数型字段：有限实数取整，否则 None。"""
  number = _finite_float(value)
  return int(number) if number is not None else None


def parse_json_object(raw: str | bytes | None) -> dict:
  """只接受 JSON 对象；损坏、空内容或其他类型统一返回空对象。"""
  if raw is None:
    return {}
  try:
    value = json.loads(raw)
  except (TypeError, ValueError, UnicodeDecodeError):
    return {}
  return value if isinstance(value, dict) else {}


def normalize_config(value: dict | None) -> dict:
  config = dict(CONFIG_DEFAULTS)
  if isinstance(value, dict):
    config.update(value)
  config["enabled"] = config.get("enabled") is True
  config["debug_log"] = config.get("debug_log") is True
  for key in ("address", "warp_path"):
    if not isinstance(config.get(key), str):
      config[key] = CONFIG_DEFAULTS[key]
  for key in ("deadline_ms", "pipeline_depth"):
    if not isinstance(config.get(key), (int, float)) or isinstance(config.get(key), bool) or not math.isfinite(config[key]):
      config[key] = CONFIG_DEFAULTS[key]
  return config


def normalize_status(value: dict | None) -> dict:
  """补齐状态字段，异常字段不传到绘图逻辑。"""
  status = dict(value) if isinstance(value, dict) else {}
  # ts 是新鲜度判定的唯一依据，缺失即视为无数据；畸形（NaN/inf/超大整数）归 None
  status["ts"] = _finite_float(status.get("ts"))
  for key in ("enabled", "present", "ready", "big_active", "connected"):
    status[key] = status.get(key) if isinstance(status.get(key), bool) else None
  state = status.get("state")
  status["state"] = state if isinstance(state, str) and state in VALID_STATES else None
  for key in ("address", "last_error"):
    status[key] = status.get(key) if isinstance(status.get(key), str) else None
  status["reconnect_armed"] = (status.get("reconnect_armed")
                               if isinstance(status.get("reconnect_armed"), bool) else None)
  for key in ("frames_sent", "frames_ok", "timeouts", "failures"):
    status[key] = _count(status.get(key))
  for key in ("p50_ms", "p95_ms", "p99_ms", "max_ms", "backoff_s",
              "reconnect_backoff_s", "reconnect_attempts", "recovered_at"):
    status[key] = _finite_float(status.get(key))
  if status.get("reconnect_attempts") is not None and status["reconnect_attempts"] < 0:
    status["reconnect_attempts"] = None
  recent = status.get("recent")
  status["recent"] = [
    {"t": _finite_float(point.get("t")), "total_ms": total}
    for point in recent if isinstance(point, dict)
    and (total := _finite_float(point.get("total_ms"))) is not None
  ] if isinstance(recent, list) else []
  return status


def status_age_s(status, now=None) -> float | None:
  """状态文件年龄（秒）；`ts` 缺失/非法/明显落在未来 → None。

  明显来自未来（超过 STALE_AFTER_S）判为不可信而不是"刚刚更新过"——设备
  时钟被改或 ts 被写坏时，宁可显示无数据也不显示正常。
  """
  stamp = _finite_float(status.get("ts")) if isinstance(status, dict) else None
  if stamp is None:
    return None
  # 墙钟：状态文件里的 ts 是 epoch 秒，必须与写盘方同一时钟域，
  # monotonic 与它不可比（ruff TID251 的建议对这里不成立）
  current = time.time() if now is None else _finite_float(now)  # noqa: TID251
  if current is None:
    return None
  age = current - stamp
  return None if age < -STALE_AFTER_S else max(0.0, age)


def format_age_s(age) -> str:
  """年龄压成短文案：秒 / 分钟 / 小时 / 天，避免陈旧很久后撑爆面板。"""
  value = _finite_float(age)
  if value is None or value < 0:
    return NO_VALUE
  if value < 60:
    return f"{int(value)}s"
  if value < 3600:
    return f"{int(value // 60)} 分钟"
  if value < 86400:
    return f"{int(value // 3600)} 小时"
  # 设备时钟被改坏时 age 会大到荒谬（几十万小时）。滚成"天"而不是继续涨位数，
  # 面板行宽才不会被一行超长数字顶破。
  return f"{int(value // 86400)} 天"


def freshness(status, now=None, *, max_age_s=STALE_AFTER_S) -> tuple[bool, str | None]:
  """状态新鲜度判定 → `(是否新鲜, 陈旧标识文案)`。

    * 状态文件缺失/为空、`state` 非法、`ts` 缺失 → `(False, "无数据")`
    * `now - ts` 超过 `max_age_s`                → `(False, "数据陈旧 · 12s 前")`
    * 其余                                       → `(True, None)`

  调用方据此决定「数值照常显示」还是「只显示陈旧标识 + 灰显」。任何畸形输入
  都落到「无数据」，绝不抛异常。
  """
  try:
    if not isinstance(status, dict) or not status:
      return False, NO_DATA_TEXT
    state = status.get("state")
    if not isinstance(state, str) or state not in VALID_STATES:
      return False, NO_DATA_TEXT
    age = status_age_s(status, now)
    if age is None:
      return False, NO_DATA_TEXT
    limit = _finite_float(max_age_s)
    limit = STALE_AFTER_S if limit is None else max(0.0, limit)
    if age > limit:
      return False, f"数据陈旧 · {format_age_s(age)}前"
    return True, None
  except Exception:
    return False, NO_DATA_TEXT


def frame_text(ok, sent) -> str:
  """帧计数文案。一帧都没发过 → `--`（区分「没测过」与「测了全丢」）。"""
  sent_value, ok_value = _count(sent), _count(ok)
  if sent_value is None or sent_value <= 0:
    return NO_VALUE
  return f"{ok_value if ok_value is not None else 0}/{sent_value}"


def latency_metric_text(value) -> str:
  """**单个**延迟指标的数值文案（不带单位）：缺失或 ≤0 → `--`。

  状态文件里"没测到"写的就是 `0.0`，与"真的是 0 毫秒"在文件里无法区分，
  所以一律当作没有测量值。多档位面板（p50/p95/p99/max）逐档用它，
  `latency_text()` 也建立在它之上 —— ≤0 的判定只存在这一处。
  """
  number = _finite_float(value)
  return NO_VALUE if number is None or number <= 0 else f"{number:.1f}"


def latency_text(p50, p99) -> str:
  """延迟文案。缺失或 ≤0 一律 `--`：0.0 表示"没测到"，不是零延迟。"""
  low, high = latency_metric_text(p50), latency_metric_text(p99)
  if low == NO_VALUE or high == NO_VALUE:
    return NO_VALUE
  return f"{low}/{high} ms"


def _count_text(value) -> str:
  """计数字段文案：非有限数 → `--`（区分"没测过"与"确实是 0 次"）。"""
  number = _count(value)
  return NO_VALUE if number is None else str(number)


def state_label(state: str | None) -> str:
  return STATE_LABELS.get(state, "--")


# -- 链路恢复三态文案 ---------------------------------------------------------
# 复用本模块已有的 `freshness()`/`format_age_s()` 判新鲜度，**不另写一份**：
# 三态只是「同一个新鲜状态」在不同链路阶段下的措辞。
#
#   重连中（退避 Ns）  链路断了，后台监督器正按 1s→2s→…→30s 退避重建
#   已恢复·外置运行    链路刚恢复（recovered_at 在 RECOVERED_HOLD_S 内）且本帧确由外置供数
#   本地兜底           其余一切「没在用外置」的情况（同帧回退本地小模型 / 等安全窗口）
#
# 注意 RECOVERED_TEXT 是**行车界面**用的紧凑写法（20s 短闪，挂在状态行里）；
# 设置页诊断面板要的是持续可见的 `PANEL_RECOVERED_TEXT`，判定表也不同，
# 见本文件末尾的「设置页链路面板」。
RECONNECTING_TEXT = "重连中"
RECOVERED_TEXT = "已恢复·外置运行"
LOCAL_FALLBACK_TEXT = "本地兜底"
# 「已恢复」高亮保持时长（秒）：恢复是瞬时事件，UI 只需短暂提示，随后回到常态文案
RECOVERED_HOLD_S = 20.0


def reconnect_backoff_s(status) -> float | None:
  """当前退避秒数：优先链路监督器的 `reconnect_backoff_s`（1→2→…→30s 阶梯），
  回落到换入退避 `backoff_s`；两者都没有正数 → None。

  写成一处是为了让「重连中(退避 Ns)」里的 N 在两个面板上口径一致，不会因为
  选了哪个字段而出现两个不同的数字。
  """
  if not isinstance(status, dict):
    return None
  for key in ("reconnect_backoff_s", "backoff_s"):
    value = _finite_float(status.get(key))
    if value is not None and value > 0:
      return value
  return None


def reconnecting_label(backoff=None) -> str:
  """重连中文案。退避秒数为正时附在括号里，否则只给「重连中」。

  全文（`重连中` / `（退避 Ns）`）只在这里拼一次，行车面板与设置面板共用，
  避免同一个意思在两处各写一遍而互相漂移。
  """
  seconds = _finite_float(backoff)
  if seconds is None or seconds <= 0:
    return RECONNECTING_TEXT
  return f"{RECONNECTING_TEXT}（退避 {seconds:.0f}s）"


def reconnecting_text(status, now=None) -> str:
  """链路是否处于「重连中」→ `重连中（退避 Ns）`；否则空串。

  触发条件是链路层监督器自己的账：`reconnect_armed` 为真且当前仍未连上。
  """
  if not isinstance(status, dict):
    return ""
  if not freshness(status, now)[0]:
    return ""
  armed = status.get("reconnect_armed")
  connected = status.get("connected")
  if armed is not True or connected is True:
    return ""
  return reconnecting_label(reconnect_backoff_s(status))


def recovered_text(status, now=None) -> str:
  """链路是否「刚恢复且正在用外置」→ `已恢复·外置运行`；否则空串。

  三个条件缺一不可：状态新鲜、`big_active` 为真、`recovered_at` 落在高亮窗口内。
  `big_active` 为真意味着**本帧确实由远端大模型出结果**（modeld 侧同帧语义），
  因此这一行不可能在「本地兜底」时误报为已恢复。
  """
  if not isinstance(status, dict):
    return ""
  if not freshness(status, now)[0]:
    return ""
  if status.get("big_active") is not True:
    return ""
  stamp = _finite_float(status.get("recovered_at"))
  if stamp is None or stamp <= 0:
    return ""
  current = time.time() if now is None else _finite_float(now)  # noqa: TID251
  if current is None:
    return ""
  age = current - stamp
  if age < -STALE_AFTER_S or age > RECOVERED_HOLD_S:
    return ""
  return RECOVERED_TEXT


def link_mode_text(status, now=None) -> str:
  """**行车状态行**用的链路三态（onroad 面板）。

  优先级：重连中 > 已恢复 > 本地兜底。

  注意这里刻意不用于设置页：行车中要的是"此刻发生了什么"（「已恢复」短闪
  RECOVERED_HOLD_S 后回到常态，免得一直亮着变成"本来就该这样"），而设置页
  是用户主动打开来确认的，要的是持续可见的结论 —— 见 `panel_status_text()`。
  """
  reconnecting = reconnecting_text(status, now)
  if reconnecting:
    return reconnecting
  if recovered_text(status, now):
    return RECOVERED_TEXT
  if isinstance(status, dict) and freshness(status, now)[0]:
    return LOCAL_FALLBACK_TEXT
  return ""


def latency_over_budget(value: float | int | None) -> bool:
  return isinstance(value, (int, float)) and value > LATENCY_BUDGET_MS


# -- 设置页链路面板 -----------------------------------------------------------
# 设置页这块面板是**诊断面板**，不是行车状态行，两者诉求不同：
#
#   行车状态行（`link_mode_text`，onroad 面板）
#                                 行驶中要的是"此刻发生了什么"，所以「已恢复」
#                                 只在 RECOVERED_HOLD_S 内短闪一次，免得一直亮着
#                                 变成"本来就该这样"。
#   设置页面板（本节）             用户是**主动打开来确认**的，因此只要这一帧
#                                 确实由外置供数，就持续显示「已恢复」——
#                                 打开设置页就能一眼确认断链后它自己接回来了。
#
# 两边共用本模块的 `freshness()`/`frame_text()`/`latency_*_text()`/`reconnect_*()`，
# 本节只负责「面板该怎么措辞 / 怎么上色」，不重写任何解析。
#
# 标题色标（tone）由本节给出，UI 只按 tone 挑颜色，不再自己判状态 ——
# 这是"判定只有一处"的最后一道闸。
TONE_STALE = "stale"              # 数据陈旧 / 无数据：整体灰显
TONE_RECONNECTING = "reconnecting"  # 正在自动重连：琥珀
TONE_RECOVERED = "recovered"      # 已恢复且正在外置运行：高亮绿
TONE_LOCAL = "local"              # 本地兜底：中性
TONE_STATE = "state"              # 兜底：按 state 原色
# 「已恢复」要求链路状态确实在跑（大模型在线 / 就绪待切换）
RECOVERED_STATES = frozenset({"running", "ready"})
# 设置页用的舒展写法（`·` 两侧留白，24px 下更透气）；与 RECOVERED_TEXT 同义，
# 但**只在这里定一份**，面板不许自己拼字。
PANEL_RECOVERED_TEXT = "已恢复 · 外置运行"
# 设置页用的固定说明文案（外置断链时回退本地、后台自己重连，界面把这件事说出来）
PANEL_HINT = "外置不可用时自动回退本地小模型；断链后按 1s→30s 退避自动重连，无需手动干预。"
# 面板曲线最多画尾部多少个点（status_writer 攒 300 个，全画反而糊成一片）
SERIES_LIMIT = 48


def panel_status_text(status, now=None) -> tuple[str, str]:
  """设置页面板的状态标题 → `(文案, tone)`。绝不抛异常。

  判定优先级（先"数据可不可信"，再看链路走到哪一步）：

  | 条件                                | 文案                        | tone           |
  |-------------------------------------|-----------------------------|----------------|
  | 不新鲜（陈旧 / ts 缺失 / state 非法）| `数据陈旧 · N 分钟前` / `无数据` | `stale`        |
  | 监督器在退避 / `state=retrying` / 退避 > 0 | `重连中（退避 Ns）`   | `reconnecting` |
  | `big_active` 为真且状态在跑          | `已恢复 · 外置运行`         | `recovered`    |
  | `big_active` 为假                    | `本地兜底`                   | `local`        |
  | 其余                                | `STATE_LABELS[state]`        | `state`        |

  「重连中」排在「已恢复」之前是刻意的：链路正在退避重建时，即使 `big_active`
  还停在上一帧的真值，也应该先告诉用户"在接了"，而不是报一个假的稳定态。
  陈旧一律压过所有链路结论 —— 没人维护状态文件时，任何"已恢复"都是旧值。
  """
  try:
    data = status if isinstance(status, dict) else {}
    fresh, marker = freshness(data, now)
    if not fresh:
      return marker or NO_DATA_TEXT, TONE_STALE
    state = data.get("state")
    backoff = reconnect_backoff_s(data)
    # 三个信号任一成立就算"正在自动重连"：监督器自己在退避、state 是
    # retrying、或换入退避 > 0。第三个信号覆盖了 supervised 标志还没来得及
    # 置位的那一小段窗口期。
    retrying = state == "retrying" or backoff is not None or bool(reconnecting_text(data, now))
    if retrying:
      return reconnecting_label(backoff), TONE_RECONNECTING
    if data.get("big_active") is True and (state == "running" or state == "ready"):
      return PANEL_RECOVERED_TEXT, TONE_RECOVERED
    if data.get("big_active") is False:
      return LOCAL_FALLBACK_TEXT, TONE_LOCAL
    return state_label(state), TONE_STATE
  except Exception:
    return NO_DATA_TEXT, TONE_STALE


def recovery_note_text(status, now=None) -> str:
  """「自动恢复」的一行遥测：断链后它自己接回来过几次、上次是什么时候。

  `recovered_at` 只在**真正重连成功**时打戳（首次握手不算，见 client 的
  `_recovery_epoch > 1`），所以它非零就等于"链路确实断过又自己接回来了"。
  `reconnect_attempts` 则是期间的重试次数。两个都拿不到时才说"待命" ——
  那句话本身就是给用户的承诺：断了不用手动干预。

  没有可汇报的事件时返回空串，由 UI 决定是否显示（不让一行空注释占版面）。
  `format_age_s()` 复用本模块已有的年龄文案。
  """
  try:
    data = status if isinstance(status, dict) else {}
    if not freshness(data, now)[0]:
      return ""
    attempts = _count(data.get("reconnect_attempts"))
    count = attempts if attempts is not None and attempts > 0 else 0
    stamp = _finite_float(data.get("recovered_at"))
    current = time.time() if now is None else _finite_float(now)  # noqa: TID251
    recovered = ""
    if stamp is not None and stamp > 0 and current is not None:
      delta = current - stamp
      if delta >= -STALE_AFTER_S:
        recovered = f"{format_age_s(max(0.0, delta))}前恢复"
    if count > 0:
      return f"自动恢复：已重连 {count} 次 · {recovered or '等待下一次恢复'}"
    if recovered:
      return f"自动恢复：链路已接回 · {recovered}"
    return "自动恢复：待命（断链后 1s→30s 退避自动重连）"
  except Exception:
    return ""


def _latency_series(status) -> list[float]:
  """`recent` → 只含正延迟的**数值序列**（画曲线用，尾部 `SERIES_LIMIT` 条）。

  非列表 / 非字典元素 / 缺失或 ≤0 的 `total_ms` 全部跳过：延迟为 0 的点是
  "没测到"，画上去会把曲线压到贴底，看着像"延迟极低"。这里就把序列压平，
  UI 侧只剩缩放和连线，不再有任何字典下标访问。
  """
  points = status.get("recent")
  if not isinstance(points, (list, tuple)):
    return []
  series = []
  for point in points:
    if not isinstance(point, dict):
      continue
    value = _finite_float(point.get("total_ms"))
    if value is not None and value > 0:
      series.append(value)
  return series[-SERIES_LIMIT:]


def _positive(value) -> float | None:
  """延迟取值：缺失或 ≤0 → None（"没测到"与"0 毫秒"在文件里不可区分）。"""
  number = _finite_float(value)
  return number if number is not None and number > 0 else None


def _panel_unavailable(config, marker=NO_DATA_TEXT) -> dict:
  """陈旧 / 无数据 / 任何异常时的面板展示值：只留标识，数值一律 `--`。"""
  enabled = isinstance(config, dict) and config.get("enabled") is True
  return {
    "fresh": False, "tone": TONE_STALE, "headline": marker or NO_DATA_TEXT,
    "state": None, "state_text": NO_VALUE, "note": "", "last_error": None,
    "config_enabled": enabled, "active": None, "active_text": NO_VALUE,
    "p50_ms": None, "p95_ms": None, "p99_ms": None, "max_ms": None,
    "latency_pair": NO_VALUE, "frames": NO_VALUE, "timeouts": NO_VALUE,
    "failures": NO_VALUE, "backoff_s": None, "backoff_text": NO_VALUE,
    "series": [], "empty_hint": "数据陈旧，不绘制曲线",
  }


def panel_display(config, status, now=None) -> dict:
  """设置页链路面板的一次性展示值（纯函数，不依赖 raylib，可离线单测）。

  一次算完，渲染帧只读字典：新鲜度、标题、三态、恢复遥测、四档延迟、帧数、
  计数、退避、曲线序列全部在这里定死。陈旧时数值统一 `--` 且曲线清空 ——
  绝不把旧值当现状呈现（面板历史上就栽在这一点上：不看新鲜度 + 无脑渲染
  `192.168.43.2:5202` + 把 `0.0` 延迟显示成 `0 ms`）。

  任何畸形输入都收敛成「无数据」灰显值，绝不抛到 UI 线程：本模块历史上因为
  `"x" in sm` / 下标访问在渲染帧抛过 `KeyError: 0`。
  """
  try:
    cfg = config if isinstance(config, dict) else {}
    data = status if isinstance(status, dict) else {}
    headline, tone = panel_status_text(data, now)
    fresh = tone != TONE_STALE
    # 配置目标来自 UI 自己写的 accelerator.json，与状态文件的新鲜度无关，
    # 陈旧时也要照常显示 —— 用户还要靠它确认"我确实开了外置"。
    config_enabled = cfg.get("enabled") is True
    if not fresh:
      return _panel_unavailable(cfg, headline)
    active = data.get("big_active")
    active_text = "外置大模型" if active is True else "本地小模型" if active is False else NO_VALUE
    last_error = data.get("last_error")
    backoff = reconnect_backoff_s(data)
    return {
      "fresh": True, "tone": tone, "headline": headline,
      "state": data.get("state"), "state_text": state_label(data.get("state")),
      "note": recovery_note_text(data, now),
      "last_error": last_error if isinstance(last_error, str) else None,
      "config_enabled": config_enabled,
      "active": active if isinstance(active, bool) else None, "active_text": active_text,
      "p50_ms": _positive(data.get("p50_ms")), "p95_ms": _positive(data.get("p95_ms")),
      "p99_ms": _positive(data.get("p99_ms")), "max_ms": _positive(data.get("max_ms")),
      "latency_pair": latency_text(data.get("p50_ms"), data.get("p99_ms")),
      "frames": frame_text(data.get("frames_ok"), data.get("frames_sent")),
      "timeouts": _count_text(data.get("timeouts")),
      "failures": _count_text(data.get("failures")),
      "backoff_s": backoff,
      "backoff_text": NO_VALUE if backoff is None else f"{backoff:g} s",
      "series": _latency_series(data), "empty_hint": "等待数据",
    }
  except Exception:
    return _panel_unavailable(config)
