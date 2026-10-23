import time
import logging
from collections import defaultdict, deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

logger = logging.getLogger("uvicorn.error")


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    In-memory sliding-window rate limiter applied per client IP to configured path prefixes.
    Extracts the true client IP even when behind reverse proxies (Cloudflare, Vercel, Nginx).
    """

    def __init__(self, app, limits: dict):
        # limits: {"/path/prefix": (max_requests, window_seconds), ...}
        super().__init__(app)
        self.limits = limits
        self.buckets = defaultdict(deque)

    def _get_client_ip(self, request) -> str:
        # Check standard reverse proxy headers
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
        cf_ip = request.headers.get("cf-connecting-ip")
        if cf_ip:
            return cf_ip.strip()
        real_ip = request.headers.get("x-real-ip")
        if real_ip:
            return real_ip.strip()
        return request.client.host if request.client else "unknown"

    async def dispatch(self, request, call_next):
        path = request.url.path
        matched_prefix = None
        for prefix in self.limits:
            if path.startswith(prefix):
                matched_prefix = prefix
                break

        if matched_prefix:
            max_requests, window = self.limits[matched_prefix]
            client_ip = self._get_client_ip(request)
            key = (matched_prefix, client_ip)
            now = time.monotonic()
            bucket = self.buckets[key]

            while bucket and now - bucket[0] > window:
                bucket.popleft()

            if len(bucket) >= max_requests:
                logger.warning(f"Rate limit exceeded for {client_ip} on {matched_prefix}")
                return JSONResponse(
                    {"detail": "Too many requests. Please slow down and try again later."},
                    status_code=429
                )

            bucket.append(now)

        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """
    Applies defense-in-depth HTTP security headers to all responses:
    - X-Content-Type-Options: nosniff (prevents MIME-sniffing)
    - X-Frame-Options: SAMEORIGIN (clickjacking protection)
    - X-XSS-Protection: 1; mode=block (XSS audit filter)
    - Referrer-Policy: strict-origin-when-cross-origin (protects path data)
    - Permissions-Policy: camera=(), microphone=(), geolocation=() (restricts unused device APIs)
    - Strict-Transport-Security: enforces TLS encryption on HTTPS
    """

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), browsing-topics=()"

        if request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains; preload"

        if "server" in response.headers:
            response.headers["server"] = "web-service"

        return response
