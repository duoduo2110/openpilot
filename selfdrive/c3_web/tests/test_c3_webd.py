import asyncio
import os
import threading

from aiohttp import web

from openpilot.selfdrive.c3_web.c3_webd import DriverViewLease, FrameBroadcaster, JpegEncoder, RouteStore, connect_live_client, jpeg_pillow_quality, jpeg_qscale, leased_live_client, live_config, live_config_get, live_config_post, live_config_response, live_dimensions, live_fps, main, preview_cache_key, route_and_segment, vision_client_factory, ws_session


class FakeParams:
  def __init__(self, offroad=True, driver=False): self.values = {"IsOffroad": offroad, "IsDriverViewEnabled": driver, "C3WebLiveQuality": b"1"}
  def get(self, key): return self.values.get(key)
  def get_bool(self, key): return self.values.get(key, False)
  def put(self, key, value): self.values[key] = value.encode() if isinstance(value, str) else value
  def put_bool(self, key, value): self.values[key] = value


def test_route_listing_and_final_segment_split(tmp_path):
  assert route_and_segment("abc--def--12") == ("abc--def", "12")
  segment = tmp_path / "abc--def--12"; segment.mkdir()
  (segment / "qcamera.ts").write_bytes(b"x")
  (segment / "rlog.zst").write_bytes(b"secret")
  (segment / "upload.lock").touch()
  route = RouteStore([tmp_path]).route("abc--def")
  assert route["active"] and route["segments"][0]["files"] == [{"name": "qcamera.ts", "size": 1}]


def test_file_whitelist_and_symlink_escape(tmp_path):
  segment = tmp_path / "route--0"; segment.mkdir(); outside = tmp_path.parent / "outside.ts"; outside.write_bytes(b"x")
  (segment / "qcamera.ts").symlink_to(outside)
  store = RouteStore([tmp_path])
  assert store.file("route", "0", "qcamera.ts") is None
  assert store.file("../route", "0", "qcamera.ts") is None
  assert store.file("route", "0", "rlog.zst") is None


def test_preview_cache_key_is_stable_and_path_bound(tmp_path):
  first, second = tmp_path / "first.ts", tmp_path / "second.ts"
  first.write_bytes(b"x"); second.write_bytes(b"x")
  assert preview_cache_key(first) == preview_cache_key(first)
  assert preview_cache_key(first) != preview_cache_key(second)


def test_main_is_process_entrypoint():
  assert callable(main)


def test_live_connect_retries_without_blocking_loop():
  class Client:
    def __init__(self, succeeds): self.succeeds = succeeds
    def connect(self, _): return self.succeeds
  async def run():
    attempts = iter((Client(False), Client(True)))
    assert (await connect_live_client(lambda: next(attempts), .1, 0)).succeeds
  asyncio.run(run())


def test_live_lease_is_acquired_before_connect_and_released():
  async def run():
    params = FakeParams(); lease = DriverViewLease(params, 0)
    class Client:
      def connect(self, _): return True
    def factory():
      assert params.get_bool("IsDriverViewEnabled")
      return Client()
    async with leased_live_client(lease, factory, .1): pass
    await asyncio.sleep(.01)
    assert not params.get_bool("IsDriverViewEnabled") and lease.clients == 0
  asyncio.run(run())


def test_multiple_live_leases_keep_driver_view_enabled():
  async def run():
    params = FakeParams(); lease = DriverViewLease(params, 0)
    lease.acquire(); lease.acquire(); lease.release(); await asyncio.sleep(.01)
    assert params.get_bool("IsDriverViewEnabled")
    lease.release(); await asyncio.sleep(.01)
    assert not params.get_bool("IsDriverViewEnabled")
  asyncio.run(run())


def test_live_fps_clamp_and_invalid_camera_need_no_hardware():
  assert live_fps("0") == 1 and live_fps("99") == 20 and live_fps("bad") == 15
  assert live_dimensions("1", "999") == (160, 960)
  assert live_dimensions("481", "361") == (480, 360)
  assert live_dimensions("1281", "961") == (1280, 960)
  assert live_dimensions("961", "721") == (960, 720)
  assert live_dimensions() == (960, 720)
  assert jpeg_qscale("1") == 2 and jpeg_qscale("99") == 15 and jpeg_qscale("bad") == 2
  assert jpeg_pillow_quality(2) == 90 and jpeg_pillow_quality(5) == 75
  assert live_config(FakeParams()) == {"selected": "high", "width": 960, "height": 720, "qscale": 2}
  try: vision_client_factory("nope")
  except KeyError: pass
  else: assert False


def test_live_quality_presets_persist_and_env_overrides():
  params = FakeParams()
  response = live_config_response(params)
  assert response["selected"] == "high" and [option["preset"] for option in response["options"]] == ["smooth", "high", "ultra"]
  params.put("C3WebLiveQuality", "0")
  assert live_config(params) == {"selected": "smooth", "width": 640, "height": 480, "qscale": 3}
  smooth_encoder = JpegEncoder(live_config(params))
  params.put("C3WebLiveQuality", "2")
  ultra_encoder = JpegEncoder(live_config(params))
  assert smooth_encoder.max_size == (640, 480) and ultra_encoder.max_size == (1280, 960)
  old = {key: os.environ.get(key) for key in ("C3_WEB_MAX_WIDTH", "C3_WEB_MAX_HEIGHT", "C3_WEB_JPEG_QSCALE")}
  try:
    os.environ.update({"C3_WEB_MAX_WIDTH": "480", "C3_WEB_MAX_HEIGHT": "360", "C3_WEB_JPEG_QSCALE": "4"})
    assert live_config(params) == {"selected": "ultra", "width": 480, "height": 360, "qscale": 4}
  finally:
    for key, value in old.items():
      if value is None: os.environ.pop(key, None)
      else: os.environ[key] = value


def test_live_quality_post_rejects_active_and_persists_selection():
  class Request:
    def __init__(self, app, preset): self.app, self.preset = app, preset
    async def json(self): return {"preset": self.preset}
  async def run():
    params = FakeParams(); app = {"params": params, "broadcasters": {}}
    assert b'"selected": "high"' in (await live_config_get(type("Request", (), {"app": app})())).body
    response = await live_config_post(Request(app, "smooth"))
    assert int(params.get("C3WebLiveQuality")) == 0 and b'"selected": "smooth"' in response.body
    app["broadcasters"] = {"road": type("Broadcaster", (), {"has_clients": True})()}
    try: await live_config_post(Request(app, "ultra"))
    except web.HTTPConflict: pass
    else: assert False
    try: await live_config_post(Request(app, "invalid"))
    except web.HTTPBadRequest: pass
    else: assert False
  asyncio.run(run())


def test_broadcaster_fanout_drops_stale_frames_and_cleans_up():
  class Client:
    def __init__(self): self.closed = False; self.threads = [threading.get_ident()]
    def connect(self, _): self.threads.append(threading.get_ident()); return True
    def recv(self, timeout_ms): self.threads.append(threading.get_ident()); return b"frame"
    def close(self): self.closed = True; self.threads.append(threading.get_ident())
  class Encoder:
    def __init__(self): self.closed = False; self.threads = [threading.get_ident()]
    def encode(self, frame): self.threads.append(threading.get_ident()); return b"jpeg-" + frame
    def close(self): self.closed = True; self.threads.append(threading.get_ident())
  async def run():
    clients, encoders = [], []
    def factory(): clients.append(Client()); return clients[-1]
    def encoder_factory(): encoders.append(Encoder()); return encoders[-1]
    bc = FrameBroadcaster("road", 20, factory, encoder_factory)
    first, second = bc.subscribe(), bc.subscribe()
    await bc.start()
    assert await asyncio.wait_for(first.get(), .5) == b"jpeg-frame"
    assert await asyncio.wait_for(second.get(), .5) == b"jpeg-frame"
    assert len(clients) == 1 and bc.thread is not None
    bc._publish(b"stale"); bc._publish(b"latest")
    assert await first.get() == b"latest"
    await bc.stop()
    assert bc.thread is None and clients[0].closed and encoders[0].closed
    assert len(set(clients[0].threads + encoders[0].threads)) == 1
  asyncio.run(run())


def test_ws_session_cancels_blocked_sender_when_peer_closes():
  class WebSocket:
    closed = False
    async def send_bytes(self, frame): assert False
    def __aiter__(self): return self
    async def __anext__(self): raise StopAsyncIteration
  async def run():
    await asyncio.wait_for(ws_session(WebSocket(), asyncio.Queue(), type("Broadcaster", (), {"error": None})()), .1)
  asyncio.run(run())


def test_driver_view_lease_preserves_existing_owner():
  params = FakeParams(driver=True); lease = DriverViewLease(params, 0)
  lease.acquire(); lease.clear()
  assert params.get_bool("IsDriverViewEnabled")


def test_driver_view_lease_clears_its_own_value():
  async def run():
    params = FakeParams(); lease = DriverViewLease(params, 0)
    lease.acquire(); assert params.get_bool("IsDriverViewEnabled")
    lease.release(); await asyncio.sleep(.01)
    assert not params.get_bool("IsDriverViewEnabled")
  asyncio.run(run())
