import json
import time
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Tuple


_CACHE_TTL_SECONDS = float(5.0)
_CACHE: Dict[str, Tuple[float, float, Dict[str, Any]]] = {}
_CACHE_LOCK = Lock()


def read_cached_json(
    obj: Any,
    path: Path,
    cache_attr: str,
    mtime_attr: str,
    error_prefix: str,
) -> Dict[str, Any]:
    """
    Reads a JSON file and caches it on the given object.
    Only re-reads from disk if the file modification time has changed.
    """
    if not path.exists():
        return {"error": f"{error_prefix}_missing", "path": str(path)}
    try:
        stat = path.stat()
        mtime = stat.st_mtime
    except Exception as exc:
        return {"error": f"{error_prefix}_unreadable", "detail": str(exc)}

    cache = getattr(obj, cache_attr, None)
    cached_mtime = getattr(obj, mtime_attr, None)

    key = str(path)
    now = time.time()
    with _CACHE_LOCK:
        shared = _CACHE.get(key)
        if shared is not None:
            shared_mtime, shared_ts, shared_data = shared
            if shared_mtime == mtime and (now - shared_ts) <= _CACHE_TTL_SECONDS:
                setattr(obj, cache_attr, shared_data)
                setattr(obj, mtime_attr, mtime)
                return shared_data

    if cache is not None and cached_mtime == mtime:
        return cache

    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception:
        try:
            # brief sleep and retry on race conditions when writer is active
            time.sleep(0.05)
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except Exception as retry_exc:
            return {
                "error": f"{error_prefix}_invalid",
                "detail": str(retry_exc),
            }

    with _CACHE_LOCK:
        _CACHE[key] = (mtime, now, data)

    setattr(obj, cache_attr, data)
    setattr(obj, mtime_attr, mtime)
    return data
