import pyray as rl
from openpilot.system.ui.lib.application import FONT_SCALE, font_fallback
from openpilot.system.ui.lib.multilang import multilang

_cache: dict[int, rl.Vector2] = {}


def measure_text_cached(font: rl.Font, text: str, font_size: int, spacing: float = 0) -> rl.Vector2:
  """Caches text measurements to avoid redundant calculations."""
  font = font_fallback(font)
  spacing = round(spacing, 4)
  # Enlarge Chinese (CJK fallback) measurement by 1.3x to match drawing
  if multilang.requires_font_fallback():
    font_size = font_size * 1.3
  key = hash((font.texture.id, text, font_size, spacing))
  if key in _cache:
    return _cache[key]

  result = rl.measure_text_ex(font, text, font_size * FONT_SCALE, spacing)  # noqa: TID251

  _cache[key] = result
  return result
