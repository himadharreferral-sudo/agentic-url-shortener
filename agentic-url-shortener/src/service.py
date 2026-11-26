import hashlib
import time
from typing import Optional, Dict, Any, Tuple
from src.models import ShortLink, ClickEvent
from src.security import validate_long_url, validate_custom_code, hash_client_ip
from src.reliability import SlidingWindowRateLimiter, CircuitBreaker
from src.storage import StorageRepository
from src.encoding import decimal_to_base62
from src.cache import LRUCache


class UrlShortenerService:
    def __init__(
        self,
        repo: StorageRepository,
        base_url: str = "http://localhost:8080",
        rate_limiter: Optional[SlidingWindowRateLimiter] = None,
        circuit_breaker: Optional[CircuitBreaker] = None,
        cache: Optional[LRUCache] = None,
    ):
        self.repo = repo
        self.base_url = base_url.rstrip("/")
        self.rate_limiter = rate_limiter or SlidingWindowRateLimiter()
        self.circuit_breaker = circuit_breaker or CircuitBreaker()
        self.cache = cache or LRUCache()
        self._fallback_cache: Dict[str, ShortLink] = {}

    def _generate_code(self, long_url: str, salt: int = 0) -> str:
        digest = hashlib.sha256(f"{long_url}:{salt}".encode("utf-8")).hexdigest()
        return digest[:7]

    def shorten(
        self,
        long_url: str,
        custom_code: Optional[str] = None,
        ttl_seconds: Optional[int] = None,
        client_key: str = "default",
        metadata: Optional[Dict[str, Any]] = None,
        redirect_status: int = 302,
    ) -> Tuple[int, Dict[str, Any]]:
        if not self.rate_limiter.allow(client_key):
            return 429, {"error": "Rate limit exceeded", "retry_after_seconds": 60}

        valid_url, url_msg = validate_long_url(long_url)
        if not valid_url:
            return 400, {"error": url_msg}

        if ttl_seconds is not None and (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, int)
            or ttl_seconds < 0
        ):
            return 400, {"error": "ttl_seconds must be a non-negative integer"}
        if metadata is not None and not isinstance(metadata, dict):
            return 400, {"error": "metadata must be a JSON object"}

        now = time.time()
        expires_at = (now + ttl_seconds) if ttl_seconds and ttl_seconds > 0 else None
        try:
            redirect_status = int(redirect_status)
        except (TypeError, ValueError):
            return 400, {"error": "redirect_status must be 301 or 302"}
        if redirect_status not in (301, 302):
            return 400, {"error": "redirect_status must be 301 or 302"}

        if custom_code:
            valid_code, code_msg = validate_custom_code(custom_code)
            if not valid_code:
                return 400, {"error": code_msg}
            code = custom_code
        else:
            existing = self.repo.find_link_by_url(long_url.strip())
            if existing and (existing.expires_at is None or existing.expires_at > now):
                return 200, {
                    "code": existing.code,
                    "short_url": f"{self.base_url}/{existing.code}",
                    "long_url": existing.long_url,
                    "expires_at": existing.expires_at,
                    "idempotent_hit": True,
                }
            numeric_id = self.repo.allocate_numeric_id()
            code = decimal_to_base62(numeric_id)

        if custom_code:
            numeric_id = self.repo.allocate_numeric_id()

        normalized_metadata = dict(metadata or {})
        normalized_metadata["redirect_status"] = redirect_status
        link = ShortLink(
            code=code,
            long_url=long_url.strip(),
            created_at=now,
            numeric_id=numeric_id,
            expires_at=expires_at,
            metadata=normalized_metadata,
        )

        if not self.circuit_breaker.allow_request(now):
            self._fallback_cache[code] = link
            return 202, {
                "code": link.code,
                "short_url": f"{self.base_url}/{link.code}",
                "long_url": link.long_url,
                "degraded_mode": "in_memory_fallback",
            }

        try:
            created = self.repo.create_link(link)
            if not created:
                return 409, {"error": f"Short code '{code}' already exists"}
            self.circuit_breaker.record_success()
            self._fallback_cache[code] = link
            self.cache.set(code, link)
            return 201, {
                "code": link.code,
                "short_url": f"{self.base_url}/{link.code}",
                "long_url": link.long_url,
                "expires_at": link.expires_at,
                "numeric_id": link.numeric_id,
                "base62_code": link.code,
                "redirect_status": redirect_status,
            }
        except Exception as exc:
            self.circuit_breaker.record_failure(now)
            self._fallback_cache[code] = link
            return 503, {"error": "Storage temporarily degraded", "detail": str(exc)}

    def resolve(
        self,
        code: str,
        user_agent: str = "unknown",
        referer: str = "",
        client_ip: str = "127.0.0.1",
    ) -> Tuple[int, Dict[str, Any]]:
        start = time.perf_counter()
        now = time.time()
        link = self.cache.get(code)
        cache_hit = link is not None
        if not cache_hit:
            link = self.repo.get_link(code) or self._fallback_cache.get(code)
            if link is not None:
                self.cache.set(code, link)
        if not link or not link.is_active:
            return 404, {"error": "Short link not found or inactive"}
        if link.expires_at is not None and now >= link.expires_at:
            return 410, {"error": "Short link has expired"}

        latency_ms = (time.perf_counter() - start) * 1000.0
        redirect_status = int(link.metadata.get("redirect_status", 302))
        event = ClickEvent(
            code=code,
            timestamp=now,
            user_agent=user_agent[:512],
            referer=referer[:2048],
            client_ip_hash=hash_client_ip(client_ip),
            redirect_status=redirect_status,
            latency_ms=latency_ms,
        )
        try:
            self.repo.record_click_and_increment(event)
            self.circuit_breaker.record_success()
        except Exception:
            self.circuit_breaker.record_failure(now)

        return redirect_status, {
            "location": link.long_url,
            "code": code,
            "cache_hit": cache_hit,
        }

    def get_stats(self, code: str) -> Tuple[int, Dict[str, Any]]:
        stats = self.repo.get_analytics(code)
        if not stats:
            return 404, {"error": "Short link not found"}
        return 200, stats
