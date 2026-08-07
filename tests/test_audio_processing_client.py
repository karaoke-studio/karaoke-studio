from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import pytest

from krok_helper.audio_processing.separation.client import (
    PyMSSAPIError,
    PyMSSClient,
    normalize_server_url,
)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    requests: list[dict] = []

    def log_message(self, *_args) -> None:
        return None

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        self.requests.append({"method": "GET", "path": parsed.path, "auth": self.headers.get("Authorization")})
        if parsed.path == "/health":
            self._send_json({"status": "ok", "model_loaded": False, "model_loading": False})
        elif parsed.path == "/openapi.json":
            self._send_json(
                {
                    "info": {"version": "1"},
                    "paths": {
                        "/v1/models/load": {},
                        "/v1/models/download": {},
                        "/v1/audio/separations": {},
                    },
                }
            )
        elif parsed.path == "/v1/server/info":
            self._send_json({"object": "server.info", "limits": {"max_audio_seconds": 600}})
        elif parsed.path == "/v1/models":
            self._send_json({"object": "list", "data": []})
        else:
            self._send_json(
                {"error": {"code": "model_not_found", "message": "missing", "param": "model"}},
                404,
            )

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.requests.append(
            {
                "method": "POST",
                "path": parsed.path,
                "query": parsed.query,
                "auth": self.headers.get("Authorization"),
                "body": body,
            }
        )
        if parsed.path == "/v1/audio/separations":
            response = b"PK\x03\x04fake-zip"
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)
        elif parsed.path == "/v1/models/load":
            self._send_json({"object": "model.load", "model_loaded": True})
        else:
            self._send_json({"object": "ok"})


@pytest.fixture
def server_url():
    _Handler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_normalize_server_url_rejects_unsafe_shapes() -> None:
    assert normalize_server_url("http://127.0.0.1:8000/") == "http://127.0.0.1:8000"
    with pytest.raises(ValueError):
        normalize_server_url("file:///tmp/pymss")
    with pytest.raises(ValueError):
        normalize_server_url("http://user:password@example.com")


def test_capability_checks_cover_protocol_and_required_endpoints(server_url) -> None:
    checks = PyMSSClient(server_url).capability_checks()
    assert checks
    assert all(ok for _name, ok, _detail in checks)


def test_api_key_and_model_load_payload(server_url) -> None:
    client = PyMSSClient(server_url, api_key="secret-token")
    response = client.load_model("demo-model", inference_params={"batch_size": 1})
    assert response["model_loaded"] is True
    request = _Handler.requests[-1]
    assert request["auth"] == "Bearer secret-token"
    payload = json.loads(request["body"])
    assert payload["model"] == "demo-model"
    assert payload["inference_params"] == {"batch_size": 1}


def test_separate_pcm_streams_zip_to_partial_then_renames(server_url, tmp_path) -> None:
    pcm = tmp_path / "input.f32le"
    pcm.write_bytes(b"\x00\x00\x00\x00" * 8)
    output = tmp_path / "result.zip"

    PyMSSClient(server_url).separate_pcm(
        pcm,
        output,
        model="demo.ckpt",
        sample_rate=44100,
        stems=("vocals",),
    )

    assert output.read_bytes() == b"PK\x03\x04fake-zip"
    assert not (tmp_path / "result.zip.part").exists()
    request = _Handler.requests[-1]
    assert request["body"] == pcm.read_bytes()
    assert "stems=vocals" in request["query"]


def test_api_error_preserves_structured_code(server_url) -> None:
    client = PyMSSClient(server_url)
    with pytest.raises(PyMSSAPIError) as captured:
        client.catalog_model("missing")
    assert captured.value.status_code == 404
    assert captured.value.code == "model_not_found"
    assert captured.value.parameter == "model"
