import threading
import time
import math
from collections import defaultdict
from typing import Dict, Tuple


LATENCY_BUCKETS_SECONDS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)
ROLLING_WINDOW_SECONDS = 60 * 60
SLO_AVAILABILITY_TARGET = 0.999
SLO_REDIRECT_P95_SECONDS = 0.1
PROCESS_START_TIME_SECONDS = time.time()


class RequestMetrics:
    def __init__(self):
        self._lock = threading.Lock()
        self._request_counts: Dict[Tuple[str, str, int], int] = defaultdict(int)
        self._duration_counts: Dict[Tuple[str, str, int], list[int]] = defaultdict(
            lambda: [0] * (len(LATENCY_BUCKETS_SECONDS) + 1)
        )
        self._duration_sums: Dict[Tuple[str, str, int], float] = defaultdict(float)
        self._startup_duration_seconds = None
        self._in_flight_requests = 0
        self._created_at = time.time()
        self._rolling_seconds = [None] * ROLLING_WINDOW_SECONDS

    def set_startup_duration(self, duration_seconds: float) -> None:
        with self._lock:
            self._startup_duration_seconds = max(0.0, duration_seconds)

    def begin_request(self) -> None:
        with self._lock:
            self._in_flight_requests += 1

    def record_request(self, method: str, route: str, status: int, duration_seconds: float) -> None:
        method = method if method in {"GET", "POST", "PUT", "DELETE", "HEAD"} else "OTHER"
        duration_seconds = max(0.0, duration_seconds)
        now = time.time()
        with self._lock:
            self._in_flight_requests = max(0, self._in_flight_requests - 1)
            self._request_counts[(method, route, status)] += 1
            key = (method, route, status)
            self._duration_sums[key] += duration_seconds
            for index, boundary in enumerate(LATENCY_BUCKETS_SECONDS):
                if duration_seconds <= boundary:
                    self._duration_counts[key][index] += 1
            self._duration_counts[key][-1] += 1
            if route not in {"/health", "/ready", "/metrics", "/dashboard", "/api/v1/observability"}:
                second = int(now)
                slot_index = second % len(self._rolling_seconds)
                slot = self._rolling_seconds[slot_index]
                if slot is None or slot["second"] != second:
                    slot = {
                        "second": second,
                        "total": 0,
                        "errors": 0,
                        "latency_buckets": [0] * (len(LATENCY_BUCKETS_SECONDS) + 1),
                    }
                    self._rolling_seconds[slot_index] = slot
                slot["total"] += 1
                if status >= 500:
                    slot["errors"] += 1
                for index, boundary in enumerate(LATENCY_BUCKETS_SECONDS):
                    if duration_seconds <= boundary:
                        slot["latency_buckets"][index] += 1
                slot["latency_buckets"][-1] += 1

    def in_flight_requests(self) -> int:
        with self._lock:
            return self._in_flight_requests

    def dashboard_snapshot(self) -> dict:
        now = time.time()
        now_second = int(now)
        with self._lock:
            slots = [
                dict(slot, latency_buckets=slot["latency_buckets"][:])
                for slot in self._rolling_seconds
                if slot is not None and 0 <= now_second - slot["second"] < len(self._rolling_seconds)
            ]
            startup_duration = self._startup_duration_seconds
            in_flight = self._in_flight_requests
            created_at = self._created_at

        total_requests = sum(slot["total"] for slot in slots)
        errors = sum(slot["errors"] for slot in slots)
        latency_buckets = [
            sum(slot["latency_buckets"][index] for slot in slots)
            for index in range(len(LATENCY_BUCKETS_SECONDS) + 1)
        ]
        elapsed_seconds = min(len(self._rolling_seconds), max(1.0, now - created_at))
        p50_seconds = self._bucket_quantile(latency_buckets, total_requests, 0.50)
        p95_seconds = self._bucket_quantile(latency_buckets, total_requests, 0.95)
        return {
            "timestamp": now,
            "window_seconds": int(elapsed_seconds),
            "traffic": {
                "requests": total_requests,
                "requests_per_second": round(total_requests / elapsed_seconds, 3),
            },
            "errors": {
                "server_errors": errors,
                "rate_percent": round((errors / total_requests) * 100, 3) if total_requests else 0.0,
            },
            "latency": {
                "sample_count": total_requests,
                "p50_seconds_approx": p50_seconds,
                "p95_seconds_approx": p95_seconds,
            },
            "saturation": {
                "in_flight_requests": in_flight,
            },
            "startup_duration_seconds": startup_duration,
        }

    @staticmethod
    def _bucket_quantile(bucket_counts: list[int], total: int, quantile: float):
        if total == 0:
            return None
        threshold = math.ceil(total * quantile)
        for index, boundary in enumerate(LATENCY_BUCKETS_SECONDS):
            if bucket_counts[index] >= threshold:
                return boundary
        if bucket_counts[-1] >= threshold:
            return f">{LATENCY_BUCKETS_SECONDS[-1]}"
        return None

    def render_prometheus(self, saturation: dict | None = None) -> str:
        with self._lock:
            request_counts = dict(self._request_counts)
            duration_counts = {key: values[:] for key, values in self._duration_counts.items()}
            duration_sums = dict(self._duration_sums)
            startup_duration = self._startup_duration_seconds
            in_flight = self._in_flight_requests

        lines = [
            "# HELP url_shortener_process_start_time_seconds Unix time when the metrics module loaded.",
            "# TYPE url_shortener_process_start_time_seconds gauge",
            f"url_shortener_process_start_time_seconds {PROCESS_START_TIME_SECONDS:.6f}",
            "# HELP url_shortener_slo_availability_target Example availability SLO target; configure for your service.",
            "# TYPE url_shortener_slo_availability_target gauge",
            f"url_shortener_slo_availability_target {SLO_AVAILABILITY_TARGET}",
            "# HELP url_shortener_slo_redirect_p95_seconds Example redirect latency objective.",
            "# TYPE url_shortener_slo_redirect_p95_seconds gauge",
            f"url_shortener_slo_redirect_p95_seconds {SLO_REDIRECT_P95_SECONDS}",
            "# HELP url_shortener_http_requests_in_flight Requests currently being handled.",
            "# TYPE url_shortener_http_requests_in_flight gauge",
            f"url_shortener_http_requests_in_flight {in_flight}",
        ]
        if saturation:
            lines.extend([
                "# HELP url_shortener_active_threads Current Python thread count.",
                "# TYPE url_shortener_active_threads gauge",
                f"url_shortener_active_threads {saturation['active_threads']}",
                "# HELP url_shortener_cache_entries Number of links currently in the LRU cache.",
                "# TYPE url_shortener_cache_entries gauge",
                f"url_shortener_cache_entries {saturation['cache_entries']}",
                "# HELP url_shortener_cache_capacity Maximum LRU cache entries.",
                "# TYPE url_shortener_cache_capacity gauge",
                f"url_shortener_cache_capacity {saturation['cache_capacity']}",
                "# HELP url_shortener_cache_utilization_ratio LRU entries divided by its capacity.",
                "# TYPE url_shortener_cache_utilization_ratio gauge",
                f"url_shortener_cache_utilization_ratio {saturation['cache_utilization_ratio']:.6f}",
            ])
        if startup_duration is not None:
            lines.extend([
                "# HELP url_shortener_startup_duration_seconds Time from server initialization start until socket bind.",
                "# TYPE url_shortener_startup_duration_seconds gauge",
                f"url_shortener_startup_duration_seconds {startup_duration:.6f}",
            ])

        lines.extend([
            "# HELP http_requests_total Total HTTP requests handled by this process.",
            "# TYPE http_requests_total counter",
        ])
        for (method, route, status), count in sorted(request_counts.items()):
            lines.append(
                f'http_requests_total{{method="{method}",route="{route}",status="{status}"}} {count}'
            )

        lines.extend([
            "# HELP http_request_duration_seconds Request duration histogram.",
            "# TYPE http_request_duration_seconds histogram",
        ])
        for (method, route, status), counts in sorted(duration_counts.items()):
            for index, boundary in enumerate(LATENCY_BUCKETS_SECONDS):
                lines.append(
                    f'http_request_duration_seconds_bucket{{method="{method}",route="{route}",status="{status}",le="{boundary:g}"}} {counts[index]}'
                )
            lines.append(
                f'http_request_duration_seconds_bucket{{method="{method}",route="{route}",status="{status}",le="+Inf"}} {counts[-1]}'
            )
            lines.append(
                f'http_request_duration_seconds_sum{{method="{method}",route="{route}",status="{status}"}} {duration_sums[(method, route, status)]:.6f}'
            )
            lines.append(
                f'http_request_duration_seconds_count{{method="{method}",route="{route}",status="{status}"}} {counts[-1]}'
            )
        return "\n".join(lines) + "\n"
