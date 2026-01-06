from __future__ import annotations

import hashlib
import hmac
import ipaddress
import threading
import time
from dataclasses import dataclass
from typing import Optional

from fastapi import Request

from app.settings import get_settings


@dataclass(frozen=True)
class RateLimitPolicy:
    """
    Per-category rate limit policy.

    We apply *both* IP-based and user/principal-based limits (when a principal is
    available). This matches OWASP guidance: IP limits mitigate anonymous abuse
    and scraping, while user limits prevent a single authenticated account from
    overwhelming the service.
    """

    ip_requests: int
    principal_requests: int


class RateLimitExceeded(Exception):
    def __init__(
        self,
        *,
        policy_name: str,
        limit_kind: str,
        limit: int,
        window_seconds: int,
        retry_after_seconds: int,
    ) -> None:
        super().__init__("Rate limit exceeded")
        self.policy_name = policy_name
        self.limit_kind = limit_kind  # "ip" | "principal"
        self.limit = int(limit)
        self.window_seconds = int(window_seconds)
        self.retry_after_seconds = int(retry_after_seconds)


class FixedWindowRateLimiter:
    """
    Simple in-memory fixed-window rate limiter.

    Notes:
    - This is per-process. If you run multiple workers/replicas, each will enforce
      its own limits (still useful, but not globally consistent). For strict global
      limits, use a shared store like Redis.
    - We intentionally keep the implementation small and dependency-free.
    """

    def __init__(self, *, window_seconds: int, max_keys: int = 50_000) -> None:
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        self._window_seconds = int(window_seconds)
        self._max_keys = int(max_keys)
        self._lock = threading.Lock()
        self._counters: dict[str, tuple[int, int]] = {}
        self._last_prune_at = 0

    def _window_start(self, now: float) -> int:
        return int(now // self._window_seconds) * self._window_seconds

    def hit(self, *, key: str, limit: int, now: float) -> Optional[int]:
        """
        Increment and return retry-after seconds if the limit is exceeded.

        Returns:
          - None if allowed
          - retry-after seconds (>=1) if blocked
        """

        if limit <= 0:
            # A limit of 0 means "block everything" – still supported.
            return self._window_seconds

        window_start = self._window_start(now)
        with self._lock:
            existing = self._counters.get(key)
            if existing and existing[0] == window_start:
                count = existing[1]
            else:
                count = 0

            if count >= limit:
                retry_after = int((window_start + self._window_seconds) - now)
                return max(1, retry_after)

            self._counters[key] = (window_start, count + 1)

            # Opportunistic pruning to avoid unbounded growth.
            if len(self._counters) > self._max_keys and (now - self._last_prune_at) > 10:
                self._prune(now)

        return None

    def _prune(self, now: float) -> None:
        self._last_prune_at = now
        cutoff = self._window_start(now) - self._window_seconds
        expired = [k for k, (start, _) in self._counters.items() if start < cutoff]
        for k in expired:
            self._counters.pop(k, None)


_limiter: FixedWindowRateLimiter | None = None


def _get_limiter() -> FixedWindowRateLimiter:
    global _limiter
    if _limiter is None:
        settings = get_settings()
        _limiter = FixedWindowRateLimiter(window_seconds=settings.rate_limit_window_seconds)
    return _limiter


def _client_ip(request: Request) -> str:
    settings = get_settings()
    if settings.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            # Left-most is the original client. We only accept syntactically-valid
            # IPs to avoid header spoofing quirks.
            candidate = forwarded.split(",")[0].strip()
            try:
                return str(ipaddress.ip_address(candidate))
            except ValueError:
                pass

        real_ip = request.headers.get("x-real-ip")
        if real_ip:
            candidate = real_ip.strip()
            try:
                return str(ipaddress.ip_address(candidate))
            except ValueError:
                pass

    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _principal(request: Request) -> Optional[str]:
    """
    Best-effort principal for user-based limits.

    - For logged-in users: session "user" (email or "admin")
    - For ingest: a stable hash of the provided secret (never store raw secrets)
    """

    if "session" in request.scope:
        try:
            user = request.session.get("user")
        except Exception:
            user = None
        if user:
            return f"user:{user}"

    ingest_secret = request.headers.get("x-ingest-secret")
    if ingest_secret:
        # Only treat the ingest secret as an identity if it matches the configured secret;
        # otherwise a caller could spoof random values to create unbounded buckets.
        settings = get_settings()
        valid_secrets = tuple(getattr(settings, "ingest_secrets", ())) or (
            (settings.ingest_secret,) if settings.ingest_secret else ()
        )
        if valid_secrets and any(hmac.compare_digest(ingest_secret, s) for s in valid_secrets):
            digest = hashlib.sha256(ingest_secret.encode("utf-8")).hexdigest()
            # Short prefix keeps keys compact while avoiding collisions in practice.
            return f"ingest:{digest[:16]}"

    return None


def _policy_for_path(path: str) -> tuple[str, RateLimitPolicy]:
    settings = get_settings()

    if path.startswith("/api/ingest"):
        return (
            "ingest",
            RateLimitPolicy(
                ip_requests=settings.rate_limit_ingest_ip,
                principal_requests=settings.rate_limit_ingest_principal,
            ),
        )

    if path in {"/login", "/signup", "/subscribe"}:
        return (
            "auth",
            RateLimitPolicy(
                ip_requests=settings.rate_limit_auth_ip,
                principal_requests=settings.rate_limit_auth_principal,
            ),
        )

    if path == "/api/health":
        return (
            "health",
            RateLimitPolicy(
                ip_requests=settings.rate_limit_health_ip,
                principal_requests=settings.rate_limit_health_principal,
            ),
        )

    return (
        "default",
        RateLimitPolicy(
            ip_requests=settings.rate_limit_default_ip,
            principal_requests=settings.rate_limit_default_principal,
        ),
    )


def _hmac_digest(value: str) -> str:
    # Used for internal cache keys to avoid storing raw user IDs in memory.
    # This does not need to be secret, only stable.
    return hmac.new(b"rate-limit", value.encode("utf-8"), hashlib.sha256).hexdigest()


async def rate_limit_dependency(request: Request) -> None:
    """
    FastAPI dependency that enforces rate limits on all routed endpoints.

    Mounted static files ("/static/*") are intentionally excluded to avoid breaking
    normal page loads (CSS/images can cause a burst of requests).
    """

    settings = get_settings()
    if not settings.rate_limit_enabled:
        return

    path = request.url.path
    if path.startswith("/static/"):
        return

    policy_name, policy = _policy_for_path(path)

    limiter = _get_limiter()
    now = time.time()

    ip = _client_ip(request)
    ip_key = f"{policy_name}:ip:{_hmac_digest(ip)}"
    retry_after = limiter.hit(key=ip_key, limit=policy.ip_requests, now=now)
    if retry_after is not None:
        raise RateLimitExceeded(
            policy_name=policy_name,
            limit_kind="ip",
            limit=policy.ip_requests,
            window_seconds=settings.rate_limit_window_seconds,
            retry_after_seconds=retry_after,
        )

    principal = _principal(request)
    if principal:
        principal_key = f"{policy_name}:principal:{_hmac_digest(principal)}"
        retry_after = limiter.hit(
            key=principal_key,
            limit=policy.principal_requests,
            now=now,
        )
        if retry_after is not None:
            raise RateLimitExceeded(
                policy_name=policy_name,
                limit_kind="principal",
                limit=policy.principal_requests,
                window_seconds=settings.rate_limit_window_seconds,
                retry_after_seconds=retry_after,
            )
