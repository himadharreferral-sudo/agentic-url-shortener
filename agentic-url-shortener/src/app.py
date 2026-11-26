import json
import logging
import os
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from src.storage import StorageRepository
from src.service import UrlShortenerService
from src.observability import RequestMetrics


logger = logging.getLogger(__name__)
DASHBOARD_HTML = Path(__file__).with_name("dashboard.html").read_bytes()
MAX_REQUEST_BODY_BYTES = 16 * 1024
REQUEST_TIMEOUT_SECONDS = 10


def _saturation_snapshot(service: UrlShortenerService, metrics: RequestMetrics) -> dict:
    cache = service.cache.snapshot()
    return {
        "active_threads": threading.active_count(),
        "in_flight_requests": metrics.in_flight_requests(),
        "cache_entries": cache["entries"],
        "cache_capacity": cache["capacity"],
        "cache_utilization_ratio": (
            cache["entries"] / cache["capacity"] if cache["capacity"] else 0.0
        ),
    }


def _route_label(method: str, path: str) -> str:
    if path == "/health":
        return "/health"
    if path == "/ready":
        return "/ready"
    if path == "/metrics":
        return "/metrics"
    if path == "/dashboard":
        return "/dashboard"
    if path == "/api/v1/observability":
        return "/api/v1/observability"
    if method == "POST" and path == "/api/v1/links":
        return "/api/v1/links"
    if method == "GET" and path.startswith("/api/v1/links/") and path.endswith("/stats"):
        return "/api/v1/links/{code}/stats"
    if method == "GET" and path.startswith("/") and path.count("/") == 1:
        return "/{code}"
    return "unmatched"


def make_handler(service: UrlShortenerService, metrics: RequestMetrics | None = None):
    request_metrics = metrics or RequestMetrics()

    class ShortenerHandler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(REQUEST_TIMEOUT_SECONDS)

        def handle_one_request(self):
            started_at = time.perf_counter()
            request_metrics.begin_request()
            self.command = None
            self.path = ""
            self._metric_status = 500
            try:
                super().handle_one_request()
            finally:
                method = getattr(self, "command", None)
                if method:
                    route = _route_label(method, urlparse(self.path).path)
                    request_metrics.record_request(
                        method,
                        route,
                        self._metric_status,
                        time.perf_counter() - started_at,
                    )
                else:
                    request_metrics.record_request(
                        "OTHER",
                        "unmatched",
                        self._metric_status,
                        time.perf_counter() - started_at,
                    )

        def send_response(self, code, message=None):
            self._metric_status = code
            super().send_response(code, message)

        def end_headers(self):
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            super().end_headers()

        def _send_json(self, status: int, payload: dict, extra_headers: dict | None = None):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if extra_headers:
                for k, v in extra_headers.items():
                    self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return

        def do_GET(self):
            try:
                self._handle_get()
            except sqlite3.Error:
                logger.exception("SQLite request failed")
                self._send_json(503, {"error": "Storage temporarily unavailable"})

        def _handle_get(self):
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"

            if path == "/health":
                self._send_json(200, {"status": "ok", "circuit": service.circuit_breaker.state})
                return

            if path == "/ready":
                try:
                    service.repo.check_health()
                except sqlite3.Error:
                    logger.exception("SQLite readiness check failed")
                    self._send_json(503, {"status": "not_ready", "dependency": "sqlite"})
                    return
                self._send_json(200, {"status": "ready", "dependency": "sqlite"})
                return

            if path == "/metrics":
                body = request_metrics.render_prometheus(
                    _saturation_snapshot(service, request_metrics)
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if path == "/dashboard":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(DASHBOARD_HTML)))
                self.end_headers()
                self.wfile.write(DASHBOARD_HTML)
                return

            if path == "/api/v1/observability":
                snapshot = request_metrics.dashboard_snapshot()
                snapshot["saturation"].update(
                    _saturation_snapshot(service, request_metrics)
                )
                snapshot["circuit_breaker"] = service.circuit_breaker.state
                self._send_json(200, snapshot)
                return

            if path.startswith("/api/v1/links/") and path.endswith("/stats"):
                parts = path.split("/")
                code = parts[4] if len(parts) > 5 else ""
                status, data = service.get_stats(code)
                self._send_json(status, data)
                return

            if path.startswith("/") and len(path) > 1:
                code = path.lstrip("/")
                client_ip = self.client_address[0] if self.client_address else "0.0.0.0"
                ua = self.headers.get("User-Agent", "unknown")
                ref = self.headers.get("Referer", "")
                status, data = service.resolve(code, user_agent=ua, referer=ref, client_ip=client_ip)
                if status in (301, 302):
                    self.send_response(status)
                    self.send_header("Location", data["location"])
                    self.send_header("X-Short-Code", code)
                    self.end_headers()
                else:
                    self._send_json(status, data)
                return

            self._send_json(404, {"error": "Not found"})

        def do_POST(self):
            try:
                self._handle_post()
            except sqlite3.Error:
                logger.exception("SQLite request failed")
                self._send_json(503, {"error": "Storage temporarily unavailable"})

        def _handle_post(self):
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/")
            if path != "/api/v1/links":
                self._send_json(404, {"error": "Not found"})
                return

            content_lengths = self.headers.get_all("Content-Length", [])
            transfer_encodings = self.headers.get_all("Transfer-Encoding", [])
            if transfer_encodings or len(content_lengths) > 1:
                self.close_connection = True
                self._send_json(
                    400,
                    {"error": "Unsupported or ambiguous request framing"},
                    {"Connection": "close"},
                )
                return
            if not content_lengths:
                self.close_connection = True
                self._send_json(
                    411,
                    {"error": "Content-Length is required"},
                    {"Connection": "close"},
                )
                return
            try:
                length = int(content_lengths[0])
            except ValueError:
                self.close_connection = True
                self._send_json(
                    400,
                    {"error": "Invalid Content-Length"},
                    {"Connection": "close"},
                )
                return
            if length < 0:
                self.close_connection = True
                self._send_json(
                    400,
                    {"error": "Invalid Content-Length"},
                    {"Connection": "close"},
                )
                return
            if length > MAX_REQUEST_BODY_BYTES:
                self.close_connection = True
                self._send_json(
                    413,
                    {"error": "Request body exceeds maximum size"},
                    {"Connection": "close"},
                )
                return

            if length > 0:
                raw = self.rfile.read(length)
                if len(raw) != length:
                    self.close_connection = True
                    self._send_json(
                        400,
                        {"error": "Incomplete request body"},
                        {"Connection": "close"},
                    )
                    return
            else:
                raw = b"{}"
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send_json(400, {"error": "Invalid JSON body"})
                return
            if not isinstance(payload, dict):
                self._send_json(400, {"error": "JSON body must be an object"})
                return

            client_ip = self.client_address[0] if self.client_address else "0.0.0.0"
            status, data = service.shorten(
                long_url=payload.get("long_url", ""),
                custom_code=payload.get("custom_code"),
                ttl_seconds=payload.get("ttl_seconds"),
                client_key=client_ip,
                metadata=payload.get("metadata"),
                redirect_status=payload.get("redirect_status", 302),
            )
            self._send_json(status, data)

    return ShortenerHandler


def run_server(
    host: str = "127.0.0.1",
    port: int = 8080,
    db_path: str = "shortener.db",
    base_url: str | None = None,
):
    if base_url is None:
        public_host = "localhost" if host in {"0.0.0.0", "::"} else host
        if ":" in public_host and not public_host.startswith("["):
            public_host = f"[{public_host}]"
        base_url = f"http://{public_host}:{port}"

    startup_started = time.perf_counter()
    repo = StorageRepository(db_path=db_path)
    service = UrlShortenerService(repo=repo, base_url=base_url)
    metrics = RequestMetrics()
    server = ThreadingHTTPServer((host, port), make_handler(service, metrics))
    metrics.set_startup_duration(time.perf_counter() - startup_started)
    print(f"URL Shortener listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        repo.close()


def main() -> None:
    host = os.environ.get("HOST", "127.0.0.1")
    raw_port = os.environ.get("PORT", "8080")
    db_path = os.environ.get("DB_PATH", "shortener.db")
    base_url = os.environ.get("PUBLIC_BASE_URL")

    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ValueError("PORT must be an integer") from exc
    if not host.strip():
        raise ValueError("HOST must not be empty")
    if not 1 <= port <= 65535:
        raise ValueError("PORT must be between 1 and 65535")
    if not db_path.strip():
        raise ValueError("DB_PATH must not be empty")
    if base_url is not None and not base_url.strip():
        raise ValueError("PUBLIC_BASE_URL must not be empty when set")

    run_server(host=host, port=port, db_path=db_path, base_url=base_url)


if __name__ == "__main__":
    main()
