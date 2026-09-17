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


def get_expected_signature(panda: Panda) -> bytes:
  try:
    fn = os.path.join(FW_PATH, panda.get_mcu_type().config.app_fn)
    return Panda.get_signature_from_firmware(fn)
  except Exception:
    cloudlog.exception("Error computing expected signature")
    return b""


def flash_panda(panda_serial: str) -> Panda:
  try:
    panda = Panda(panda_serial)
  except PandaProtocolMismatch:
    cloudlog.warning("detected protocol mismatch, reflashing panda")
    HARDWARE.recover_internal_panda()
    raise

  fw_signature = get_expected_signature(panda)
  internal_panda = panda.is_internal()
  hw_type = panda.get_type()

  # skip flashing if the detected device is deprecated from upstream
  if hw_type in Panda.DEPRECATED_DEVICES:
    cloudlog.warning(f"Panda {panda_serial} is deprecated (hw_type: {hw_type}), skipping flash...")
    return panda

  # Cross-check the image we would program against the type the device reports.
  # An STM32F4 (DOS/White/Black) must only ever receive the F4 image: writing H7
  # firmware into an STM32F4 would brick it, so refuse on any mismatch instead of
  # trusting a single mapping table.
  if hw_type in Panda.F4_DEVICES:
    expected_app_fn = "panda.bin.signed"
  elif hw_type in Panda.H7_DEVICES:
    expected_app_fn = "panda_h7.bin.signed"
  else:
    cloudlog.warning(f"Panda {panda_serial} unknown hw type (hw_type: {hw_type}), skipping flash...")
    return panda

  mcu_type = panda.get_mcu_type()
  app_fn = mcu_type.config.app_fn
  if app_fn != expected_app_fn:
    cloudlog.error(f"Panda {panda_serial} refusing to flash: hw type {hw_type} expects {expected_app_fn} but {mcu_type} maps to {app_fn}")
    return panda

  panda_version = "bootstub" if panda.bootstub else panda.get_version()
  panda_signature = b"" if panda.bootstub else panda.get_signature()
  cloudlog.warning(f"Panda {panda_serial} connected, hw_type: {hw_type}, image: {app_fn}, version: {panda_version}, signature {panda_signature.hex()[:16]}, expected {fw_signature.hex()[:16]}")

  # An unavailable expected signature means we cannot tell whether the on-device
  # firmware is current. Treat that as "do not flash", never as "out of date".
  if fw_signature == b"":
    cloudlog.warning(f"Panda {panda_serial} expected signature unavailable for {app_fn}, skipping flash...")
    return panda

  if panda.bootstub or panda_signature != fw_signature:
    cloudlog.info("Panda firmware out of date, update required")
    panda.flash()
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

  return panda


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

  count = 0
  no_internal_panda_count = 0
  recover_attempts = 0

  while not do_exit:
    try:
      count += 1
      cloudlog.event("pandad.flash_and_connect", count=count)

      # TODO: remove this in the next AGNOS
      # wait until USB is up before counting
      if time.monotonic() < 60.:
        no_internal_panda_count = 0

      # Handle missing internal panda.
      # Order matters: never reset a panda that has not finished enumerating.
      # Resetting too early pushes it into the ROM bootloader/DFU, after which
      # it takes much longer (or a full recovery) to come back online. This is
      # why a naive "reset every few seconds" loop leaves the device stuck at
      # "NO PANDA" forever.
      if no_internal_panda_count > 0:
        panda_serials: list[str] = Panda.list()
        if not panda_serials:
          cloudlog.info("Panda not found yet, waiting briefly for USB enumeration...")
          for _ in range(5):
            time.sleep(1)
            panda_serials = Panda.list()
            if len(panda_serials):
              cloudlog.info(f"Panda appeared after waiting: {panda_serials}")
              break

        if not panda_serials:
          # The panda may be in its bootstub phase: the USB device is present
          # but not yet openable while it validates the app and switches to it.
          # Only reset when the USB device is truly absent, since resetting
          # mid-bootstub just restarts the ~10 s boot sequence.
          try:
            usb_present = '3801' in subprocess.run(['lsusb'], capture_output=True, text=True).stdout
          except Exception:
            usb_present = False
          if usb_present:
            cloudlog.info("Panda USB present but not enumerable (bootstub->app?), waiting up to 30s...")
            for _ in range(60):
              time.sleep(0.5)
              panda_serials = Panda.list()
              if len(panda_serials):
                cloudlog.info(f"Panda appeared after waiting: {panda_serials}")
                break

        if not panda_serials:
          cloudlog.info("No pandas found, resetting internal panda")
          HARDWARE.reset_internal_panda()
          # The internal panda takes a few seconds to boot its app after a
          # reset. Wait for it to come back in normal (non-bootstub) mode
          # before deciding whether to flash.
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

          if not panda_serials:
            # Never force a board that already has firmware into the ROM
            # bootloader right away: recover() erases the app sector before
            # reflashing. Only a truly blank board needs it, and that is
            # detected after several normal resets fail to produce a panda.
            if not PandaDFU.list() and recover_attempts < 3:
              recover_attempts += 1
              cloudlog.warning(f"Panda did not appear after reset ({recover_attempts}/3), retrying normal reset...")
              continue
            recover_attempts = 0
            cloudlog.info("Panda missing after normal resets, entering ROM bootloader (recover)...")
            HARDWARE.recover_internal_panda()
            time.sleep(2)
      else:
        panda_serials = Panda.list()

      # Only touch DFU when the panda truly never came up in normal mode
      if not panda_serials:
        for serial in PandaDFU.list():
          cloudlog.info(f"Panda in DFU mode found, flashing recovery {serial}")
          PandaDFU(serial).recover()
          time.sleep(1)
        panda_serials = Panda.list()

      if len(panda_serials) == 0:
        no_internal_panda_count += 1
        continue

      cloudlog.info(f"{len(panda_serials)} panda(s) found, connecting - {panda_serials}")

      # Flash all connected pandas, then keep only the internal one
      pandas: list[Panda] = []
      for serial in panda_serials:
        pandas.append(flash_panda(serial))

      internal_pandas = [panda for panda in pandas if panda.is_internal()]
      if len(internal_pandas) == 0:
        for panda in pandas:
          panda.close()
        cloudlog.error("Internal panda is missing, trying again")
        no_internal_panda_count += 1
        continue

      # this build drives the comma three's single internal panda
      assert len(internal_pandas) == 1
      panda_serial = internal_pandas[0].get_usb_serial()
      for panda in pandas:
        panda.close()
      no_internal_panda_count = 0
    # TODO: wrap all panda exceptions in a base panda exception
    except (usb1.USBErrorNoDevice, usb1.USBErrorPipe):
      # a panda was disconnected while setting everything up. let's try again
      cloudlog.exception("Panda USB exception while setting up")
      continue
    except PandaProtocolMismatch:
      cloudlog.exception("pandad.protocol_mismatch")
      continue
    except Exception:
      cloudlog.exception("pandad.uncaught_exception")
      continue

    # run pandad against the internal panda
    os.environ['MANAGER_DAEMON'] = 'pandad'
    process = subprocess.Popen(["./pandad", panda_serial], cwd=os.path.join(BASEDIR, "openpilot/selfdrive/pandad"))
    process.wait()


if __name__ == "__main__":
  main()
