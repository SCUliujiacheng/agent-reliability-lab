"""Native executable entry point: bundled API, browser UI, per-user storage."""

import argparse
import asyncio
import hmac
import importlib
import json
import os
import secrets
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from html import escape
from http.cookies import SimpleCookie
from pathlib import Path

import uvicorn
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response


class DesktopSession:
    """The one-time launch link exchanges a nonce for an HttpOnly session cookie."""

    def __init__(self, app, origin, name, stop):
        self.app, self.origin, self.name, self.stop = app, origin, name, stop
        self.launch_path = "/__launch/" + secrets.token_urlsafe(32)
        self.deadline = time.monotonic() + 120
        self.launched = False
        self.token = secrets.token_urlsafe(32)
        self.cookie_name = "desktop_session_" + origin.rsplit(":", 1)[1]
        self.cookie = (
            f"{self.cookie_name}={self.token}; Path=/; HttpOnly; SameSite=Strict"
        )

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {}
        for key, value in scope["headers"]:
            key = key.decode().lower()
            if key in headers and key in {"host", "origin", "cookie", "content-length"}:
                return await JSONResponse({"detail": "Duplicate header"}, 400)(
                    scope, receive, send
                )
            headers[key] = value.decode("latin-1")
        if (
            headers.get("host") != self.origin.removeprefix("http://")
            or ("origin" in headers and headers["origin"] != self.origin)
            or headers.get("sec-fetch-site") in {"cross-site", "same-site"}
        ):
            return await JSONResponse(
                {"detail": "Only this local application may access the service"}, 403
            )(scope, receive, send)
        path = scope["path"]
        if path.startswith("/__launch/"):
            if (
                scope["method"] != "GET"
                or self.launched
                or time.monotonic() > self.deadline
                or not hmac.compare_digest(path, self.launch_path)
            ):
                response = Response(status_code=404)
            else:
                self.launched = True
                response = RedirectResponse(
                    "/__desktop",
                    303,
                    headers={
                        "Set-Cookie": self.cookie,
                        "Referrer-Policy": "no-referrer",
                        "Cache-Control": "no-store",
                    },
                )
            return await response(scope, receive, send)
        cookie = SimpleCookie()
        try:
            cookie.load(headers.get("cookie", ""))
            authorized = hmac.compare_digest(cookie[self.cookie_name].value, self.token)
        except (KeyError, ValueError):
            authorized = False
        if not authorized:
            return await JSONResponse(
                {"detail": "Please launch the downloaded application again"}, 401
            )(scope, receive, send)
        if path == "/__desktop":
            name = escape(self.name)
            html = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>{name}</title><link rel="stylesheet" href="/__desktop.css"><body><main><h1>{name}</h1><p>程序已在这台电脑上启动。账目或实验记录保存在你的用户目录，不在安装包里。</p><p>The application is running locally. Your data is stored separately from the installation.</p><p><a href="/" target="_blank" rel="noopener">打开应用 / Open app →</a></p><button id="stop">退出程序 / Quit</button><p id="status" role="status"></p><p>关闭应用页面不会停止本机服务，请用上面的按钮退出。<br>Closing a browser tab does not stop the service. Use Quit when finished.</p></main><script src="/__desktop.js"></script></body></html>"""
            return await HTMLResponse(
                html,
                headers={
                    "Cache-Control": "no-store",
                    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'; base-uri 'none'",
                    "Referrer-Policy": "no-referrer",
                },
            )(scope, receive, send)
        if path == "/__desktop.css":
            return await Response(
                "body{margin:0;background:#f5f4ef;color:#272d30;font:16px/1.7 system-ui}main{max-width:620px;margin:12vh auto;padding:28px}a{color:#176c61}button{padding:10px 20px;font:inherit;cursor:pointer}p{margin:24px 0}",
                media_type="text/css",
            )(scope, receive, send)
        if path == "/__desktop.js":
            script = "document.querySelector('#stop').onclick=async()=>{try{const r=await fetch('/__stop',{method:'POST',headers:{'X-Desktop-Request':'1','Content-Type':'application/json'},body:'{}'});if(!r.ok)throw Error();document.querySelector('#status').textContent='已退出，可以关闭页面。Stopped. You can close this tab.'}catch(e){document.querySelector('#status').textContent='无法退出，请检查程序窗口。Could not stop the application.'}}"
            return await Response(script, media_type="text/javascript")(
                scope, receive, send
            )
        if path == "/__stop":
            if (
                scope["method"] != "POST"
                or headers.get("x-desktop-request") != "1"
                or headers.get("content-type") != "application/json"
            ):
                return await Response(status_code=403)(scope, receive, send)
            await JSONResponse({"stopped": True})(scope, receive, send)
            self.stop()
            return
        return await self.app(scope, receive, send)


def user_data_dir(slug):
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return base / slug


def resource_root():
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "resources"
    return Path(__file__).resolve().parents[1] / "build/resources"


def _get(url, cookie=None):
    request = urllib.request.Request(url, headers={"Cookie": cookie} if cookie else {})
    # Ignore proxy environment settings for our local socket.
    return urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
        request, timeout=30
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--resources", type=Path)
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument("--smoke-output", type=Path)
    args = parser.parse_args()
    resources = (args.resources or resource_root()).resolve()
    config = json.loads((resources / "app.json").read_text(encoding="utf-8"))
    # Smoke mode always uses a fresh directory, even if an existing --data-dir is supplied.
    scratch = (
        tempfile.TemporaryDirectory(prefix=config["slug"] + "-smoke-")
        if args.smoke_test
        else None
    )
    data = (
        Path(scratch.name)
        if scratch
        else (args.data_dir or user_data_dir(config["slug"]))
    )
    data.mkdir(parents=True, exist_ok=True)
    application_module = importlib.import_module(config["desktop_module"])
    factory = application_module.create_app
    app = factory(str(data.resolve()), str(resources))
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(32)
    origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
    server = None

    def stop():
        server.should_exit = True

    secured = DesktopSession(app, origin, config["name"], stop)
    server = uvicorn.Server(
        uvicorn.Config(
            secured,
            host="127.0.0.1",
            port=0,
            access_log=False,
            log_config=None,
            log_level="warning",
            ws="none",
        )
    )
    failures = []

    def on_ready():
        try:
            deadline = time.monotonic() + 120
            while not server.started:
                if time.monotonic() > deadline or server.should_exit:
                    raise RuntimeError("Local server did not become ready")
                time.sleep(0.05)
            if args.smoke_test:
                try:
                    _get(origin + config["health_path"])
                    raise AssertionError(
                        "Private endpoint accepted an unauthenticated request"
                    )
                except urllib.error.HTTPError as exc:
                    assert exc.code == 401
                cookie = secured.cookie.split(";", 1)[0]
                with _get(origin + config["health_path"], cookie) as result:
                    assert result.status == 200
                    json.loads(result.read())
                with _get(origin + "/", cookie) as result:
                    assert result.status == 200 and b"<" in result.read()
                if hasattr(application_module, "smoke_test"):
                    application_module.smoke_test(origin, cookie)
                report = {
                    "status": "ok",
                    "version": config["version"],
                    "slug": config["slug"],
                    "platform": sys.platform,
                    "frozen": bool(getattr(sys, "frozen", False)),
                    "resource_dir": str(resources),
                }
                if args.smoke_output:
                    args.smoke_output.write_text(json.dumps(report), encoding="utf-8")
                if sys.stdout:
                    print(json.dumps(report), flush=True)
                stop()
            elif not webbrowser.open(origin + secured.launch_path):
                raise RuntimeError("No default browser could be opened")
        except Exception as exc:  # noqa: BLE001 -- forward a worker failure to the main process
            failures.append(exc)
            stop()

    ready = threading.Thread(target=on_ready, daemon=True)
    ready.start()
    try:
        asyncio.run(server.serve(sockets=[listener]))
        ready.join(timeout=5)
        if failures:
            raise failures[0]
    finally:
        listener.close()
        if scratch:
            scratch.cleanup()


if __name__ == "__main__":
    main()
