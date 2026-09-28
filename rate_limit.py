import time
import logging
from collections import defaultdict, deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

logger = logging.getLogger("uvicorn.error")


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Simple in-memory sliding-window rate limiter, applied per client IP to a
    configured set of path prefixes. Not distributed — fine for a single-process
    deployment; a multi-worker deployment would need a shared store (e.g. Redis)
    for this to hold across processes.
    """

    def __init__(self, app, limits: dict):
        # limits: {"/path/prefix": (max_requests, window_seconds), ...}
        super().__init__(app)
        self.limits = limits
        self.buckets = defaultdict(deque)

    async def dispatch(self, request, call_next):
        path = request.url.path
        matched_prefix = None
        for prefix in self.limits:
            if path.startswith(prefix):
                matched_prefix = prefix
                break

        if matched_prefix:
            max_requests, window = self.limits[matched_prefix]
            client_ip = request.client.host if request.client else "unknown"
            key = (matched_prefix, client_ip)
            now = time.monotonic()
            bucket = self.buckets[key]

            while bucket and now - bucket[0] > window:
                bucket.popleft()

            if len(bucket) >= max_requests:
                logger.warning(f"Rate limit exceeded for {client_ip} on {matched_prefix}")
                return JSONResponse({"detail": "Too many requests. Please slow down."}, status_code=429)

            bucket.append(now)

        return await call_next(request)
