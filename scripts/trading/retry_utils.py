"""Generic retry utilities for networking or flaky processes."""

import logging
import time
from functools import wraps
from typing import Any, Callable, Optional, TypeVar

import requests

logger = logging.getLogger(__name__)

T = TypeVar("T")


def with_retry(max_retries: int = 3, delay: float = 1.0) -> Callable:
    def decorator(func: Callable[..., Optional[T]]) -> Callable[..., Optional[T]]:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Optional[T]:
            last_exc = None
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except requests.RequestException as exc:
                    last_exc = exc
                    logger.warning(
                        "API error (attempt %d/%d): %s",
                        attempt + 1,
                        max_retries,
                        exc,
                    )
                    if attempt < max_retries - 1:
                        time.sleep(delay)
            logger.error("API failed after %d retries: %s", max_retries, last_exc)
            return None

        return wrapper

    return decorator
