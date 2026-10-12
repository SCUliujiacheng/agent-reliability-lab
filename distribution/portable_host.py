"""Bounded, authenticated loopback transport for a bundled offline Android app.

The session is passed directly to the native shell, never through a URL or log.
This is not a LAN/public web server. Keep it bound to 127.0.0.1.
"""

import hmac
import importlib
import json
import mimetypes
import re
import secrets
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, unquote, urlsplit

BODY_LIMIT = 32 * 1024 * 1024
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'",
}


def _unique_object(pairs):
    value = {}
    for name, item in pairs:
        if name in value:
            raise ValueError("Repeated JSON key")
        value[name] = item
    return value


def _reject_constant(value):
    raise ValueError("Non-finite JSON number")


class LocalServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    block_on_close = True
    allow_reuse_address = False
    request_queue_size = 16

    def __init__(self, app, resources):
        self.app = app
        self.resources = Path(resources).resolve()
        self.slots = threading.BoundedSemaphore(12)
        super().__init__(("127.0.0.1", 0), Handler)
        self.origin = f"http://127.0.0.1:{self.server_port}"
        self.cookie_name = f"portable_session_{self.server_port}"
        self.token = secrets.token_urlsafe(32)
        self.cookie = (
            f"{self.cookie_name}={self.token}; Path=/; HttpOnly; SameSite=Strict"
        )

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(15)
        return connection, address

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    # Close each connection: rejected bodies are never interpreted as another request.
    protocol_version = "HTTP/1.0"
    server_version = "OfflineApp"
    sys_version = ""

    def log_message(self, *args):
        pass  # No URLs, financial data, tokens, or request bodies in Android logs.

    def send_value(self, status, headers, body):
        self.send_response(status)
        for name, value in {**headers, **SECURITY_HEADERS}.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self.close_connection = True

    def fail(self, status, message):
        self.send_value(
            status,
            {"Content-Type": "application/json; charset=utf-8"},
            json.dumps({"detail": message}, ensure_ascii=False).encode(),
        )

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        self.do_GET()

    def do_PUT(self):
        self.do_GET()

    def do_DELETE(self):
        self.do_GET()

    def do_PATCH(self):
        self.do_GET()

    def do_GET(self):
        try:
            self.dispatch()
        except (ConnectionError, TimeoutError):
            self.close_connection = True
        except Exception:  # noqa: BLE001 -- the HTTP boundary must not expose private tracebacks
            # Do not reveal a filesystem path or private record via error text.
            self.fail(500, "操作未完成，请重新打开应用后重试")

    def dispatch(self):
        sensitive = {
            "host",
            "origin",
            "cookie",
            "content-length",
            "content-type",
            "transfer-encoding",
            "content-encoding",
            "x-ledger-request",
            "idempotency-key",
        }
        for name in sensitive:
            if len(self.headers.get_all(name, [])) > 1:
                return self.fail(400, "重复的请求头")
        if "Transfer-Encoding" in self.headers or "Content-Encoding" in self.headers:
            return self.fail(400, "不支持该传输编码")
        host = self.headers.get("Host", "")
        if host != self.server.origin.removeprefix("http://"):
            return self.fail(403, "只允许本机应用访问")
        origin = self.headers.get("Origin")
        site = self.headers.get("Sec-Fetch-Site", "")
        if (origin is not None and origin != self.server.origin) or site in {
            "cross-site",
            "same-site",
        }:
            return self.fail(403, "不接受跨站请求")
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            token = cookie[self.server.cookie_name].value
        except (KeyError, ValueError):
            return self.fail(401, "应用会话已失效，请重新打开")
        if not hmac.compare_digest(token, self.server.token):
            return self.fail(401, "应用会话已失效，请重新打开")
        if len(self.path) > 4096:
            return self.fail(414, "请求地址过长")
        parsed = urlsplit(self.path)
        path = unquote(parsed.path, errors="strict")
        if (
            parsed.scheme
            or parsed.netloc
            or parsed.fragment
            or "\\" in path
            or "%" in path
            or "\x00" in path
            or any(p in {".", ".."} for p in path.split("/"))
        ):
            return self.fail(404, "资源不存在")
        length_text = self.headers.get("Content-Length", "0")
        if not re.fullmatch(r"[0-9]{1,10}", length_text):
            return self.fail(400, "Content-Length 不合法")
        length = int(length_text)
        if length > BODY_LIMIT:
            return self.fail(413, "手机请求不能超过 32 MiB；导入文件不能超过 16 MiB")
        body = None
        if self.command not in {"GET", "HEAD"}:
            if (
                self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                != "application/json"
            ):
                return self.fail(415, "写操作须使用 application/json")
            try:
                raw = self.rfile.read(length)
                if len(raw) != length:
                    return self.fail(400, "请求不完整")
                body = json.loads(
                    raw.decode("utf-8"),
                    object_pairs_hook=_unique_object,
                    parse_constant=_reject_constant,
                )
                if not isinstance(body, dict):
                    raise TypeError("Object required")
            except (UnicodeError, ValueError, TypeError, RecursionError):
                return self.fail(422, "请求必须是有效的 JSON 对象")
        elif length:
            return self.fail(400, "读取请求不应带内容")
        if path == "/__health" and self.command in {"GET", "HEAD"}:
            return self.send_value(
                200,
                {"Content-Type": "application/json"},
                b'{"status":"ok","transport":"mobile/v1"}',
            )
        if path.startswith(("/api/", "/v1/")) or path in {"/api", "/v1", "/health"}:
            return self.send_value(
                *self.server.app.handle(
                    self.command,
                    path,
                    parse_qs(parsed.query, keep_blank_values=True, max_num_fields=50),
                    body,
                    {k.lower(): v for k, v in self.headers.items()},
                )
            )
        if self.command not in {"GET", "HEAD"}:
            return self.fail(405, "不支持此操作")
        web = self.server.resources / "web"
        candidate = (web / path.lstrip("/")).resolve()
        if not candidate.is_relative_to(web.resolve()):
            return self.fail(404, "资源不存在")
        if (
            not candidate.is_file()
            and not Path(path).suffix
            and not path.startswith("/assets/")
        ):
            candidate = web / "index.html"
        if not candidate.is_file():
            return self.fail(404, "资源不存在")
        mime = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        if candidate.suffix in {".js", ".mjs"}:
            mime = "text/javascript"
        return self.send_value(200, {"Content-Type": mime}, candidate.read_bytes())


def make_server(app, resources):
    return LocalServer(app, resources)


_lock = threading.Lock()
_server = None
_thread = None


def start(data_dir, resource_dir):
    global _server, _thread
    with _lock:
        if _server is None:
            app = importlib.import_module("mobile_app").create_app(
                data_dir, resource_dir
            )
            _server = make_server(app, resource_dir)
            _thread = threading.Thread(
                target=_server.serve_forever, name="local-app", daemon=True
            )
            _thread.start()
        return {"origin": _server.origin, "cookie": _server.cookie}


def stop():
    global _server, _thread
    with _lock:
        if _server is not None:
            _server.shutdown()
            _server.server_close()
            _thread.join(timeout=5)
            _server = _thread = None
