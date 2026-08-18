import time
from collections import defaultdict
from threading import Lock

_WINDOW_SECONDS = 15 * 60
_MAX_ATTEMPTS = 5

_hits: dict[str, list[float]] = defaultdict(list)
_lock = Lock()


def _prune(key: str, now: float) -> list[float]:
    hits = [t for t in _hits[key] if now - t < _WINDOW_SECONDS]
    _hits[key] = hits
    return hits


def is_rate_limited(*keys: str) -> bool:
    now = time.time()
    with _lock:
        for key in keys:
            if len(_prune(key, now)) >= _MAX_ATTEMPTS:
                return True
    return False


def register_attempt(*keys: str) -> None:
    now = time.time()
    with _lock:
        for key in keys:
            _prune(key, now)
            _hits[key].append(now)


def reset_attempts(*keys: str) -> None:
    with _lock:
        for key in keys:
            _hits.pop(key, None)
