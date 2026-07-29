#!/usr/bin/env python3
import asyncio
from contextlib import asynccontextmanager
import hashlib
import io
import os
import re
import secrets
import tempfile
import threading
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
from aiohttp import web
from PIL import Image

EXPOSED_FILES = {"qcamera.ts", "fcamera.hevc", "dcamera.hevc", "ecamera.hevc"}
ROUTE_RE, SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.-]+$"), re.compile(r"^\d+$")
SEGMENT_NAME_RE = re.compile(r"^(.+)--(\d+)$")

def route_and_segment(name):
  """loggerd uses <route>--<final numeric segment>; split only at that suffix."""
  match = SEGMENT_NAME_RE.fullmatch(name)
  return match.groups() if match else None

def contained(root, path):
  try:
    root, path = Path(root).resolve(strict=True), Path(path).resolve(strict=True)
    path.relative_to(root)
    return path
  except (OSError, ValueError):
    return None

def preview_cache_key(path, stat=None):
  stat = stat or Path(path).stat()
  value = f"{Path(path).resolve()}:{stat.st_size}:{stat.st_mtime_ns}"
  return hashlib.sha256(value.encode()).hexdigest()

def remux_preview(source, destination):
  """Copy browser-compatible qcamera streams; never decode or transcode them."""
  import av
  source, destination = Path(source), Path(destination)
  input_container = output_container = None
  try:
    input_container = av.open(str(source))
    streams = [s for s in input_container.streams if s.type in ("video", "audio")]
    if not any(s.type == "video" for s in streams) or any(s.codec_context.name not in ({"h264"} if s.type == "video" else {"aac"}) for s in streams):
      raise ValueError("preview requires H.264/AAC")
    output_container = av.open(str(destination), "w", format="mp4", options={"movflags": "+faststart+frag_keyframe+empty_moov+default_base_moof"})
    output_streams = {}
    for stream in streams:
      output_streams[stream.index] = output_container.add_stream_from_template(stream)
    for packet in input_container.demux(streams):
      if packet.dts is not None:
        packet.stream = output_streams[packet.stream.index]
        output_container.mux(packet)
  finally:
    if output_container is not None: output_container.close()
    if input_container is not None: input_container.close()

async def connect_live_client(factory, timeout, retry=.1):
  """Retry blocking VisionIPC setup off the aiohttp event loop."""
  loop, deadline = asyncio.get_running_loop(), asyncio.get_running_loop().time() + timeout
  while True:
    client = None
    try:
      client = await asyncio.to_thread(factory)
      if await asyncio.to_thread(client.connect, False): return client
    except Exception:
      pass
    if client is not None:
      close = getattr(client, "close", None)
      if callable(close):
        try: close()
        except Exception: pass
    if loop.time() >= deadline: raise RuntimeError("camera unavailable")
    await asyncio.sleep(min(retry, max(0, deadline - loop.time())))

@asynccontextmanager
async def leased_live_client(lease, factory, timeout):
  lease.acquire()
  client = None
  try:
    client = await connect_live_client(factory, timeout)
    yield client
  finally:
    if client is not None:
      close = getattr(client, "close", None)
      if callable(close):
        try: close()
        except Exception: pass
    lease.release()

def vision_client_factory(camera):
  streams = {"road": "VISION_STREAM_ROAD", "driver": "VISION_STREAM_DRIVER", "wide_road": "VISION_STREAM_WIDE_ROAD"}
  if camera not in streams: raise KeyError(camera)
  from msgq.visionipc import VisionIpcClient, VisionStreamType
  stream = getattr(VisionStreamType, streams[camera])
  return lambda: VisionIpcClient("camerad", stream, conflate=True)

class RouteStore:
  def __init__(self, roots=None):
    if roots is None:
      from openpilot.system.hardware.hw import Paths
      roots = (Paths.log_root(), Paths.log_root_external())
    self.roots = [Path(p) for p in roots]

  def _segments(self):
    result = defaultdict(list)
    for root in self.roots:
      root = contained(root, root)
      if root is None: continue
      try: entries = list(root.iterdir())
      except OSError: continue
      for entry in entries:
        parsed, path = route_and_segment(entry.name), contained(root, entry)
        if parsed and path and path.is_dir() and ROUTE_RE.fullmatch(parsed[0]):
          result[parsed[0]].append((int(parsed[1]), parsed[1], path, root))
    return result

  @staticmethod
  def _info(number, path):
    files, newest, active = [], 0., False
    try: entries = list(path.iterdir())
    except OSError: entries = []
    for entry in entries:
      try:
        if entry.is_file() and entry.name in EXPOSED_FILES and contained(path, entry):
          stat = entry.stat(); files.append({"name": entry.name, "size": stat.st_size}); newest = max(newest, stat.st_mtime)
        active |= entry.name.endswith(".lock") or entry.name == "lock"
      except OSError: pass
    return {"id": number, "files": files, "cameras": [f["name"] for f in files], "size": sum(f["size"] for f in files), "mtime": newest, "active": active}

  def routes(self):
    out = []
    for route, segments in self._segments().items():
      info = [self._info(n, p) for _, n, p, _ in sorted(segments)]
      out.append({"id": route, "segments": len(info), "mtime": max((x["mtime"] for x in info), default=0), "size": sum(x["size"] for x in info), "active": any(x["active"] for x in info)})
    return sorted(out, key=lambda x: x["mtime"], reverse=True)

  def route(self, route):
    segments = self._segments().get(route) if ROUTE_RE.fullmatch(route) else None
    if not segments: return None
    info = [self._info(n, p) for _, n, p, _ in sorted(segments)]
    return {"id": route, "segments": info, "segment_count": len(info), "mtime": max(x["mtime"] for x in info), "size": sum(x["size"] for x in info), "active": any(x["active"] for x in info)}

  def file(self, route, segment, filename):
    if not ROUTE_RE.fullmatch(route) or not SEGMENT_RE.fullmatch(segment) or filename not in EXPOSED_FILES: return None
    for _, n, directory, root in self._segments().get(route, []):
      if n == segment: return contained(root, directory / filename)
    return None

class DriverViewLease:
  def __init__(self, params, grace=2.): self.params, self.grace, self.clients, self.owned, self.timer = params, grace, 0, False, None
  def acquire(self):
    self.clients += 1
    if self.timer: self.timer.cancel(); self.timer = None
    if self.clients == 1 and self.params.get_bool("IsOffroad") and not self.params.get_bool("IsDriverViewEnabled"):
      self.params.put_bool("IsDriverViewEnabled", True); self.owned = True
  def release(self):
    self.clients = max(0, self.clients - 1)
    if not self.clients: self.timer = asyncio.get_running_loop().call_later(self.grace, self.clear)
  def clear(self):
    self.timer = None
    if not self.clients and self.owned: self.params.put_bool("IsDriverViewEnabled", False); self.owned = False
  def close(self):
    if self.timer: self.timer.cancel()
    self.clients = 0; self.clear()

def jpeg_from_frame(frame, max_size=None, quality=None):
  y = np.asarray(frame.data[:frame.uv_offset], dtype=np.uint8).reshape((-1, frame.stride))[:frame.height, :frame.width]
  uv = np.asarray(frame.data[frame.uv_offset:], dtype=np.uint8).reshape((-1, frame.stride))[:frame.height // 2, :frame.width]
  u, v = uv[:, 0::2], uv[:, 1::2]
  u, v = np.repeat(np.repeat(u, 2, axis=0), 2, axis=1), np.repeat(np.repeat(v, 2, axis=0), 2, axis=1)
  yuv = np.dstack((y, u[:frame.height, :frame.width], v[:frame.height, :frame.width])).astype(np.int16)
  yuv[:, :, 1:] -= 128
  rgb = np.dot(yuv, np.array([[1., 1., 1.], [0., -.39465, 2.03211], [1.13983, -.58060, 0.]])).clip(0, 255).astype(np.uint8)
  image = Image.fromarray(rgb); image.thumbnail(max_size or live_dimensions())
  out = io.BytesIO(); image.save(out, "JPEG", quality=quality or jpeg_pillow_quality()); return out.getvalue()

def live_fps(value=None):
  try: return max(1, min(20, int(value if value is not None else os.getenv("C3_WEB_FPS", "15"))))
  except (TypeError, ValueError): return 15

LIVE_PRESETS = (
  {"preset": "smooth", "label": "流畅", "width": 640, "height": 480, "qscale": 3, "estimatedFps": 11},
  {"preset": "high", "label": "高清", "width": 960, "height": 720, "qscale": 2, "estimatedFps": 9},
  {"preset": "ultra", "label": "超清", "width": 1280, "height": 960, "qscale": 2, "estimatedFps": 5},
)

def live_dimensions(width=None, height=None):
  def dimension(value, env, default, low, high):
    try: return max(low, min(high, int(value if value is not None else os.getenv(env, default))) & ~1)
    except (TypeError, ValueError): return default
  return dimension(width, "C3_WEB_MAX_WIDTH", 960, 160, 1280), dimension(height, "C3_WEB_MAX_HEIGHT", 720, 120, 960)

def jpeg_qscale(value=None):
  """FFmpeg MJPEG quantizer: 2 is high quality; 15 is lowest supported quality."""
  try: return max(2, min(15, int(value if value is not None else os.getenv("C3_WEB_JPEG_QSCALE", "2"))))
  except (TypeError, ValueError): return 2

def jpeg_pillow_quality(qscale=None):
  """Approximate FFmpeg qscale for Pillow: qscale 2→90, qscale 5→75."""
  return max(25, min(95, 100 - 5 * jpeg_qscale(qscale)))

def live_preset(params):
  try:
    raw = params.get("C3WebLiveQuality")
    index = int(raw.decode("utf-8")) if isinstance(raw, bytes) else int(raw) if raw is not None else 1
  except (AttributeError, TypeError, ValueError): index = 1
  return LIVE_PRESETS[index] if 0 <= index < len(LIVE_PRESETS) else LIVE_PRESETS[1]

def live_config(params=None):
  preset = live_preset(params) if params is not None else LIVE_PRESETS[1]
  width = None if "C3_WEB_MAX_WIDTH" in os.environ else preset["width"]
  height = None if "C3_WEB_MAX_HEIGHT" in os.environ else preset["height"]
  qscale = None if "C3_WEB_JPEG_QSCALE" in os.environ else preset["qscale"]
  width, height = live_dimensions(width, height)
  return {"selected": preset["preset"], "width": width, "height": height, "qscale": jpeg_qscale(qscale)}

def live_config_response(params):
  return {"selected": live_config(params)["selected"], "options": [dict(option) for option in LIVE_PRESETS]}

class AvJpegEncoder:
  """Per-camera MJPEG encoder. NV12 is copied line-by-line to tolerate padded planes."""
  def __init__(self, max_size=None, qscale=None):
    import av
    self.av, self.codec, self.size, self.max_size, self.qscale, self.sample = av, None, None, max_size or live_dimensions(), jpeg_qscale(qscale), None

  def encode(self, frame):
    width, height = frame.width, frame.height
    scale = min(1, self.max_size[0] / width, self.max_size[1] / height)
    size = (max(2, int(width * scale) & ~1), max(2, int(height * scale) & ~1))
    raw = memoryview(frame.data); source_stride, uv_offset = frame.stride, frame.uv_offset
    nv12 = self.av.VideoFrame(*size, "nv12")
    y = np.frombuffer(raw, dtype=np.uint8, count=height * source_stride).reshape(height, source_stride)[:, :width]
    uv = np.frombuffer(raw, dtype=np.uint8, count=(height // 2) * source_stride, offset=uv_offset).reshape(height // 2, source_stride)[:, :width].reshape(height // 2, width // 2, 2)
    if self.sample is None or self.sample[0] != (width, height, size):
      self.sample = ((width, height, size), np.linspace(0, height - 1, size[1], dtype=int), np.linspace(0, width - 1, size[0], dtype=int), np.linspace(0, height // 2 - 1, size[1] // 2, dtype=int), np.linspace(0, width // 2 - 1, size[0] // 2, dtype=int))
    _, y_rows, y_cols, uv_rows, uv_cols = self.sample
    planes = ((nv12.planes[0], y[np.ix_(y_rows, y_cols)]), (nv12.planes[1], uv[np.ix_(uv_rows, uv_cols)].reshape(size[1] // 2, size[0])))
    for plane, pixels in planes:
      data = bytearray(plane.buffer_size)
      for row in range(pixels.shape[0]):
        data[row * plane.line_size:row * plane.line_size + size[0]] = pixels[row].tobytes()
      plane.update(bytes(data))
    if self.codec is None or self.size != size:
      if self.codec is not None: self.codec.close()
      self.codec = self.av.CodecContext.create("mjpeg", "w")
      self.codec.width, self.codec.height, self.codec.pix_fmt = *size, "yuvj420p"
      self.codec.options = {"qscale": str(self.qscale)}
      try: self.codec.global_quality = self.qscale * 118 # FF_QP2LAMBDA; honored by FFmpeg's MJPEG encoder.
      except (AttributeError, TypeError): pass
      self.codec.open(); self.size = size
    encoded = nv12.reformat(format="yuvj420p")
    try: encoded.quality = self.qscale * 118
    except (AttributeError, TypeError): pass
    packets = self.codec.encode(encoded)
    if not packets: raise RuntimeError("MJPEG encoder produced no frame")
    return bytes(packets[0])

class JpegEncoder:
  def __init__(self, config=None):
    config = config or live_config()
    self.max_size, self.qscale = (config["width"], config["height"]), config["qscale"]
    try: self.av = AvJpegEncoder(self.max_size, self.qscale)
    except Exception: self.av = None

  def encode(self, frame):
    if self.av is not None:
      try: return self.av.encode(frame)
      except Exception: self.av = None
    return jpeg_from_frame(frame, self.max_size, jpeg_pillow_quality(self.qscale))

  def close(self):
    if self.av and self.av.codec: self.av.codec.close()

class FrameBroadcaster:
  """One VisionIPC reader per camera; broadcasts JPEG frames to all WebSocket clients."""
  def __init__(self, camera, fps=None, client_factory=None, encoder_factory=None):
    self.camera, self.fps, self.client_factory = camera, live_fps(fps), client_factory
    self.encoder_factory, self.clients, self.latest_frame, self.error = encoder_factory or JpegEncoder, set(), None, None
    self.frames_encoded, self.frames_dropped, self.started_at = 0, 0, 0.
    self.thread, self.stop_event, self.loop, self.generation, self.terminal = None, None, None, 0, False

  def metrics(self):
    elapsed = max(0.001, asyncio.get_running_loop().time() - self.started_at) if self.started_at else 0.
    return {"frames": self.frames_encoded, "dropped": self.frames_dropped, "fps": round(self.frames_encoded / elapsed, 1) if elapsed else 0.}

  def subscribe(self):
    q = asyncio.Queue(maxsize=1); self.clients.add(q)
    if self.latest_frame:
      self._put_latest(q, self.latest_frame)
    elif self.error:
      self._put_latest(q, None)
    return q

  def unsubscribe(self, q): self.clients.discard(q)
  @property
  def has_clients(self): return bool(self.clients)

  async def start(self):
    if self.thread is None or not self.thread.is_alive():
      self.error = None
      self.frames_encoded, self.frames_dropped, self.terminal = 0, 0, False
      self.started_at, self.loop, self.stop_event = asyncio.get_running_loop().time(), asyncio.get_running_loop(), threading.Event()
      self.generation += 1
      self.thread = threading.Thread(target=self._worker, args=(self.generation,), name=f"c3-web-{self.camera}", daemon=True)
      self.thread.start()

  @staticmethod
  def _put_latest(q, value):
    while q.full():
      try: q.get_nowait()
      except asyncio.QueueEmpty: break
    q.put_nowait(value)

  def _publish(self, value):
    for q in list(self.clients):
      if q.full(): self.frames_dropped += 1
      self._put_latest(q, value)

  async def stop(self):
    thread, stop_event = self.thread, self.stop_event
    if thread is not None:
      stop_event.set()
      await asyncio.to_thread(thread.join)
      await asyncio.sleep(0)
      if self.thread is thread: self.thread = None

  def _on_frame(self, generation, jpeg):
    if generation != self.generation or self.terminal: return
    self.latest_frame, self.frames_encoded = jpeg, self.frames_encoded + 1
    self._publish(jpeg)

  def _on_terminal(self, generation, error):
    if generation != self.generation or self.terminal: return
    self.terminal, self.error = True, error
    self._publish(None)

  def _connect_worker_client(self, factory):
    deadline = time.monotonic() + float(os.getenv("C3_WEB_CAMERA_STARTUP", "5"))
    while not self.stop_event.is_set():
      client = None
      try:
        client = factory()
        if client.connect(False): return client
      except Exception:
        pass
      if client is not None:
        close = getattr(client, "close", None)
        if callable(close):
          try: close()
          except Exception: pass
      if time.monotonic() >= deadline: break
      self.stop_event.wait(.1)
    raise RuntimeError("camera unavailable")

  def _worker(self, generation):
    client = None
    encoder = None
    error = None
    try:
      factory = self.client_factory or (lambda: vision_client_factory(self.camera)())
      client = self._connect_worker_client(factory)
      encoder = self.encoder_factory()
      interval = 1 / self.fps
      next_frame = time.monotonic()
      while not self.stop_event.is_set():
        frame = client.recv(timeout_ms=max(50, int(interval * 1000)))
        if frame:
          jpeg = encoder.encode(frame)
          if jpeg:
            self.loop.call_soon_threadsafe(self._on_frame, generation, jpeg)
            next_frame += interval
            delay = next_frame - time.monotonic()
            if delay > 0: self.stop_event.wait(delay)
            else: next_frame = time.monotonic()
    except Exception as exc: error = str(exc)
    finally:
      if encoder is not None:
        close = getattr(encoder, "close", None)
        if callable(close):
          try: close()
          except Exception: pass
      if client:
        close = getattr(client, "close", None)
        if callable(close):
          try: close()
          except Exception: pass
      self.loop.call_soon_threadsafe(self._on_terminal, generation, error)

async def ws_sender(ws, queue, broadcaster):
  while not ws.closed:
    frame = await queue.get()
    if frame is None:
      if broadcaster.error: await ws.close(code=1011, message=b"camera unavailable")
      return
    await ws.send_bytes(frame)

async def ws_receiver(ws):
  async for _ in ws: pass

async def ws_session(ws, queue, broadcaster):
  sender, receiver = asyncio.create_task(ws_sender(ws, queue, broadcaster)), asyncio.create_task(ws_receiver(ws))
  done, pending = await asyncio.wait((sender, receiver), return_when=asyncio.FIRST_COMPLETED)
  for task in pending: task.cancel()
  await asyncio.gather(*pending, return_exceptions=True)
  for task in done: task.result()

async def live_ws(request):
  """WebSocket endpoint: pushes paced JPEG frames (15 FPS by default, up to 20)."""
  camera = request.match_info["camera"]
  if camera not in ("road", "driver", "wide_road"): raise web.HTTPNotFound()
  lease, broadcasters = request.app["lease"], request.app["broadcasters"]
  lease.acquire(); q = bc = ws = None
  try:
    ws = web.WebSocketResponse(heartbeat=30); await ws.prepare(request)
    if camera not in broadcasters: broadcasters[camera] = FrameBroadcaster(camera, encoder_factory=request.app["encoder_factory"])
    bc = broadcasters[camera]; await bc.start(); q = bc.subscribe()
    await ws_session(ws, q, bc)
  except asyncio.CancelledError: raise
  except (ConnectionError, OSError): pass
  finally:
    if q is not None and bc is not None:
      bc.unsubscribe(q)
      if not bc.has_clients: await bc.stop()
    lease.release()
  assert ws is not None
  return ws

async def route_response(request):
  result = request.app["store"].route(request.match_info["route"])
  if result is None: raise web.HTTPNotFound()
  return web.json_response(result)

async def health_response(request):
  return web.json_response({"ok": True})

async def status_response(request):
  return web.json_response({"ok": True, "routes": len(request.app["store"].routes()), "live": {camera: bc.metrics() for camera, bc in request.app["broadcasters"].items()}, "liveConfig": live_config(request.app["params"])})

async def live_config_get(request):
  return web.json_response(live_config_response(request.app["params"]))

async def live_config_post(request):
  try: preset = (await request.json()).get("preset")
  except (ValueError, AttributeError): raise web.HTTPBadRequest(text="invalid preset")
  options = {option["preset"]: index for index, option in enumerate(LIVE_PRESETS)}
  if preset not in options: raise web.HTTPBadRequest(text="invalid preset")
  if any(bc.has_clients for bc in request.app["broadcasters"].values()): raise web.HTTPConflict(text="disconnect live streams first")
  request.app["params"].put("C3WebLiveQuality", str(options[preset]))
  return web.json_response(live_config_response(request.app["params"]))

async def routes_response(request):
  return web.json_response(request.app["store"].routes())

async def file_response(request):
  path = request.app["store"].file(**request.match_info)
  if path is None: raise web.HTTPNotFound()
  response = web.FileResponse(path)
  if path.suffix == ".hevc": response.headers["Content-Disposition"] = f'{"inline" if request.query.get("inline") == "1" else "attachment"}; filename="{path.name}"'
  return response

async def preview_response(request):
  path = request.app["store"].file(request.match_info["route"], request.match_info["segment"], "qcamera.ts")
  if path is None: raise web.HTTPNotFound()
  try:
    key = preview_cache_key(path)
  except OSError as exc:
    raise web.HTTPNotFound() from exc
  output = request.app["preview_dir"] / f"{key}.mp4"
  if not output.is_file():
    temporary = output.with_suffix(".tmp")
    async with request.app["preview_semaphore"]:
      if not output.is_file():
        try:
          temporary.unlink(missing_ok=True)
          await asyncio.to_thread(remux_preview, path, temporary)
          temporary.replace(output)
        except FileNotFoundError as exc:
          temporary.unlink(missing_ok=True)
          raise web.HTTPNotFound() from exc
        except Exception as exc:
          temporary.unlink(missing_ok=True)
          raise web.HTTPServiceUnavailable(text="preview unavailable") from exc
  return web.FileResponse(output, headers={"Content-Type": "video/mp4"})

def create_app(roots=None, params=None, preview_dir=None):
  if params is None:
    from openpilot.common.params import Params
    params = Params()
  preview_dir = Path(preview_dir or Path(tempfile.gettempdir()) / "c3-web-preview"); preview_dir.mkdir(parents=True, exist_ok=True)
  app = web.Application(); app["store"] = RouteStore(roots); app["params"] = params; app["lease"] = DriverViewLease(params, float(os.getenv("C3_WEB_DRIVER_GRACE", "2"))); app["preview_dir"] = preview_dir; app["preview_semaphore"] = asyncio.Semaphore(max(1, int(os.getenv("C3_WEB_PREVIEW_JOBS", "2")))); app["broadcasters"] = {}; app["encoder_factory"] = lambda: JpegEncoder(live_config(params))
  app["sessions"] = {}  # token -> expiry_time

  @web.middleware
  async def auth_middleware(request, handler):
    if request.path == "/health" or request.path.startswith("/api/auth/"):
      return await handler(request)
    if not request.path.startswith("/api/"):
      return await handler(request)  # static files pass through
    token = request.cookies.get("c3_token")
    if token and token in request.app["sessions"] and time.time() < request.app["sessions"][token]:
      return await handler(request)
    return web.json_response({"error": "unauthorized"}, status=401)

  app.middlewares.append(auth_middleware)

  async def auth_verify(request):
    try: body = await request.json()
    except Exception: return web.json_response({"error": "invalid"}, status=400)
    pin = body.get("pin", "")
    correct = params.get("C3WebPin")
    if correct is None: correct = "0909"
    elif isinstance(correct, bytes): correct = correct.decode("utf-8")
    if str(pin) != str(correct):
      return web.json_response({"error": "wrong pin"}, status=401)
    token = secrets.token_hex(32); expiry = time.time() + 86400  # 24h
    request.app["sessions"][token] = expiry
    resp = web.json_response({"authenticated": True})
    resp.set_cookie("c3_token", token, max_age=86400, httponly=True, samesite="Lax")
    return resp

  async def auth_status(request):
    token = request.cookies.get("c3_token")
    ok = token is not None and token in request.app["sessions"] and time.time() < request.app["sessions"][token]
    return web.json_response({"authenticated": ok})

  app.router.add_get("/health", health_response)
  app.router.add_post("/api/auth/verify", auth_verify); app.router.add_get("/api/auth/status", auth_status)
  app.router.add_get("/api/status", status_response)
  app.router.add_get("/api/live/config", live_config_get); app.router.add_post("/api/live/config", live_config_post)
  app.router.add_get("/api/routes", routes_response); app.router.add_get("/api/routes/{route}", route_response)
  app.router.add_get("/api/file/{route}/{segment}/{filename}", file_response); app.router.add_get("/api/preview/{route}/{segment}", preview_response); app.router.add_get("/api/live/{camera}/ws", live_ws)
  static = Path(__file__).parent / "static"
  if static.is_dir():
    async def index_response(request): return web.FileResponse(static / "index.html")
    app.router.add_get("/", index_response); app.router.add_static("/", static, show_index=False)
  async def cleanup(app):
    for bc in app.get("broadcasters", {}).values(): await bc.stop()
    app["lease"].close()
  app.on_cleanup.append(cleanup); return app

def main():
  web.run_app(create_app(), host="0.0.0.0", port=int(os.getenv("C3_WEB_PORT", "8082")))

if __name__ == "__main__": main()
