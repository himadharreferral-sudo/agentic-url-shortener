import json
import os
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from src.app import main, make_handler
from src.observability import RequestMetrics
from src.reliability import SlidingWindowRateLimiter
from src.service import UrlShortenerService
from src.storage import StorageRepository


class TestRequestMetrics(unittest.TestCase):
    def test_dashboard_uses_one_hour_rolling_window(self):
        with patch("src.observability.time.time", return_value=1000):
            metrics = RequestMetrics()
        with patch("src.observability.time.time", return_value=1061):
            metrics.record_request("GET", "/{code}", 404, 0.01)
            snapshot = metrics.dashboard_snapshot()
        self.assertEqual(snapshot["traffic"]["requests"], 1)

        with patch("src.observability.time.time", return_value=4662):
            snapshot = metrics.dashboard_snapshot()
        self.assertEqual(snapshot["window_seconds"], 3600)
        self.assertEqual(snapshot["traffic"]["requests"], 0)

    def test_prometheus_metrics_include_counts_latency_and_startup(self):
        metrics = RequestMetrics()
        metrics.set_startup_duration(0.042)
        metrics.record_request("GET", "/{code}", 302, 0.02)
        metrics.record_request("POST", "/api/v1/links", 503, 0.6)

        output = metrics.render_prometheus()

        self.assertIn('http_requests_total{method="GET",route="/{code}",status="302"} 1', output)
        self.assertIn('http_requests_total{method="POST",route="/api/v1/links",status="503"} 1', output)
        self.assertIn('http_request_duration_seconds_bucket{method="GET",route="/{code}",status="302",le="0.025"} 1', output)
        self.assertIn("url_shortener_startup_duration_seconds 0.042000", output)


class TestAppConfiguration(unittest.TestCase):
    def test_main_reads_container_environment(self):
        environment = {
            "HOST": "0.0.0.0",
            "PORT": "8181",
            "DB_PATH": "/data/shortener.db",
            "PUBLIC_BASE_URL": "https://short.example.com",
        }
        with patch.dict(os.environ, environment, clear=True):
            with patch("src.app.run_server") as run_server:
                main()
        run_server.assert_called_once_with(
            host="0.0.0.0",
            port=8181,
            db_path="/data/shortener.db",
            base_url="https://short.example.com",
        )

    def test_main_rejects_invalid_port(self):
        with patch.dict(os.environ, {"PORT": "not-a-port"}, clear=True):
            with self.assertRaisesRegex(ValueError, "PORT must be an integer"):
                main()


class TestSREEndpoints(unittest.TestCase):
    def setUp(self):
        self.repo = StorageRepository(":memory:")
        self.service = UrlShortenerService(self.repo)
        self.metrics = RequestMetrics()
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(self.service, self.metrics),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = "http://127.0.0.1:{0}".format(self.server.server_address[1])

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.repo.close()

    def test_readiness_and_metrics_endpoints(self):
        with urlopen(self.base_url + "/ready") as response:
            self.assertEqual(response.status, 200)
            self.assertIn(b'"status": "ready"', response.read())
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
            self.assertEqual(response.headers["X-Frame-Options"], "DENY")

        with urlopen(self.base_url + "/dashboard") as response:
            self.assertEqual(response.status, 200)
            dashboard = response.read()
            self.assertIn(b"URL Shortener", dashboard)
            self.assertIn(b"rolling 1-hour view", dashboard)

        with urlopen(self.base_url + "/api/v1/observability") as response:
            snapshot = json.loads(response.read().decode("utf-8"))
            self.assertIn("traffic", snapshot)
            self.assertIn("errors", snapshot)
            self.assertIn("latency", snapshot)
            self.assertEqual(snapshot["saturation"]["cache_utilization_ratio"], 0)

        with urlopen(self.base_url + "/metrics") as response:
            metrics_before = response.read().decode("utf-8")
            self.assertEqual(response.status, 200)
            self.assertIn("url_shortener_slo_availability_target 0.999", metrics_before)
            self.assertIn("url_shortener_http_requests_in_flight", metrics_before)
            self.assertIn("url_shortener_cache_utilization_ratio", metrics_before)

        with urlopen(self.base_url + "/metrics") as response:
            metrics_after = response.read().decode("utf-8")
        self.assertIn('http_requests_total{method="GET",route="/ready",status="200"} 1', metrics_after)
        self.assertIn('http_requests_total{method="GET",route="/metrics",status="200"} 1', metrics_after)

    def test_readiness_returns_503_when_database_is_unavailable(self):
        self.repo.close()
        with self.assertRaises(HTTPError) as error:
            urlopen(self.base_url + "/ready")
        self.assertEqual(error.exception.code, 503)
        self.assertIn(b'"status": "not_ready"', error.exception.read())

    def test_oversized_create_body_is_rejected(self):
        request = Request(
            self.base_url + "/api/v1/links",
            data=b"x" * (17 * 1024),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as error:
            urlopen(request)
        self.assertEqual(error.exception.code, 413)

    def test_client_cannot_spoof_rate_limit_identity(self):
        self.service.rate_limiter = SlidingWindowRateLimiter(max_requests=1, window_seconds=60)
        statuses = []
        for index in (1, 2):
            body = json.dumps({
                "long_url": "https://example.com/tenant-{0}".format(index),
                "tenant_id": "spoofed-tenant-{0}".format(index),
            }).encode("utf-8")
            request = Request(
                self.base_url + "/api/v1/links",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urlopen(request) as response:
                    statuses.append(response.status)
            except HTTPError as error:
                statuses.append(error.code)
        self.assertEqual(statuses, [201, 429])


if __name__ == "__main__":
    unittest.main()
