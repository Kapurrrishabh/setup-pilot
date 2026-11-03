"""Keep the local web app usable only from this machine's own browser tabs.

The server listens on loopback only. These checks stop other websites open in your browser from
using it (cross-site requests and DNS rebinding), and stop a stuck page from flooding it.
"""

import secrets
import threading
import time
from collections import deque
from typing import get_args

from flask import Flask, abort, jsonify, request

from setuppilot.config import WebConfig, WebHost

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_GUARDED_GETS = {"/api/preflight"}
_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'; base-uri 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def protect(app: Flask, config: WebConfig) -> str:
    """Add the checks to every request. Returns the CSRF token pages must send back."""
    # A fresh random token each time the app starts; it never leaves this machine's pages.
    token = secrets.token_urlsafe(32)
    allowed_hosts = {f"{h}:{config.port}" for h in get_args(WebHost)} | {f"[::1]:{config.port}"}
    allowed_origins = {f"http://{h}" for h in allowed_hosts}
    posts = _PostLimit(config.posts_per_minute)
    app.config["MAX_CONTENT_LENGTH"] = config.max_request_bytes
    app.config["SESSION_COOKIE_SAMESITE"] = "Strict"
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.secret_key = secrets.token_bytes(32)

    @app.before_request
    def check_request():
        # A page on another site that points a hostname at 127.0.0.1 sends its own Host.
        if request.host not in allowed_hosts:
            abort(400, f"Unknown host. Open SetupPilot at http://127.0.0.1:{config.port}")
        # The pre-flight check runs programs, so it is guarded like a change.
        if request.method in _SAFE_METHODS and request.path not in _GUARDED_GETS:
            return None
        origin = request.headers.get("Origin")
        if origin is not None and origin not in allowed_origins:
            abort(403, "Requests from other websites are not allowed.")
        sent = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token", "")
        if not secrets.compare_digest(sent.encode(), token.encode()):
            abort(403, "This page is out of date (the app restarted?). Reload it and try again.")
        if not posts.allow():
            return jsonify(
                error={"message": "Too many requests.", "details": "Wait a minute."}
            ), 429
        return None

    @app.after_request
    def add_headers(response):
        response.headers.update(_HEADERS)
        return response

    return token


class _PostLimit:
    """At most `per_minute` changing requests in any 60 seconds, for the whole app."""

    def __init__(self, per_minute: int) -> None:
        self._per_minute = per_minute
        self._times: deque[float] = deque()
        self._lock = threading.Lock()

    def allow(self) -> bool:
        now = time.monotonic()
        with self._lock:
            while self._times and self._times[0] <= now - 60:
                self._times.popleft()
            if len(self._times) >= self._per_minute:
                return False
            self._times.append(now)
            return True
