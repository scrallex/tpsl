#!/usr/bin/env python3
"""State manager handling kill switches and transient Valkey states for TradingService."""

import logging
import os
from typing import Optional

try:
    import redis  # type: ignore
except ImportError:
    redis = None

logger = logging.getLogger(__name__)


class StateManager:
    """Manages Redis/Valkey state such as global kill switch configurations."""

    def __init__(self, redis_url: Optional[str] = None):
        self.redis_url = redis_url or os.getenv("VALKEY_URL") or os.getenv("REDIS_URL")
        self.kill_switch_key = os.getenv("KILL_SWITCH_KEY", "ops:kill_switch")
        self._valkey_client = self._connect_valkey(self.redis_url)
        self._kill_switch_enabled = self._load_kill_switch_state(default=True)

    @property
    def kill_switch_enabled(self) -> bool:
        return self._kill_switch_enabled

    def _connect_valkey(self, redis_url: Optional[str]):
        if not redis or not redis_url:
            return None
        try:
            return redis.from_url(redis_url)
        except (redis.ConnectionError, redis.TimeoutError) if redis else Exception:
            logger.warning("Unable to connect to Valkey at %s", redis_url)
            return None

    def _load_kill_switch_state(self, *, default: bool) -> bool:
        if not self._valkey_client or not self.kill_switch_key:
            return default
        try:
            raw = self._valkey_client.get(self.kill_switch_key)
        except (redis.RedisError, ConnectionError) if redis else Exception:
            logger.warning("Failed to read kill switch key %s", self.kill_switch_key)
            return default
        if raw is None:
            return default
        value = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
        return value.strip() not in {"0", "false", "False"}

    def set_kill_switch(self, enabled: bool) -> bool:
        flag = bool(enabled)
        self._kill_switch_enabled = flag
        if not self._valkey_client or not self.kill_switch_key:
            return flag
        try:
            self._valkey_client.set(self.kill_switch_key, "1" if flag else "0")
        except (redis.RedisError, ConnectionError) if redis else Exception:
            logger.warning(
                "Failed to persist kill switch state to %s", self.kill_switch_key
            )
        return flag
