import unittest
import time
from src.storage import StorageRepository
from src.service import UrlShortenerService
from src.reliability import SlidingWindowRateLimiter, CircuitBreaker
from src.encoding import decimal_to_base62, base62_to_decimal
from src.cache import LRUCache


class TestUrlShortener(unittest.TestCase):
    def setUp(self):
        self.repo = StorageRepository(":memory:")
        self.service = UrlShortenerService(repo=self.repo, base_url="http://localhost:8080")

    def tearDown(self):
        self.repo.close()

    def test_shorten_and_redirect_and_analytics(self):
        status, created = self.service.shorten("https://example.com/articles/agentic-sdlc", custom_code="agentic1")
        self.assertEqual(status, 201)
        self.assertEqual(created["short_url"], "http://localhost:8080/agentic1")

        r_status, r_data = self.service.resolve("agentic1", user_agent="TestAgent/1.0", client_ip="10.0.0.5")
        self.assertEqual(r_status, 302)
        self.assertEqual(r_data["location"], "https://example.com/articles/agentic-sdlc")

        s_status, stats = self.service.get_stats("agentic1")
        self.assertEqual(s_status, 200)
        self.assertEqual(stats["total_clicks"], 1)
        self.assertEqual(stats["unique_recent_visitors"], 1)
        self.assertNotIn("referer", stats["recent_events"][0])
        self.assertNotIn("user_agent", stats["recent_events"][0])
        self.assertNotIn("client_ip_hash", stats["recent_events"][0])

    def test_ssrf_and_invalid_url_rejected(self):
        bad_urls = [
            "http://127.0.0.1/admin",
            "http://192.168.1.1/admin",
            "http://[::1]/admin",
            "http://2130706433/admin",
            "http://0x7f000001/admin",
            "http://localhost:8080/secret",
            "http://user:password@example.com/",
            "http://example.com:99999/",
            "javascript:alert(1)",
            "not-a-url",
        ]
        for u in bad_urls:
            status, _ = self.service.shorten(u)
            self.assertEqual(status, 400, f"Expected 400 for {u}")
        self.assertTrue(self.service.shorten("https://8.8.8.8/")[0] in (200, 201))

    def test_ttl_expiration(self):
        status, _ = self.service.shorten("https://example.com/temp", custom_code="temp1", ttl_seconds=1)
        self.assertEqual(status, 201)
        time.sleep(1.05)
        r_status, _ = self.service.resolve("temp1")
        self.assertEqual(r_status, 410)

    def test_rate_limiter_and_circuit_breaker(self):
        limiter = SlidingWindowRateLimiter(max_requests=2, window_seconds=60)
        svc = UrlShortenerService(repo=self.repo, rate_limiter=limiter)
        self.assertEqual(svc.shorten("https://example.com/1", client_key="t1")[0], 201)
        self.assertEqual(svc.shorten("https://example.com/2", client_key="t1")[0], 201)
        blocked_status, _ = svc.shorten("https://example.com/3", client_key="t1")
        self.assertEqual(blocked_status, 429)

        cb = CircuitBreaker(failure_threshold=2, recovery_seconds=5.0)
        now = 1000.0
        self.assertTrue(cb.allow_request(now))
        cb.record_failure(now)
        cb.record_failure(now + 1)
        self.assertEqual(cb.state, "OPEN")
        self.assertFalse(cb.allow_request(now + 2))
        self.assertTrue(cb.allow_request(now + 12))

    def test_base62_encoding(self):
        self.assertEqual(decimal_to_base62(125), "21")
        self.assertEqual(decimal_to_base62(620), "a0")
        self.assertEqual(base62_to_decimal("21"), 125)

    def test_configurable_redirect_and_cache(self):
        cache = LRUCache(capacity=2)
        service = UrlShortenerService(
            repo=self.repo,
            base_url="http://localhost:8080",
            cache=cache,
        )
        status, created = service.shorten(
            "https://example.com/permanent",
            custom_code="perm1",
            redirect_status=301,
        )
        self.assertEqual(status, 201)

        status, redirect = service.resolve("perm1")
        self.assertEqual(status, 301)
        self.assertTrue(redirect["cache_hit"])

        status, redirect = service.resolve("perm1")
        self.assertEqual(status, 301)
        self.assertTrue(redirect["cache_hit"])

    def test_generated_code_is_base62(self):
        status, created = self.service.shorten("https://example.com/base62")
        self.assertEqual(status, 201)
        self.assertGreater(created["numeric_id"], 0)
        self.assertEqual(
            created["base62_code"],
            decimal_to_base62(created["numeric_id"]),
        )

    def test_malformed_custom_code_and_ttl_types_are_rejected(self):
        self.assertEqual(
            self.service.shorten("https://example.com/type", custom_code=123)[0],
            400,
        )
        self.assertEqual(
            self.service.shorten("https://example.com/type", ttl_seconds=True)[0],
            400,
        )


if __name__ == "__main__":
    unittest.main()
