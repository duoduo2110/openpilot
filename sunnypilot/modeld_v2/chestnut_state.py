from functools import cached_property
import struct

import cereal.messaging as messaging
from cereal.messaging import PubMaster
from tinygrad.device import Device

from openpilot.common.swaglog import cloudlog


class ChestnutState:
  # only modeld can access chestnut
  def __init__(self, pm: PubMaster, big: bool):
    self.pm = pm
    self.big = big
    self.valid = True
    self.sends = 0
    self.metrics = {}

  @cached_property
  def power_limit(self) -> int:
    smu = Device["AMD"].iface.dev_impl.smu
    return smu._send_msg(smu.smu_mod.PPSMC_MSG_GetPptLimit, 0, read_back_arg=True, timeout=100)

  def send(self) -> None:
    msg = messaging.new_message('chestnutState')
    state = msg.chestnutState
    self.sends += 1
    if self.big and "AMD" in Device._opened_devices and self.sends % 100 == 1:
      try:
        smu = Device["AMD"].iface.dev_impl.smu
        smu._send_msg(smu.smu_mod.PPSMC_MSG_TransferTableSmu2Dram, smu.smu_mod.TABLE_SMU_METRICS, timeout=100)
        metrics = smu.read_table(smu.smu_mod.SmuMetricsExternal_t, smu.smu_mod.TABLE_SMU_METRICS).SmuMetrics
        self.metrics = {'tempC': metrics.AvgTemperature[smu.smu_mod.TEMP_HOTSPOT],
                        'memoryTempC': metrics.AvgTemperature[smu.smu_mod.TEMP_MEM],
                        'powerDrawW': metrics.AverageSocketPower,
                        'powerLimitW': self.power_limit,
                        'gpuUsagePercent': metrics.AverageGfxActivity,
                        'gpuClockMhz': metrics.AverageGfxclkFrequencyPostDs,
                        'fanSpeedRpm': metrics.AvgFanRpm}
        self.valid = True
      except Exception:
        if self.valid:
          cloudlog.exception("chestnut state read failed")
        self.valid = False
        self.metrics.clear()
    if self.big:
      for k, v in self.metrics.items():
        setattr(state, k, v)

    asm_valid = False
    if "AMD" in Device._opened_devices:
      try:
        # ASM runs on USB-C power, these still read without a gpu
        asm = Device["AMD"].iface.pci_dev.usb
        state.pcieLtssm = asm.read(0xB450, 1)[0]
        state.supplyVoltage, state.supplyCurrent = struct.unpack('<Hh', bytes(asm.usb.control_read(0xC0, 5))[:4])
        asm_valid = True
      except Exception:
        pass

    msg.valid = asm_valid and (not self.big or self.valid)
    self.pm.send('chestnutState', msg)