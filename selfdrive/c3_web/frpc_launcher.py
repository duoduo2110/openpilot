#!/usr/bin/env python3
"""FRP client launcher — runs bundled frpc, auto-restarts on exit."""
import os, signal, subprocess, time
from pathlib import Path

FRPC_BIN = Path(__file__).parent / "frpc"
FRPC_CFG = Path(__file__).parent / "frpc.toml"

def kill_existing():
  """Kill any orphaned frpc processes from a previous launcher instance."""
  try:
    result = subprocess.run(["pgrep", "-f", str(FRPC_BIN)], capture_output=True, text=True)
    for pid in result.stdout.strip().split():
      try: os.kill(int(pid), signal.SIGTERM)
      except OSError: pass
  except Exception: pass

def main():
  # Ensure frpc is executable regardless of git permissions
  if not os.access(FRPC_BIN, os.X_OK):
    FRPC_BIN.chmod(FRPC_BIN.stat().st_mode | 0o111)

  kill_existing()
  cmd = [str(FRPC_BIN), "-c", str(FRPC_CFG)]
  while True:
    print(f"frpc_launcher: starting {cmd}", flush=True)
    proc = subprocess.Popen(cmd)
    proc.wait()
    print(f"frpc_launcher: exited code={proc.returncode}, restart in 5s", flush=True)
    time.sleep(5)

if __name__ == "__main__":
  main()
