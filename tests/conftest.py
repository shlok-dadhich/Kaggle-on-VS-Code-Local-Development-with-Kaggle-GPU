"""Shared test fixtures for kaggle-runner test suite."""

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit

import pytest


class FakeJupyterHandler(BaseHTTPRequestHandler):
    # Class-level server state so requests can modify it
    storage = {}  # posix path -> bytes or 'DIR'
    kernels = {}  # kernel_id -> state
    fail_auth = False
    fail_500 = False

    def log_message(self, format, *args):
        # Silence HTTP server logs during tests
        pass

    def _parse_url(self):
        parsed = urlsplit(self.path)
        path = parsed.path
        parts = [p for p in path.split("/") if p]
        return parsed, parts

    def do_GET(self):
        if self.fail_auth:
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"Unauthorized")
            return

        if self.fail_500:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"Server Error")
            return

        parsed, parts = self._parse_url()

        # /k/<session>/<token>/proxy/api
        if len(parts) >= 4 and parts[3] == "proxy":
            subpath = "/".join(parts[4:])

            if subpath == "api":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"version": "7.0.6"}).encode("utf-8"))
                return

            if subpath == "api/kernels":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                data = [{"id": k, "name": "python3", "execution_state": "idle"} for k in self.kernels]
                self.wfile.write(json.dumps(data).encode("utf-8"))
                return

            if subpath.startswith("api/kernels/"):
                kid = parts[6] if len(parts) > 6 else ""
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"id": kid, "execution_state": "idle"}).encode("utf-8"))
                return

            if subpath == "api/sessions":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"[]")
                return

            if subpath == "api/kernelspecs":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"default": "python3"}).encode("utf-8"))
                return

            if subpath.startswith("api/contents"):
                content_path = "/".join(parts[5:])
                if content_path in self.storage:
                    item = self.storage[content_path]
                    if item == "DIR":
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(json.dumps({"type": "directory", "name": content_path}).encode("utf-8"))
                    else:
                        b64 = base64.b64encode(item).decode("ascii")
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(json.dumps({
                            "type": "file",
                            "format": "base64",
                            "content": b64,
                            "name": content_path.split("/")[-1],
                            "size": len(item),
                        }).encode("utf-8"))
                    return
                self.send_response(404)
                self.end_headers()
                return

            if subpath.startswith("files/"):
                file_path = "/".join(parts[5:])
                storage_key = f"contents/{file_path}"
                if storage_key in self.storage and self.storage[storage_key] != "DIR":
                    data = self.storage[storage_key]
                    range_header = self.headers.get("Range")
                    if range_header and range_header.startswith("bytes="):
                        rng = range_header[6:].split("-")
                        start = int(rng[0]) if rng[0] else 0
                        end = int(rng[1]) if len(rng) > 1 and rng[1] else len(data) - 1
                        end = min(end, len(data) - 1)
                        chunk = data[start:end + 1]
                        self.send_response(206)
                        self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
                        self.send_header("Content-Length", str(len(chunk)))
                        self.end_headers()
                        self.wfile.write(chunk)
                        return

                    self.send_response(200)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                self.send_response(404)
                self.end_headers()
                return

        self.send_response(404)
        self.end_headers()

    def do_PUT(self):
        parsed, parts = self._parse_url()
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length > 0 else b"{}"
        data = json.loads(body)

        content_path = "/".join(parts[5:])
        if data.get("type") == "directory":
            self.storage[content_path] = "DIR"
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"name": content_path, "type": "directory"}).encode("utf-8"))
            return

        if data.get("type") == "file":
            chunk_num = data.get("chunk")
            raw_b64 = data.get("content", "")
            raw_bytes = base64.b64decode(raw_b64) if raw_b64 else b""

            if chunk_num is not None:
                # Chunked upload
                if content_path not in self.storage or self.storage[content_path] == "DIR":
                    self.storage[content_path] = bytearray()
                if isinstance(self.storage[content_path], bytes):
                    self.storage[content_path] = bytearray(self.storage[content_path])
                self.storage[content_path].extend(raw_bytes)
                if chunk_num == -1:
                    self.storage[content_path] = bytes(self.storage[content_path])
            else:
                self.storage[content_path] = raw_bytes

            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"name": content_path, "type": "file"}).encode("utf-8"))
            return

        self.send_response(400)
        self.end_headers()

    def do_POST(self):
        parsed, parts = self._parse_url()
        subpath = "/".join(parts[4:])
        if subpath == "api/kernels":
            kid = f"kernel-{len(self.kernels) + 1}"
            self.kernels[kid] = "idle"
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"id": kid, "name": "python3"}).encode("utf-8"))
            return

        if subpath.endswith("/interrupt"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok"}).encode("utf-8"))
            return

        self.send_response(404)
        self.end_headers()

    def do_DELETE(self):
        parsed, parts = self._parse_url()
        subpath = "/".join(parts[4:])
        if subpath.startswith("api/kernels/"):
            kid = parts[6] if len(parts) > 6 else ""
            self.kernels.pop(kid, None)
            self.send_response(204)
            self.end_headers()
            return

        if subpath.startswith("api/contents/"):
            content_path = "/".join(parts[5:])
            # Delete file or directory and children
            to_del = [k for k in self.storage if k == content_path or k.startswith(f"{content_path}/")]
            for k in to_del:
                self.storage.pop(k, None)
            self.send_response(204)
            self.end_headers()
            return

        self.send_response(404)
        self.end_headers()


class FakeServerController:
    def __init__(self, server, handler_cls):
        self.server = server
        self.handler_cls = handler_cls
        self.host, self.port = server.server_address

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/k/test-session/test-token-12345/proxy"

    def clear(self):
        self.handler_cls.storage.clear()
        self.handler_cls.kernels.clear()
        self.handler_cls.fail_auth = False
        self.handler_cls.fail_500 = False


@pytest.fixture(scope="session")
def fake_jupyter_server():
    server = HTTPServer(("127.0.0.1", 0), FakeJupyterHandler)
    controller = FakeServerController(server, FakeJupyterHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield controller
    server.shutdown()
