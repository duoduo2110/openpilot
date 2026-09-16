import os
import capnp
from importlib.resources import as_file, files

capnp.remove_import_hook()

with as_file(files("openpilot.cereal")) as fspath, as_file(files("opendbc")) as opendbc_path:
  CEREAL_PATH = fspath.as_posix()
  opendbc_import_path = os.path.join(os.path.realpath(opendbc_path.as_posix()), 'car')
  log = capnp.load(os.path.join(CEREAL_PATH, "log.capnp"), imports=[opendbc_import_path])
  custom = capnp.load(os.path.join(CEREAL_PATH, "custom.capnp"), imports=[opendbc_import_path])
  # The comma three's pinned opendbc does `from cereal import car`, so expose the
  # car schema loaded through the same import path log.capnp used (avoids a second
  # registration of car.capnp under a different path, which aborts with Duplicate ID).
  car = capnp.load(os.path.join(opendbc_import_path, "car.capnp"), imports=[opendbc_import_path])
