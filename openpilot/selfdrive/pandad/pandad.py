#!/usr/bin/env python3
# simple pandad wrapper that updates the panda first
import os
import usb1
import time
import signal
import subprocess

from panda import Panda, PandaDFU, PandaProtocolMismatch, FW_PATH
from openpilot.common.basedir import BASEDIR
from openpilot.common.params import Params
from openpilot.common.hardware import HARDWARE
from openpilot.common.swaglog import cloudlog

from openpilot.sunnypilot.selfdrive.pandad.rivian_long_flasher import flash_rivian_long


def get_expected_signature(panda: Panda) -> bytes:
  fn = os.path.join(FW_PATH, panda.get_mcu_type().config.app_fn)
  return Panda.get_signature_from_firmware(fn)

def flash_panda(panda_serial: str):
  panda = Panda(panda_serial)

  # skip flashing if the detected panda is not supported
  if panda.get_type() not in Panda.SUPPORTED_DEVICES:
    cloudlog.warning(f"Panda {panda_serial} is not supported (hw_type: {panda.get_type()}), skipping flash...")
    panda.close()
    return

  fw_signature = get_expected_signature(panda)
  internal_panda = panda.is_internal()

  panda_version = "bootstub" if panda.bootstub else panda.get_version()
  panda_signature = b"" if panda.bootstub else panda.get_signature()
  cloudlog.warning(f"Panda {panda_serial} connected, version: {panda_version}, signature {panda_signature.hex()[:16]}, expected {fw_signature.hex()[:16]}")

  if panda.bootstub or panda_signature != fw_signature:
    cloudlog.info("Panda firmware out of date, update required")
    try:
      panda.flash()
    except Exception:
      # the flasher path failed (e.g. the bootloader command was stalled), fall
      # back to re-entering the bootloader and recovering over DFU
      cloudlog.exception("flasher-based flash failed, falling back to DFU recover")
      panda.close()
      panda = Panda(panda_serial)
      panda.recover(reset=(not internal_panda))
    cloudlog.info("Done flashing")

  if panda.bootstub:
    bootstub_version = panda.get_version()
    cloudlog.info(f"Flashed firmware not booting, flashing development bootloader. {bootstub_version=}, {internal_panda=}")
    if internal_panda:
      HARDWARE.recover_internal_panda()
    panda.recover(reset=(not internal_panda))
    cloudlog.info("Done flashing bootstub")

  if panda.bootstub:
    cloudlog.info("Panda still not booting, exiting")
    raise AssertionError

  panda_signature = panda.get_signature()
  if panda_signature != fw_signature:
    cloudlog.info("Version mismatch after flashing, exiting")
    raise AssertionError

  panda.close()


def check_panda_support(panda_serials: list[str]) -> list[str]:
  spi_serials = set(Panda.spi_list())
  for serial in panda_serials:
    if serial in spi_serials:
      return [serial]

  for serial in panda_serials:
    panda = Panda(serial)
    is_internal = panda.is_internal()
    panda.close()
    if is_internal:
      return [serial]

  return []


def main() -> None:
  # signal pandad to close the relay and exit
  def signal_handler(signum, frame):
    cloudlog.info(f"Caught signal {signum}, exiting")
    nonlocal do_exit
    do_exit = True
    if process is not None:
      process.send_signal(signal.SIGINT)

  process = None
  do_exit = False
  signal.signal(signal.SIGINT, signal_handler)

  # check health for lost heartbeat
  try:
    for s in Panda.list():
      with Panda(s) as p:
        health = p.health()
        if p.is_internal() and health["heartbeat_lost"]:
          Params().put_bool("PandaHeartbeatLost", True, block=True)
          cloudlog.event("heartbeat lost", deviceState=health)
  except Exception:
    cloudlog.exception("pandad.uncaught_exception")

  # The comma three's internal panda is a dos, and it is on USB.
  usb_panda = HARDWARE.get_device_type() == "tici"

  count = 0
  recover_attempts = 0
  while not do_exit:
    try:
      cloudlog.event("pandad.flash_and_connect", count=count)
      if usb_panda:
        # The comma three's internal panda is a dos on USB, so unlike the H7 on
        # SPI it has to re-enumerate on USB after a reset. Only reset it when it
        # is actually missing instead of resetting on every pass.
        panda_serials = Panda.list()
        if not panda_serials:
          cloudlog.info("No pandas found, resetting internal panda")
          HARDWARE.reset_internal_panda()
          # The internal panda takes a few seconds to boot its app after a
          # reset. Wait for it to come back in normal (non-bootstub) mode before
          # deciding whether to flash.
          for _ in range(40):
            panda_serials = Panda.list()
            if len(panda_serials) == 1:
              try:
                with Panda(panda_serials[0]) as p:
                  if not p.bootstub:
                    break
              except Exception:
                pass
            time.sleep(0.5)
          if panda_serials:
            recover_attempts = 0
          else:
            # Never force a board that already has firmware into the ROM
            # bootloader right away: recover() erases the app sector before
            # reflashing. Only a truly blank board needs it, and that is
            # detected after several normal resets fail to produce a panda.
            if not PandaDFU.list() and recover_attempts < 3:
              recover_attempts += 1
              count += 1
              cloudlog.warning(f"Panda did not appear after reset ({recover_attempts}/3), retrying normal reset...")
              continue
            recover_attempts = 0
            cloudlog.info("Panda missing after normal resets, entering ROM bootloader (recover)...")
            HARDWARE.recover_internal_panda()
            time.sleep(2)
      elif (count % 2) == 0:
        HARDWARE.reset_internal_panda()
      else:
        HARDWARE.recover_internal_panda()
      count += 1

      # This loop was written for an H7 on SPI, which is addressable again
      # milliseconds after a reset, so it resets on every pass and never waits.
      # A dos has to re-enumerate on USB first. Without this the list below runs
      # while the panda is still coming up, the next pass drops it into DFU, and
      # the pass after that reflashes a panda that was never broken.
      if usb_panda:
        time.sleep(3)

      # Flash all Pandas in DFU mode
      for serial in PandaDFU.list():
        cloudlog.info(f"Panda in DFU mode found, flashing recovery {serial}")
        PandaDFU(serial).recover()
        time.sleep(1)

      panda_serials = Panda.list()
      if len(panda_serials):
        # custom flasher for xnor's Rivian Longitudinal Upgrade Kit
        flash_rivian_long(panda_serials)
        # find the internal supported panda (e.g. skip external Black Panda)
        panda_serials = check_panda_support(panda_serials)

        assert len(panda_serials) == 1
        cloudlog.info(f"{len(panda_serials)} panda found, connecting - {panda_serials}")
        flash_panda(panda_serials[0])

        # run real pandad
        os.environ['MANAGER_DAEMON'] = 'pandad'
        # give the python libusb handles a moment to fully release the USB
        # interface before handing off to the C++ pandad
        if usb_panda:
          time.sleep(2)
        process = subprocess.Popen(["./pandad", panda_serials[0]], cwd=os.path.join(BASEDIR, "openpilot/selfdrive/pandad"))
        process.wait()
    # TODO: wrap all panda exceptions in a base panda exception
    except (usb1.USBErrorNoDevice, usb1.USBErrorPipe):
      # a panda was disconnected while setting everything up. let's try again
      cloudlog.exception("Panda USB exception while setting up")
    except PandaProtocolMismatch:
      cloudlog.exception("pandad.protocol_mismatch")
    except Exception:
      cloudlog.exception("pandad.uncaught_exception")


if __name__ == "__main__":
  main()
