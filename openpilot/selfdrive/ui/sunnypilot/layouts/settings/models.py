"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import os
import re
import time
import threading
import pyray as rl

from openpilot.cereal import custom
from openpilot.sunnypilot.models.helpers import ACTIVE_BUNDLE_KEYS, get_selected_bundle, resolve_bundle_by_ref
from openpilot.common.constants import CV
from openpilot.selfdrive.ui.ui_state import device, ui_state
from openpilot.selfdrive.ui.sunnypilot.model_info import big_model_state, bundles_for_source, carrying_model, default_model_name, queued_name
from openpilot.system.ui.lib.multilang import tr
from openpilot.system.ui.lib.application import gui_app
from openpilot.system.ui.widgets import DialogResult, Widget
from openpilot.system.ui.widgets.confirm_dialog import alert_dialog, ConfirmDialog
from openpilot.system.ui.widgets.scroller_tici import Scroller
from openpilot.system.ui.widgets.toggle import ON_COLOR

from openpilot.sunnypilot.models.runners.constants import CUSTOM_MODEL_PATH
from openpilot.system.ui.sunnypilot.lib.styles import style
from openpilot.system.ui.sunnypilot.lib.utils import NoElideButtonAction, ScrollingButtonAction
from openpilot.system.ui.sunnypilot.widgets.list_view import ListItemSP, toggle_item_sp, option_item_sp
from openpilot.system.ui.sunnypilot.widgets.download_status import download_status_item
from openpilot.system.ui.sunnypilot.widgets.tree_dialog import TreeOptionDialog, TreeNode, TreeFolder
from openpilot.selfdrive.ui.sunnypilot.accelerator_link import (AcceleratorLinkPanel, get_accelerator_panel_headline,
                                                               get_accelerator_snapshot, write_debug_log,
                                                               write_enabled)

if gui_app.sunnypilot_ui():
  from openpilot.system.ui.sunnypilot.widgets.list_view import button_item_sp as button_item


class ModelsLayout(Widget):
  def __init__(self):
    super().__init__()
    self.model_manager = None
    self.model_dialog = None
    self._selection_source = None
    self._downloading = False
    self._verifying = False
    self._last_note = None
    self.last_cache_calc_time = 0
    self._accelerator_config, self._accelerator_status = get_accelerator_snapshot()
    self._accelerator_write_error = None
    self._accelerator_write_pending = False
    self._accelerator_debug_write_pending = False

    self._initialize_items()

    self.clear_cache_item.action_item.set_value(f"{self.calculate_cache_size():.2f} MB")
    for ctrl, key in [(self.lane_turn_value_control, "LaneTurnValue"), (self.delay_control, "LagdToggleDelay"), (self.camera_offset, "CameraOffset")]:
      ctrl.action_item.set_value(int(float(ui_state.params.get(key, return_default=True)) * 100))

    self._scroller = Scroller(self.items, line_separator=True, spacing=0)

  def _initialize_items(self):
    self.small_model_item = ListItemSP(
      title=tr("Small Model"),
      description="",
      action_item=ScrollingButtonAction(tr("SELECT")),
      callback=lambda: self._open_source_dialog("qcom")
    )

    self.big_model_item = ListItemSP(
      title=tr("Big Model"),
      action_item=ScrollingButtonAction(tr("SELECT")),
      callback=lambda: self._open_source_dialog("chestnut")
    )

    self.download_item = download_status_item(lambda: tr("Download") if self._downloading else tr("Model Status"))

    self.refresh_item = button_item(tr("Refresh Model List"), tr("REFRESH"), "",
                                    lambda: (ui_state.params.put("ModelManager_LastSyncTime", 0),
                                             ui_state.params.put("ModelManager_LastSyncTime_Chestnut", 0),
                                             gui_app.push_widget(alert_dialog(tr("Fetching Latest Models")))))

    self.clear_cache_item = ListItemSP(
      title=tr("Clear Model Cache"),
      description="",
      action_item=NoElideButtonAction(tr("CLEAR")),
      callback=self._clear_cache
    )

    self.cancel_download_item = button_item(lambda: tr("Cancel Verification") if self._verifying else tr("Cancel Download"),
                                            tr("Cancel"), "",
                                            lambda: ui_state.params.remove("ModelManager_DownloadRef"))

    self.lane_turn_value_control = option_item_sp(tr("Adjust Lane Turn Speed"), "LaneTurnValue", 500, 2000,
                                                  tr("Set the maximum speed for lane turn desires. Default is 19 mph."),
                                                  int(round(100 / CV.MPH_TO_KPH)), None, True, "", style.BUTTON_ACTION_WIDTH, None, True,
                                                  lambda v: f"{int(round(v / 100 * (CV.MPH_TO_KPH if ui_state.is_metric else 1)))}" +
                                                            f" {'km/h' if ui_state.is_metric else 'mph'}")

    self.lane_turn_desire_toggle = toggle_item_sp(tr("Use Lane Turn Desires"),
                                                  tr("If you're driving at 20 mph (32 km/h) or below and have your blinker on," +
                                                     " the car will plan a turn in that direction at the nearest drivable path. " +
                                                     "This prevents situations (like at red lights) where the car might plan the wrong turn direction."),
                                                  param="LaneTurnDesire")

    self.delay_control = option_item_sp(tr("Adjust Software Delay"), "LagdToggleDelay", 5, 50,
                                        tr("Adjust the software delay when Live Learning Steer Delay is toggled off. The default software delay value is 0.2"),
                                        1, None, True, "", style.BUTTON_ACTION_WIDTH, None, True, lambda v: f"{v / 100:.2f}s")

    self.lagd_toggle = toggle_item_sp(tr("Live Learning Steer Delay"), "", param="LagdToggle")

    self.camera_offset = option_item_sp(tr("Adjust Camera Offset"), "CameraOffset", -35, 35,
                                        tr("Virtually shift camera's perspective to move model's center to Left(+ values) or Right (- values)"),
                                        1, None, True, "", style.BUTTON_ACTION_WIDTH, None, True,
                                        lambda v: f"{v / 100:.2f} m")

    self.accelerator_item = ListItemSP(
      title="外置大模型 / Accelerator Link",
      description="停车未挂挡或未启用辅助驾驶时可切换；切换后无需重启，通常数秒内自动接入或回退。",
      action_item=ScrollingButtonAction("切换"),
      callback=self._toggle_accelerator,
      inline=False,
    )
    self.accelerator_item.action_item.set_value(
      "外置笔记本运行" if self._accelerator_config.get("enabled") else "本地运行",
      ON_COLOR if self._accelerator_config.get("enabled") else style.ITEM_TEXT_VALUE_COLOR,
    )
    # 调试日志开关：默认关闭；开启后远程链路关键事件异步写入 /data/accelerator/remote_debug.log
    self.accelerator_debug_item = ListItemSP(
      title="外置大模型调试日志",
      description="开启后把远程链路关键事件（连接/握手、每帧耗时、超时与降级原因、传输模式）写入设备 "
                  "/data/accelerator/remote_debug.log；有界轮转、只追加。默认关闭，关闭时零写盘。",
      action_item=ScrollingButtonAction("切换"),
      callback=self._toggle_accelerator_debug,
      inline=False,
    )
    self._update_accelerator_debug_item()
    self.accelerator_panel = AcceleratorLinkPanel()
    self.items = [self.small_model_item, self.big_model_item, self.accelerator_item, self.accelerator_debug_item,
                  self.accelerator_panel, self.cancel_download_item, self.download_item, self.refresh_item,
                  self.clear_cache_item, self.lane_turn_value_control, self.delay_control]

  def _update_lagd_description(self, lagd_toggle: bool):
    desc = tr("Enable this for the car to learn and adapt its steering response time. Disable to use a fixed steering response time. " +
              "Keeping this on provides the stock openpilot experience.")
    if lagd_toggle:
      desc += f"<br>{tr('Live Steer Delay:')} {ui_state.sm['lateralDelay'].lateralDelay:.3f} s"
    elif ui_state.CP is not None:
      sw = float(ui_state.params.get("LagdToggleDelay", "0.2"))
      cp = ui_state.CP.steerActuatorDelay
      desc += f"<br>{tr('Actuator Delay:')} {cp:.2f} s + {tr('Software Delay:')} {sw:.2f} s = {tr('Total Delay:')} {cp + sw:.2f} s"
    self.lagd_toggle.set_description(desc)

  @staticmethod
  def calculate_cache_size():
    cache_size = 0.0
    if os.path.exists(CUSTOM_MODEL_PATH):
      for file in os.listdir(CUSTOM_MODEL_PATH):
        try:
          cache_size += os.path.getsize(os.path.join(CUSTOM_MODEL_PATH, file))
        except OSError:
          continue
    return cache_size / (1024**2)

  def _clear_cache(self):
    def _callback(response):
      if response == DialogResult.CONFIRM:
        ui_state.params.put_bool("ModelManager_ClearCache", True)
        self.clear_cache_item.action_item.set_value(f"{self.calculate_cache_size():.2f} MB")

    dialog = ConfirmDialog(tr("This will delete ALL downloaded models from the cache except the currently active model. Are you sure?"),
                           tr("Clear Cache"), callback=_callback)
    gui_app.push_widget(dialog)

  def _toggle_accelerator(self):
    if not self._accelerator_switch_allowed() or self._accelerator_write_pending:
      return
    enabled = not self._accelerator_config.get("enabled", False)
    self._accelerator_write_pending = True
    self._accelerator_write_error = None

    def _write():
      try:
        write_enabled(enabled)
        self._accelerator_write_error = None
      except Exception as exc:
        self._accelerator_write_error = f"配置写入失败：{exc}"
      finally:
        self._accelerator_write_pending = False

    threading.Thread(target=_write, name="accelerator-config-write", daemon=True).start()

  def _update_accelerator_debug_item(self):
    on = self._accelerator_config.get("debug_log") is True
    self.accelerator_debug_item.action_item.set_value(
      "开启" if on else "关闭",
      ON_COLOR if on else style.ITEM_TEXT_VALUE_COLOR,
    )

  def _toggle_accelerator_debug(self):
    """切换调试日志；不依赖驾驶状态（只影响日志，不影响链路门禁）。"""
    if self._accelerator_debug_write_pending:
      return
    enabled = not (self._accelerator_config.get("debug_log") is True)
    self._accelerator_debug_write_pending = True
    self._accelerator_write_error = None

    def _write():
      try:
        write_debug_log(enabled)
        self._accelerator_write_error = None
      except Exception as exc:
        self._accelerator_write_error = f"配置写入失败：{exc}"
      finally:
        self._accelerator_debug_write_pending = False

    threading.Thread(target=_write, name="accelerator-debug-write", daemon=True).start()

  @staticmethod
  def _accelerator_state_received() -> bool:
    """是否已收到 selfdriveState。

    注意：cereal 的 SubMaster **没有实现 __contains__**，`"x" in sm` 会退化成
    `sm[0]` 并抛 `KeyError: 0`（真实崩溃事故）。因此必须用只读字典的 `.get()`。
    """
    sm = ui_state.sm
    try:
      return bool(sm.seen.get("selfdriveState", False)) and bool(sm.alive.get("selfdriveState", False))
    except Exception:
      return False

  @staticmethod
  def _accelerator_engaging() -> bool:
    """辅助驾驶是否已启用；无法判定时**保守视为已启用**（不可切换）。"""
    if not ModelsLayout._accelerator_state_received():
      return True
    try:
      return bool(ui_state.sm["selfdriveState"].enabled)
    except Exception:
      return True

  @staticmethod
  def _accelerator_switch_allowed() -> bool:
    """是否可切换外置/内置：offroad 直接允许；onroad 则需已收到 selfdriveState 且未启用辅助驾驶。"""
    if ui_state.is_offroad():
      return True
    return not ModelsLayout._accelerator_engaging()


  def _handle_bundle_download_progress(self):
    self.cancel_download_item.set_visible(False)
    self._downloading = False
    self._verifying = False
    self.download_item.set_visible(True)

    if (current_time := time.monotonic()) - self.last_cache_calc_time > 0.5:
      self.last_cache_calc_time = current_time
      self.clear_cache_item.action_item.set_value(f"{self.calculate_cache_size():.2f} MB")

    bundle = self.model_manager.selectedBundle if self.model_manager else None
    progresses = [model.artifact.downloadProgress for model in bundle.models if model.artifact.fileName] if bundle else []
    if not progresses or bundle.status not in (custom.ModelManagerSP.DownloadStatus.downloading,
                                               custom.ModelManagerSP.DownloadStatus.failed):
      self.download_item.action_item.update(name="", segments=self._slot_segments())
      return

    self.cancel_download_item.set_visible(ui_state.params.get("ModelManager_DownloadRef") is not None)
    if bundle.status == custom.ModelManagerSP.DownloadStatus.downloading:
      device._reset_interactive_timeout()

    state = self._download_row_state(progresses, bundle.internalName)
    if queued := queued_name(bundle.ref):
      state["name"] += f"  |  {queued} {tr('queued')}"
    self.download_item.action_item.update(**state)
    self._downloading = self.download_item.action_item.downloading
    ds = custom.ModelManagerSP.DownloadStatus
    self._verifying = any(getattr(p.status, 'raw', p.status) == ds.verifying for p in progresses)

  def _slot_segments(self):
    """small and big slots side by side; green marks the slot whose pick is actually
    driving (runner-matched, so a failed Default big greens neither slot), an empty
    slot shows its default."""
    big_state = big_model_state()
    carry_source, carry_internal, _ = carrying_model()
    segments = []
    for source, label in (("qcom", tr("small")), ("chestnut", tr("big"))):
      if segments:
        segments.append(("|", rl.GRAY, None, None))
      bundle = get_selected_bundle(ui_state.params, source)
      name = bundle.internalName if bundle else default_model_name(source)
      color = ON_COLOR if (source == carry_source and name == carry_internal) else rl.LIGHTGRAY
      name = "● " + name
      if source == "chestnut":
        if big_state == 'failed':
          color = rl.RED
        elif big_state == 'loading':
          color = rl.GOLD
      segments.append((label, rl.GRAY, None, None))
      segments.append((name, color, None, None))
    return segments

  @staticmethod
  def _set_item_note(item, text):
    # a description renders only while shown; hide before clearing or the
    # empty description keeps its visible state
    if text:
      item.set_description(text)
      item.show_description(True)
    else:
      item.show_description(False)
      item.set_description("")

  def _status_note(self) -> str:
    """The failover story for the Model Status row. One-way big -> small, and the
    fallback is runner-matched: a Default big can only fall back to the Default
    small (stock modeld), a custom big has no automatic fallback yet."""
    if not ui_state.chestnut_present:
      return ""
    big_bundle = get_selected_bundle(ui_state.params, "chestnut")
    big_name = big_bundle.internalName if big_bundle else default_model_name("chestnut")
    big_is_default = big_bundle is None
    fallback_name = default_model_name("qcom")
    state = big_model_state()
    if state == 'failed':
      if big_is_default:
        return tr("Big model unavailable, {} is driving until the next drive.").format(fallback_name)
      return tr("Big model unavailable until the next drive.")
    if state == 'loading':
      if big_is_default:
        return tr("{} drives until the big model is ready.").format(fallback_name)
      return tr("Getting the big model ready.")
    if big_is_default:
      return tr("{} will drive. If it fails during a drive, {} takes over until the next drive.").format(big_name, fallback_name)
    return tr("{} will drive when the chestnut is ready.").format(big_name)

  @staticmethod
  def _download_row_state(progresses, name: str) -> dict:
    """Maps a bundle's artifact progress to DownloadStatusAction.update kwargs."""
    # .raw: _DynamicEnum equals its int but does not hash like it
    statuses = {getattr(p.status, 'raw', p.status) for p in progresses}
    progress = sum(p.progress for p in progresses) / len(progresses)
    ds = custom.ModelManagerSP.DownloadStatus

    if ds.failed in statuses:
      # close.png is authored black and a tint cannot lift it, hence close2
      return {"name": name, "status_text": tr("download failed"), "text_color": rl.RED, "icon": "icons/close2.png"}
    if ds.verifying in statuses:
      return {"name": name, "downloading": True, "progress": progress, "status_text": tr("verifying")}
    if ds.downloading in statuses:
      return {"name": name, "downloading": True, "progress": progress}
    if statuses <= {ds.downloaded, ds.cached}:
      return {"name": name, "text_color": ON_COLOR, "icon": "icons/checkmark.png"}
    # circled_slash is authored grey; tinting it again only darkens it
    return {"name": name, "text_color": rl.GRAY, "icon": "icons/circled_slash.png", "icon_color": rl.WHITE}

  def _on_model_selected(self, result):
    if result != DialogResult.CONFIRM:
      self.model_dialog = None
      return
    selected_ref = self.model_dialog.selection_ref
    self.model_dialog = None
    if selected_ref == "Default":
      if self._selection_source in ACTIVE_BUNDLE_KEYS:
        ui_state.params.remove(ACTIVE_BUNDLE_KEYS[self._selection_source])
      return
    if selected_bundle := self._resolve_selected_bundle(selected_ref):
      ui_state.params.put("ModelManager_DownloadRef", selected_bundle.ref)

  def _resolve_selected_bundle(self, ref):
    source_bundles = {source: bundles_for_source(source) for source in ("qcom", "chestnut")}
    resolved = resolve_bundle_by_ref(ref, source_bundles)
    return resolved[0] if resolved else None

  @staticmethod
  def _bundle_to_node(bundle):
    return TreeNode(bundle.ref, {'display_name': bundle.displayName, 'short_name': bundle.internalName})

  def _get_folders(self, favorites, bundles):
    folders = {}
    for bundle in bundles:
      folders.setdefault(next((ov_ride.value for ov_ride in bundle.overrides if ov_ride.key == "folder"), ""), []).append(bundle)

    folders_list = []
    for folder, folder_bundles in sorted(folders.items(), key=lambda x: max((bundle.index for bundle in x[1]), default=-1), reverse=True):
      folder_bundles.sort(key=lambda bundle: bundle.index, reverse=True)
      name = folder + (f" - (Updated: {m.group(1)})" if folder_bundles and (m := re.search(r'\(([^)]*)\)[^(]*$', folder_bundles[0].displayName)) else "")
      folders_list.append(TreeFolder(name, [self._bundle_to_node(bundle) for bundle in folder_bundles]))

    if favorites and (fav_bundles := [bundle for bundle in bundles if bundle.ref in favorites]):
      folders_list.insert(0, TreeFolder("Favorites", [self._bundle_to_node(bundle) for bundle in fav_bundles]))
    return folders_list

  def _open_source_dialog(self, source):
    self._selection_source = source
    favs = ui_state.params.get("ModelManager_Favs")
    favorites = set(favs.split(';')) if favs else set()
    folders_list = self._source_folders(favorites, source)
    if not folders_list:
      gui_app.push_widget(alert_dialog(tr("No models are available for this hardware yet. Connect to the internet and refresh the model list.")))
      return
    self.model_dialog = TreeOptionDialog(tr("Select a Model"), folders_list, self._slot_active_ref(source), "ModelManager_Favs",
                                         get_folders_fn=lambda favs: self._source_folders(favs, source), on_exit=self._on_model_selected)
    gui_app.push_widget(self.model_dialog)

  def _source_folders(self, favorites, source):
    bundles = bundles_for_source(source)
    if not bundles:
      return []
    folders_list = [TreeFolder("", [TreeNode("Default", {'display_name': default_model_name(source)})])]
    folders_list.extend(self._get_folders(favorites, bundles))
    return folders_list

  @staticmethod
  def _slot_active_ref(source: str) -> str:
    bundle = get_selected_bundle(ui_state.params, source)
    return bundle.ref if bundle else "Default"

  def _update_state(self):
    advanced_controls: bool = ui_state.params.get_bool("ShowAdvancedControls")
    turn_desire: bool = ui_state.params.get_bool("LaneTurnDesire")
    live_delay: bool = ui_state.params.get_bool("LagdToggle")
    camera_offset: bool = ui_state.active_bundle is not None

    self.lane_turn_desire_toggle.action_item.set_state(turn_desire)
    self.lane_turn_value_control.set_visible(turn_desire and advanced_controls)
    self.lagd_toggle.action_item.set_state(live_delay)
    self.delay_control.set_visible(not live_delay and advanced_controls)
    new_step = int(round(100 / CV.MPH_TO_KPH)) if ui_state.is_metric else 100
    if self.lane_turn_value_control.action_item is not None and self.lane_turn_value_control.action_item.value_change_step != new_step:
      self.lane_turn_value_control.action_item.value_change_step = new_step
    self.camera_offset.set_visible(camera_offset)

    self._update_lagd_description(live_delay)
    self.model_manager = ui_state.sm["modelManagerSP"]
    self._handle_bundle_download_progress()

    carry_source, _, carry_display = carrying_model()
    for item, item_source in ((self.small_model_item, "qcom"), (self.big_model_item, "chestnut")):
      bundle = get_selected_bundle(ui_state.params, item_source)
      name = bundle.displayName if bundle else default_model_name(item_source)
      color = ON_COLOR if (item_source == carry_source and name == carry_display) else style.ITEM_TEXT_VALUE_COLOR
      item.action_item.set_value(name, color)

    note = self._status_note()
    if note != self._last_note:
      self._last_note = note
      self._set_item_note(self.download_item, note)

    offroad = ui_state.is_offroad()
    self.small_model_item.action_item.set_enabled(offroad)
    self.big_model_item.action_item.set_enabled(offroad)
    self.small_model_item.set_description("" if offroad else tr("Only available when vehicle is off, or always offroad mode is on"))
    self._accelerator_config, self._accelerator_status = get_accelerator_snapshot()
    engaging = ModelsLayout._accelerator_engaging()
    switch_allowed = self._accelerator_switch_allowed()
    accelerator_enabled = switch_allowed and not self._accelerator_write_pending
    self.accelerator_item.action_item.set_enabled(accelerator_enabled)
    self.accelerator_item.set_enabled(accelerator_enabled)
    self.accelerator_item.action_item.set_value(
      "写入中…" if self._accelerator_write_pending else
      "外置笔记本运行" if self._accelerator_config.get("enabled") else "本地运行",
      ON_COLOR if self._accelerator_config.get("enabled") else style.ITEM_TEXT_VALUE_COLOR,
    )
    description = "停车未挂挡或未启用辅助驾驶时可切换；切换后无需重启，通常数秒内自动接入或回退。"
    # 断链时明确告诉用户「正在自己重连（退避 N 秒）」/「已恢复 · 外置运行」/「本地兜底」，
    # 避免面板与状态行看起来跟「一切正常」一样。这里必须复用面板标题的同一句话 ——
    # 两行紧挨着，说法不一致比不说更让人怀疑。
    headline = get_accelerator_panel_headline()
    if headline:
      description += f" 当前：{headline}。"
    if engaging:
      description += " 行驶中已锁定。"
    elif not ModelsLayout._accelerator_state_received() and not offroad:
      description += " 等待辅助驾驶状态，暂不可切换。"
    self.accelerator_item.set_description(description)
    self.accelerator_item.show_description(not switch_allowed or self._accelerator_write_pending)
    self._update_accelerator_debug_item()
    self.accelerator_debug_item.action_item.set_enabled(not self._accelerator_debug_write_pending)
    self.accelerator_panel.update_snapshot(self._accelerator_config, self._accelerator_status,
                                           self._accelerator_write_error)

  def _render(self, rect):
    self._scroller.render(rect)

  def show_event(self):
    self._scroller.show_event()
    self._last_note = None  # re-expand the failover note every time the page opens
