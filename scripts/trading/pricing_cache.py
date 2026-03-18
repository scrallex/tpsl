import json
import logging
import time
from typing import Dict, List, Optional, Tuple

try:
    import redis  # type: ignore
except ImportError:  # pragma: no cover
    redis = None

logger = logging.getLogger(__name__)


class PriceHistoryCache:
    """Cache small pricing series snapshots in Valkey (or memory fallback)."""

    def __init__(
        self, redis_url: Optional[str], *, ttl_seconds: int = 300, max_points: int = 500
    ) -> None:
        self.ttl_seconds = max(30, int(ttl_seconds))
        self.max_points = max(10, int(max_points))
        self._client = None
        if redis and redis_url:
            try:
                self._client = redis.from_url(redis_url)
            except (redis.ConnectionError, redis.RedisError, ValueError, OSError) as exc:  # pragma: no cover
                logger.debug("Failed to connect to Redis at %s: %s", redis_url, exc)
                self._client = None
        self._memory: Dict[str, Tuple[float, List[Dict[str, object]]]] = {}

    def _key(self, instrument: str, granularity: str) -> str:
        return f"pricing:history:{granularity.upper()}:{instrument.upper()}"

    def get(
        self, instrument: str, granularity: str
    ) -> Tuple[List[Dict[str, object]], Optional[float]]:
        key = self._key(instrument, granularity)
        payload: Optional[Dict[str, object]] = None
        if self._client:
            try:
                raw = self._client.get(key)
            except (redis.ConnectionError, redis.TimeoutError, redis.RedisError) as exc:  # pragma: no cover
                logger.debug("Redis get failed for %s: %s", key, exc)
                raw = None
            if raw:
                try:
                    payload = json.loads(
                        raw if isinstance(raw, str) else raw.decode("utf-8")
                    )
                except (json.JSONDecodeError, UnicodeDecodeError, AttributeError) as exc:
                    logger.debug("Failed to decode Redis payload for %s: %s", key, exc)
                    payload = None
        if not payload:
            entry = self._memory.get(key)
            if entry:
                ts, points = entry
                return [dict(p) for p in points], ts
            return [], None
        points = payload.get("points") if isinstance(payload, dict) else None
        fetched_at = payload.get("fetched_at") if isinstance(payload, dict) else None
        if not isinstance(points, list):
            points = []
        ts_value: Optional[float]
        try:
            ts_value = float(fetched_at) if fetched_at is not None else None
        except (ValueError, TypeError):
            ts_value = None
        return [dict(p) for p in points], ts_value

    def set(
        self, instrument: str, granularity: str, points: List[Dict[str, object]]
    ) -> None:
        key = self._key(instrument, granularity)
        trimmed = [dict(p) for p in points][-self.max_points :]
        record = {
            "points": trimmed,
            "fetched_at": time.time(),
        }
        if self._client:
            try:
                self._client.set(key, json.dumps(record), ex=self.ttl_seconds)
            except (redis.ConnectionError, redis.TimeoutError, redis.RedisError) as exc:  # pragma: no cover
                logger.debug("Redis set failed for %s: %s", key, exc)
        self._memory[key] = (record["fetched_at"], trimmed)
