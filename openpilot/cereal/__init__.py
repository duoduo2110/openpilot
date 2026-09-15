import os
import sys
import capnp
from importlib.resources import as_file, files

capnp.remove_import_hook()

with as_file(files("openpilot.cereal")) as fspath:
  CEREAL_PATH = fspath.as_posix()
  # Load car.capnp first: log.capnp imports it, and this capnp build cannot
  # re-register a schema that was already pulled in transitively (doing so
  # aborts with "Duplicate ID"). Loading car up front lets log reuse it.
  car = capnp.load(os.path.join(CEREAL_PATH, "car.capnp"))
  log = capnp.load(os.path.join(CEREAL_PATH, "log.capnp"))
  custom = capnp.load(os.path.join(CEREAL_PATH, "custom.capnp"))

# The comma three's pinned opendbc does `from cereal import car`. In this nested
# layout the package is openpilot.cereal, so also publish it under the legacy
# top-level name. Without it opendbc falls back to loading car.capnp a second
# time, which this capnp build refuses ("Duplicate ID") and aborts the process.
sys.modules.setdefault("cereal", sys.modules[__name__])
