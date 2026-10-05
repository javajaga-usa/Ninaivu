"""The AI server connector, against a stand-in ComfyUI.

The stand-in speaks the handful of ComfyUI endpoints Ninaivu uses and records
what it was sent, so these tests check the real HTTP exchange: the photograph
is uploaded, the workflow arrives with its placeholders filled, the finished
image comes back at the preview's size — and the failure paths (a rejected
workflow, a job that never finishes, a redirect) say something useful.
"""
import base64
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from PIL import Image

from conftest import login, ADMIN, FAMILY
from ninaivu import extensions

# The AI server is the creative-studio extension's, not the core's: these
# tests skip when it is not importable. ``pip install -e
# extensions/creative-studio``, or put extensions/creative-studio on the
# path as CI does.
pytest.importorskip("ninaivu_studio")
from ninaivu_studio.ai_server import comfyui, service, workflows          # noqa: E402
from ninaivu_studio.ai_server.comfyui import AIServerError, Client, network_scope, normalise_url  # noqa: E402


@pytest.fixture(autouse=True)
def studio_on(cfg, monkeypatch):
    """The extension switched on for every app these tests build."""
    monkeypatch.setenv(extensions.DEV_MODULES_VAR, "ninaivu_studio")
    extensions.discover(refresh=True)
    cfg.extensions = ["creative-studio"]
    yield
    extensions.discover(refresh=True)


# ---------------------------------------------------------------------------
# A stand-in ComfyUI
# ---------------------------------------------------------------------------

class FakeComfy:
    def __init__(self):
        self.uploads = {}           # name -> bytes
        self.prompts = []           # workflows received
        self.deleted = []
        self.interrupted = 0
        self.mode = "ok"            # ok | reject | never | redirect | fail
        self.polls_before_done = 1
        self._polls = {}
        self.websocket = True       # serve /ws with progress messages
        self.queue = None           # what GET /queue returns, when set
        self.last_prompt_id = ""
        self._ws = {}               # clientId -> queue of frames to send
        self._ws_lock = threading.Lock()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, body, kind="application/json", headers=None):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(data)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(data)

            def _body(self):
                return self.rfile.read(int(self.headers.get("Content-Length") or 0))

            def _websocket(self, client_id):
                import base64 as b64
                import hashlib
                import queue as queue_mod
                key = self.headers.get("Sec-WebSocket-Key", "")
                accept = b64.b64encode(hashlib.sha1(
                    key.encode() + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest()).decode()
                self.send_response_only(101)
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept", accept)
                self.end_headers()
                self.wfile.flush()
                frames = queue_mod.Queue()
                with server._ws_lock:
                    server._ws[client_id] = frames
                # Things a real ComfyUI also sends that are not progress text.
                self.wfile.write(bytes([0x82, 4]) + bytes([0, 1, 2, 3]))   # a binary preview frame
                self.wfile.write(bytes([0x89, 2]) + b"hi")                  # a ping
                self.wfile.flush()
                while True:
                    try:
                        message = frames.get(timeout=10)
                    except queue_mod.Empty:
                        return
                    if message is None:
                        return
                    data = json.dumps(message).encode()
                    header = bytes([0x81, len(data)]) if len(data) < 126 else (
                        bytes([0x81, 126]) + len(data).to_bytes(2, "big"))
                    try:
                        self.wfile.write(header + data)
                        self.wfile.flush()
                    except OSError:
                        return

            def do_GET(self):
                parts = urlsplit(self.path)
                if parts.path == "/ws":
                    if not server.websocket:
                        return self._send(404, {"error": "no websocket"})
                    return self._websocket(parse_qs(parts.query).get("clientId", [""])[0])
                if server.mode == "redirect":
                    return self._send(302, b"", headers={"Location": "http://elsewhere.example/"})
                if parts.path == "/system_stats":
                    return self._send(200, {"system": {"comfyui_version": "0.3.99", "pytorch_version": "2.8"},
                                            "devices": [{"name": "cuda:0 NVIDIA GeForce RTX 4090", "type": "cuda",
                                                         "vram_total": 24 * 1024 ** 3, "vram_free": 20 * 1024 ** 3}]})
                if parts.path == "/object_info":
                    return self._send(200, {name: {} for name in
                                            ("LoadImage", "LoadImageMask", "SaveImage", "ImageInvert", "CLIPTextEncode")})
                if parts.path.startswith("/history/"):
                    prompt_id = parts.path.rsplit("/", 1)[1]
                    count = server._polls.get(prompt_id, 0) + 1
                    server._polls[prompt_id] = count
                    if server.mode == "never" or count <= server.polls_before_done:
                        return self._send(200, {})
                    if server.mode == "fail":
                        return self._send(200, {prompt_id: {"status": {"status_str": "error", "completed": False,
                                          "messages": [["execution_error", {"node_type": "KSampler",
                                                                             "exception_message": "CUDA out of memory"}]]},
                                                            "outputs": {}}})
                    return self._send(200, {prompt_id: {"status": {"status_str": "success", "completed": True},
                                                        "outputs": {"9": {"images": [
                                                            {"filename": "result.png", "subfolder": "", "type": "output"}]}}}})
                if parts.path == "/view":
                    query = parse_qs(parts.query)
                    assert query["filename"] == ["result.png"]
                    return self._send(200, server.result_png(), kind="image/png")
                if parts.path == "/queue":
                    if server.queue is not None:
                        return self._send(200, server.queue(server))
                    return self._send(200, {"queue_running": [[0, "p-running"]], "queue_pending": []})
                return self._send(404, {"error": "not found"})

            def do_POST(self):
                parts = urlsplit(self.path)
                body = self._body()
                if parts.path == "/upload/image":
                    start = body.index(b'filename="') + len(b'filename="')
                    name = body[start:body.index(b'"', start)].decode()
                    data_start = body.index(b"\r\n\r\n", start) + 4
                    data_end = body.rindex(b"\r\n--")
                    server.uploads[name] = body[data_start:data_end]
                    return self._send(200, {"name": name, "subfolder": "", "type": "input"})
                if parts.path == "/prompt":
                    payload = json.loads(body)
                    server.prompts.append(payload["prompt"])
                    if server.mode == "reject":
                        return self._send(400, {"error": {"message": "Prompt outputs failed validation"},
                                                "node_errors": {"7": {"class_type": "UNETLoader", "errors": [
                                                    {"message": "Value not in list", "details": "unet_name: missing.safetensors"}]}}})
                    prompt_id = "p-running" if server.mode == "never" else f"p{len(server.prompts)}"
                    with server._ws_lock:
                        frames = server._ws.get(payload.get("client_id", ""))
                    if frames is not None and server.mode == "ok":
                        frames.put({"type": "execution_start", "data": {"prompt_id": prompt_id}})
                        frames.put({"type": "progress", "data": {"value": 1, "max": 9, "prompt_id": "someone-else"}})
                        for step in range(1, 5):
                            frames.put({"type": "progress", "data": {"value": step, "max": 4, "prompt_id": prompt_id}})
                        frames.put({"type": "executing", "data": {"node": None, "prompt_id": prompt_id}})
                    server.last_prompt_id = prompt_id
                    return self._send(200, {"prompt_id": prompt_id})
                if parts.path == "/queue":
                    server.deleted.extend(json.loads(body).get("delete", []))
                    return self._send(200, {})
                if parts.path == "/interrupt":
                    server.interrupted += 1
                    return self._send(200, {})
                return self._send(404, {"error": "not found"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def result_png(self):
        """The uploaded photo, inverted and at twice the size: proof of a round trip."""
        photo = next(data for name, data in self.uploads.items())
        with Image.open(io.BytesIO(photo)) as source:
            image = source.convert("RGB").point(lambda v: 255 - v)
        image = image.resize((image.width * 2, image.height * 2))
        buffer = io.BytesIO()
        image.save(buffer, "PNG")
        return buffer.getvalue()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture()
def comfy():
    server = FakeComfy()
    yield server
    server.close()


EDIT_WORKFLOW = {
    "1": {"class_type": "LoadImage", "inputs": {"image": "{{image}}"}},
    "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "a photo, {{prompt}}"}},
    "3": {"class_type": "CLIPTextEncode", "inputs": {"text": "{{negative_prompt}}"}},
    "4": {"class_type": "KSampler", "inputs": {"seed": "{{seed}}", "steps": 20}},
    "9": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}},
}
REMOVE_WORKFLOW = {
    "1": {"class_type": "LoadImage", "inputs": {"image": "{{image}}"}},
    "2": {"class_type": "LoadImageMask", "inputs": {"image": "{{mask}}", "channel": "red"}},
    "9": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}},
}


def _png(size=(64, 40), colour=(200, 40, 40)):
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, "PNG")
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Addresses
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("given, expected", [
    ("192.168.1.50:8188", "http://192.168.1.50:8188"),
    ("http://gpu-box.local:8188/", "http://gpu-box.local:8188"),
    ("https://comfy.home.arpa/comfy/", "https://comfy.home.arpa/comfy"),
    ("http://[fd00::5]:8188", "http://[fd00::5]:8188"),
])
def test_addresses_are_normalised(given, expected):
    assert normalise_url(given) == expected


@pytest.mark.parametrize("bad", ["", "ftp://host", "http://user:pw@host:8188", "http://host?x=1",
                                 "http://host:99999", "http://"])
def test_bad_addresses_are_refused(bad):
    with pytest.raises(ValueError):
        normalise_url(bad)


@pytest.mark.parametrize("url, scope", [
    ("http://192.168.1.50:8188", "home"), ("http://10.0.0.2", "home"), ("http://127.0.0.1:8188", "home"),
    ("http://gpu-box.local:8188", "home"), ("http://gpubox:8188", "home"),
    ("http://8.8.8.8:8188", "public"), ("http://comfy.example.com", "unknown"),
])
def test_the_console_knows_a_home_address_from_a_public_one(url, scope):
    assert network_scope(url) == scope


# ---------------------------------------------------------------------------
# Workflows
# ---------------------------------------------------------------------------

def test_the_editor_format_is_recognised_and_explained():
    with pytest.raises(ValueError, match="Export \\(API\\)"):
        workflows.parse({"nodes": [], "links": []})


def test_placeholders_are_found_and_checked():
    assert workflows.placeholders(EDIT_WORKFLOW) == {"image", "prompt", "negative_prompt", "seed"}
    with pytest.raises(ValueError, match="does not fill"):
        workflows.placeholders({"1": {"class_type": "X", "inputs": {"a": "{{photo}}"}}})
    with pytest.raises(ValueError, match="whole value"):
        workflows.placeholders({"1": {"class_type": "X", "inputs": {"a": "input/{{image}}"}}})


def test_fill_puts_numbers_where_numbers_go():
    filled = workflows.fill(EDIT_WORKFLOW, {"image": "ninaivu-1.png", "prompt": "make it snow",
                                            "negative_prompt": "blur", "seed": 7})
    assert filled["1"]["inputs"]["image"] == "ninaivu-1.png"
    assert filled["2"]["inputs"]["text"] == "a photo, make it snow"
    assert filled["4"]["inputs"]["seed"] == 7 and filled["4"]["inputs"]["steps"] == 20
    assert EDIT_WORKFLOW["1"]["inputs"]["image"] == "{{image}}", "the saved workflow was changed"


def test_a_workflow_missing_what_its_job_needs_is_not_saved(cfg):
    with pytest.raises(ValueError, match="mask"):
        workflows.save(cfg, "Removal", "remove", EDIT_WORKFLOW)
    entry = workflows.save(cfg, "Qwen image edit!", "edit", json.dumps(EDIT_WORKFLOW))
    assert entry["id"] == "qwen-image-edit"
    assert workflows.load(cfg, "qwen-image-edit")["workflow"] == EDIT_WORKFLOW
    assert workflows.load(cfg, "../config") is None and workflows.load(cfg, "---") is None


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------

def test_a_job_round_trips(comfy):
    client = Client(comfy.url)
    name = client.upload(_png())
    result = client.run(workflows.fill(EDIT_WORKFLOW, {"image": name, "prompt": "p",
                                                       "negative_prompt": "", "seed": 1}), deadline=10)
    assert comfy.prompts[0]["1"]["inputs"]["image"] == name
    with Image.open(io.BytesIO(result)) as image:
        assert image.size == (128, 80)


def test_describing_the_server(comfy):
    stats = Client(comfy.url).system_stats()
    assert stats["devices"][0]["vram_total"] == 24 * 1024 ** 3
    assert "LoadImageMask" in Client(comfy.url).node_types()


def test_a_rejected_workflow_says_which_node(comfy):
    comfy.mode = "reject"
    with pytest.raises(AIServerError, match="UNETLoader.*missing.safetensors"):
        Client(comfy.url).run(EDIT_WORKFLOW, deadline=10)


def test_a_failed_job_reports_the_server_error(comfy):
    comfy.mode = "fail"
    with pytest.raises(AIServerError, match="CUDA out of memory"):
        Client(comfy.url).run(EDIT_WORKFLOW, deadline=10)


def test_a_job_that_never_finishes_is_cancelled(comfy):
    comfy.mode = "never"
    with pytest.raises(AIServerError, match="did not finish"):
        Client(comfy.url).run(EDIT_WORKFLOW, deadline=0.3)
    assert comfy.deleted == ["p-running"] and comfy.interrupted == 1


def test_redirects_are_never_followed(comfy):
    comfy.mode = "redirect"
    with pytest.raises(AIServerError, match="redirect"):
        Client(comfy.url).system_stats()


def test_an_unreachable_server_says_how_to_fix_it():
    with pytest.raises(AIServerError, match="--listen"):
        Client("http://127.0.0.1:9", timeout=2).system_stats()


# ---------------------------------------------------------------------------
# The Playground endpoints
# ---------------------------------------------------------------------------

def _use_server(app, comfy, **extra):
    cfg = app.config["MV_CONFIG"]
    workflows.save(cfg, "Edit", "edit", EDIT_WORKFLOW)
    workflows.save(cfg, "Remove", "remove", REMOVE_WORKFLOW)
    cfg.ai_server_url, cfg.ai_server_enabled = comfy.url, True
    cfg.ai_server_edit_workflow, cfg.ai_server_remove_workflow = "edit", "remove"
    for key, value in extra.items():
        setattr(cfg, key, value)
    return cfg


def test_nothing_is_sent_until_the_server_is_switched_on(app, comfy, people):
    cfg = _use_server(app, comfy)
    cfg.ai_server_enabled = False
    family = login(app.test_client(), *FAMILY)
    caps = family.get("/api/ai-playground/capabilities").get_json()
    assert caps["image_provider"] != "ai-server"
    family.post("/api/ai-playground/generate",
                json={"prompt": "add snow", "image": base64.b64encode(_png()).decode()})
    assert comfy.uploads == {} and comfy.prompts == []


def test_a_generative_edit_runs_on_the_server(app, comfy, people):
    _use_server(app, comfy)
    family = login(app.test_client(), *FAMILY)
    caps = family.get("/api/ai-playground/capabilities").get_json()
    assert caps["image_model"] and caps["image_provider"] == "ai-server"
    assert caps["image_edit_max_side"] == 1024

    response = family.post("/api/ai-playground/generate", json={
        "prompt": "add snow", "image": base64.b64encode(_png()).decode(),
        "options": {"seed": 99, "negative_prompt": "people"}})
    assert response.status_code == 200, response.get_json()
    sent = comfy.prompts[0]
    assert sent["2"]["inputs"]["text"] == "a photo, add snow"
    assert sent["3"]["inputs"]["text"] == "people" and sent["4"]["inputs"]["seed"] == 99
    with Image.open(io.BytesIO(response.data)) as image:
        assert image.size == (64, 40), "the result is not at the preview's size"
        assert image.getpixel((5, 5)) == (55, 215, 215), "the server's result was not used"


def test_the_request_filter_runs_before_anything_is_uploaded(app, comfy, people):
    _use_server(app, comfy)
    family = login(app.test_client(), *FAMILY)
    response = family.post("/api/ai-playground/generate", json={
        "prompt": "make them naked", "image": base64.b64encode(_png()).decode()})
    assert response.status_code == 400
    assert comfy.uploads == {}


def test_object_removal_sends_a_clean_mask(app, comfy, people):
    _use_server(app, comfy)
    family = login(app.test_client(), *FAMILY)
    assert family.get("/api/ai-playground/capabilities").get_json()["object_removal_provider"] == "ai-server"
    mask = Image.new("L", (64, 40), 0)
    mask.paste(90, (10, 10, 20, 20))              # a soft grey brush stroke
    buffer = io.BytesIO()
    mask.save(buffer, "PNG")
    response = family.post("/api/ai-playground/inpaint", json={
        "image": base64.b64encode(_png()).decode(), "mask": base64.b64encode(buffer.getvalue()).decode()})
    assert response.status_code == 200, response.get_json()
    sent_mask_name = comfy.prompts[0]["2"]["inputs"]["image"]
    with Image.open(io.BytesIO(comfy.uploads[sent_mask_name])) as sent:
        assert sent.size == (64, 40) and set(sent.convert("L").getdata()) == {0, 255}


def test_a_large_removal_preview_is_shrunk_for_the_server(app, comfy, people):
    _use_server(app, comfy, ai_server_max_side=512)
    family = login(app.test_client(), *FAMILY)
    mask = Image.new("L", (1600, 1000), 255)
    buffer = io.BytesIO()
    mask.save(buffer, "PNG")
    response = family.post("/api/ai-playground/inpaint", json={
        "image": base64.b64encode(_png((1600, 1000))).decode(),
        "mask": base64.b64encode(buffer.getvalue()).decode()})
    assert response.status_code == 200, response.get_json()
    photo = comfy.uploads[comfy.prompts[0]["1"]["inputs"]["image"]]
    with Image.open(io.BytesIO(photo)) as sent:
        assert max(sent.size) == 512
    with Image.open(io.BytesIO(response.data)) as image:
        assert image.size == (1600, 1000)


def test_an_unreachable_server_is_a_503_with_a_reason(app, people):
    cfg = app.config["MV_CONFIG"]
    workflows.save(cfg, "Edit", "edit", EDIT_WORKFLOW)
    cfg.ai_server_url, cfg.ai_server_enabled, cfg.ai_server_edit_workflow = "http://127.0.0.1:9", True, "edit"
    family = login(app.test_client(), *FAMILY)
    response = family.post("/api/ai-playground/generate", json={
        "prompt": "add snow", "image": base64.b64encode(_png()).decode()})
    assert response.status_code == 503 and "AI server" in response.get_json()["error"]


# ---------------------------------------------------------------------------
# The console
# ---------------------------------------------------------------------------

def test_the_console_saves_validates_and_tests(app, comfy, people):
    admin = login(app.test_client(), *ADMIN)
    cfg = app.config["MV_CONFIG"]

    assert admin.post("/api/admin/ai-server", json={"enabled": True}).status_code == 400
    assert admin.post("/api/admin/ai-server", json={"url": "ftp://x"}).status_code == 400
    assert admin.post("/api/admin/ai-server", json={"timeout": 5}).status_code == 400
    assert admin.post("/api/admin/ai-server", json={"edit_workflow": "nope"}).status_code == 400
    assert admin.post("/api/admin/ai-server", json={"surprise": 1}).status_code == 400
    assert not cfg.ai_server_enabled and not cfg.ai_server_url, "a rejected request changed settings"

    saved = admin.post("/api/admin/ai-server/workflows", json={
        "name": "Edit", "purpose": "edit", "workflow": json.dumps(EDIT_WORKFLOW)})
    assert saved.status_code == 200, saved.get_json()
    bad = admin.post("/api/admin/ai-server/workflows", json={
        "name": "Broken", "purpose": "remove", "workflow": EDIT_WORKFLOW})
    assert bad.status_code == 400 and "mask" in bad.get_json()["error"]

    result = admin.post("/api/admin/ai-server", json={
        "url": comfy.url, "enabled": True, "edit_workflow": "edit", "timeout": 120, "max_side": 1536})
    assert result.status_code == 200, result.get_json()
    settings = result.get_json()["settings"]
    assert settings["active"] == {"edit": True, "remove": False, "upscale": False, "restore": False,
                                  "colorize": False, "inpaint": False} and settings["network"] == "home"
    stored = json.loads(Path(cfg.config_path).read_text(encoding="utf-8"))
    assert stored["ai_server_url"] == comfy.url and stored["ai_server_max_side"] == 1536

    test = admin.post("/api/admin/ai-server/test", json={}).get_json()
    assert test["ok"] and "4090" in test["server"]["devices"][0]["name"]
    assert test["workflows"][0]["missing_nodes"] == ["KSampler"]

    deleted = admin.delete("/api/admin/ai-server/workflows/edit").get_json()
    assert deleted["settings"]["edit_workflow"] == "" and cfg.ai_server_edit_workflow == ""


def test_family_members_cannot_reach_the_console_endpoints(app, people):
    family = login(app.test_client(), *FAMILY)
    assert family.get("/api/admin/ai-server").status_code in (401, 403, 404)
    assert family.post("/api/admin/ai-server", json={"url": "http://8.8.8.8"}).status_code in (401, 403, 404)


def test_the_console_endpoints_are_not_on_the_family_port(scanned):
    from ninaivu import build_services, create_home_app
    cfg, _, _ = scanned
    cfg.watch = False
    services = build_services(cfg)
    services.scanner.stop()
    routes = {str(rule) for rule in create_home_app(services).url_map.iter_rules()}
    assert not [r for r in routes if r.startswith("/api/admin/ai-server")]


def test_the_preview_size_and_timeout_stay_in_bounds(cfg):
    cfg.ai_server_max_side, cfg.ai_server_timeout = 99999, 1
    assert service.max_side(cfg) == service.MAX_SIDE_RANGE[1]
    assert service.timeout(cfg) == service.TIMEOUT_RANGE[0]
    assert comfyui.MAX_IMAGE_BYTES > 0


# ---------------------------------------------------------------------------
# Progress, and jobs that run in the background
# ---------------------------------------------------------------------------

def test_progress_arrives_over_the_websocket(comfy):
    client = Client(comfy.url)
    name = client.upload(_png())
    seen = []
    comfy.polls_before_done = 2
    client.run(workflows.fill(EDIT_WORKFLOW, {"image": name, "prompt": "p", "negative_prompt": "",
                                              "seed": 1}), deadline=10, on_progress=seen.append)
    counted = [u for u in seen if u["stage"] == "running" and u["max"]]
    assert counted and counted[-1] == {"stage": "running", "value": 4, "max": 4}, seen
    assert not [u for u in seen if u.get("max") == 9], "another job's progress was reported as this one's"


def test_progress_falls_back_to_the_queue_without_a_websocket(comfy):
    comfy.websocket = False
    comfy.polls_before_done = 3
    comfy.queue = lambda server: {"queue_running": [[0, "other-house"]],
                                  "queue_pending": [[1, "earlier"], [2, server.last_prompt_id], [3, "later"]]}
    client = Client(comfy.url)
    client.upload(_png())                    # the stand-in returns the uploaded photo
    seen = []
    client.run(EDIT_WORKFLOW, deadline=15, on_progress=seen.append)
    assert {"stage": "queued", "ahead": 2} in seen, seen


def test_the_websocket_listener_skips_previews_answers_pings_and_notices_close(comfy):
    import time as time_mod
    from ninaivu_studio.ai_server import websocket
    host, port = comfy.url.rsplit("//", 1)[1].split(":")
    listener = websocket.Listener(host, int(port), "/ws?clientId=listener-test")
    try:
        frames = None
        for _ in range(100):
            with comfy._ws_lock:
                frames = comfy._ws.get("listener-test")
            if frames:
                break
            time_mod.sleep(0.05)
        frames.put({"type": "status", "data": {"hello": 1}})
        assert json.loads(listener.receive(5)) == {"type": "status", "data": {"hello": 1}}
        assert listener.receive(0.2) is None
        frames.put(None)                     # the server ends the connection
        with pytest.raises(OSError):
            listener.receive(5)
    finally:
        listener.close()


def _wait_for(client, job_id, timeout=20):
    import time as time_mod
    deadline = time_mod.time() + timeout
    while time_mod.time() < deadline:
        state = client.get(f"/api/ai-playground/server-jobs/{job_id}").get_json()
        if state["state"] in ("done", "error"):
            return state
        time_mod.sleep(0.2)
    raise AssertionError("the job did not finish")


UPSCALE_WORKFLOW = {
    "1": {"class_type": "LoadImage", "inputs": {"image": "{{image}}"}},
    "9": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}},
}


def test_an_upscale_runs_as_a_background_job_and_keeps_its_size(app, comfy, people):
    from ninaivu.media import jobs
    jobs.reset()
    cfg = _use_server(app, comfy)
    workflows.save(cfg, "Upscale x2", "upscale", UPSCALE_WORKFLOW)
    cfg.ai_server_upscale_workflow = "upscale-x2"
    family = login(app.test_client(), *FAMILY)
    assert "upscale" in family.get("/api/ai-playground/capabilities").get_json()["server_jobs"]

    started = family.post("/api/ai-playground/server-jobs",
                          json={"kind": "upscale", "image": base64.b64encode(_png()).decode()})
    assert started.status_code == 202, started.get_json()
    job_id = started.get_json()["id"]
    assert _wait_for(family, job_id)["state"] == "done"

    someone_else = login(app.test_client(), *ADMIN)
    assert someone_else.get(f"/api/ai-playground/server-jobs/{job_id}").status_code == 404, \
        "another profile saw the job"
    result = family.get(f"/api/ai-playground/server-jobs/{job_id}/result")
    assert result.status_code == 200
    with Image.open(io.BytesIO(result.data)) as image:
        assert image.size == (128, 80), "an upscale must not be shrunk back to the preview"
    assert family.get(f"/api/ai-playground/server-jobs/{job_id}/result").status_code == 404, "collected twice"


def test_a_background_edit_reports_the_server_error(app, comfy, people):
    from ninaivu.media import jobs
    jobs.reset()
    _use_server(app, comfy)
    comfy.mode = "fail"
    family = login(app.test_client(), *FAMILY)
    started = family.post("/api/ai-playground/server-jobs", json={
        "kind": "edit", "prompt": "add snow", "image": base64.b64encode(_png()).decode()})
    state = _wait_for(family, started.get_json()["id"])
    assert state["state"] == "error" and "CUDA out of memory" in state["error"]


def test_background_jobs_check_the_request_before_starting(app, comfy, people):
    from ninaivu.media import jobs
    jobs.reset()
    _use_server(app, comfy)
    family = login(app.test_client(), *FAMILY)
    image = base64.b64encode(_png()).decode()
    assert family.post("/api/ai-playground/server-jobs", json={"kind": "colorize", "image": image}).status_code == 404
    # Inpainting needs the painted area and the words for it, and a workflow assigned to it.
    assert family.post("/api/ai-playground/server-jobs", json={"kind": "inpaint", "image": image}).status_code == 400
    assert family.post("/api/ai-playground/server-jobs", json={"kind": "inpaint", "image": image, "mask": image}).status_code == 400
    assert family.post("/api/ai-playground/server-jobs", json={"kind": "inpaint", "image": image, "mask": image, "prompt": "hair"}).status_code == 404
    assert family.post("/api/ai-playground/server-jobs", json={
        "kind": "edit", "prompt": "make them naked", "image": image}).status_code == 400
    assert family.post("/api/ai-playground/server-jobs", json={"kind": "remove", "image": image}).status_code == 400
    assert comfy.uploads == {}


def test_a_ninaivu_runs_at_most_two_server_jobs_at_once():
    import time as time_mod
    from ninaivu.media import jobs
    jobs.reset()
    release = threading.Event()

    def slow(report):
        release.wait(10)
        return b"png"

    ids = [jobs.start(1, "edit", slow) for _ in range(jobs.MAX_RUNNING)]
    try:
        with pytest.raises(jobs.JobError, match="two edits"):
            jobs.start(1, "edit", slow)
    finally:
        release.set()
    for _ in range(100):
        if all(jobs.status(i, 1)["state"] == "done" for i in ids):
            break
        time_mod.sleep(0.05)
    assert jobs.take_result(ids[0], 1) == b"png" and jobs.take_result(ids[0], 1) is None
    assert jobs.status(ids[1], 2) is None, "a job answered to a profile that did not start it"


def test_the_console_assigns_every_job(app, comfy, people):
    admin = login(app.test_client(), *ADMIN)
    cfg = app.config["MV_CONFIG"]
    purposes = admin.get("/api/admin/ai-server").get_json()["purposes"]
    assert set(purposes) == {"edit", "remove", "upscale", "restore", "colorize", "inpaint"}
    assert purposes["inpaint"]["required"] == ["image", "mask", "prompt"]
    assert purposes["colorize"]["required"] == ["image"]
    assert admin.post("/api/admin/ai-server/workflows", json={
        "name": "Colour", "purpose": "colorize", "workflow": UPSCALE_WORKFLOW}).status_code == 200
    assert admin.post("/api/admin/ai-server", json={"jobs": {"upscale": "colour"}}).status_code == 400
    saved = admin.post("/api/admin/ai-server", json={"url": comfy.url, "enabled": True,
                                                     "jobs": {"colorize": "colour"}}).get_json()
    assert saved["settings"]["jobs"]["colorize"] == "colour" and saved["settings"]["active"]["colorize"]
    assert json.loads(Path(cfg.config_path).read_text(encoding="utf-8"))["ai_server_colorize_workflow"] == "colour"
    admin.delete("/api/admin/ai-server/workflows/colour")
    assert cfg.ai_server_colorize_workflow == ""
