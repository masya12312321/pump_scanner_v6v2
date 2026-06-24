"""
cache_manager.py — Smart TTL Cache
Reduces API calls via in-memory async cache.
TTLs: RugCheck=10m, LargestAccounts=5m, CreatorScore=30m, Dexscreener=30s
"""
import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("Cache")

# TTL in seconds
TTL = {
    "rugcheck":        600,   # 10 min
    "largest_accounts": 300,  # 5 min
    "creator_score":   1800,  # 30 min
    "dexscreener":      30,   # 30 sec
    "mint_info":        300,  # 5 min
}


@dataclass
class CacheEntry:
    value:      Any
    expires_at: float


class CacheManager:
    def __init__(self) -> None:
        self._store: dict[str, CacheEntry] = {}
        self._lock = asyncio.Lock()
        self._hits   = 0
        self._misses = 0

    def _make_key(self, namespace: str, key: str) -> str:
        return f"{namespace}:{key}"

    async def get(self, namespace: str, key: str) -> Any | None:
        full_key = self._make_key(namespace, key)
        async with self._lock:
            entry = self._store.get(full_key)
            if entry is None:
                self._misses += 1
                log.debug(f"CACHE MISS  {full_key}")
                return None
            if time.monotonic() > entry.expires_at:
                del self._store[full_key]
                self._misses += 1
                log.debug(f"CACHE EXPIRED {full_key}")
                return None
            self._hits += 1
            log.debug(f"CACHE HIT   {full_key}")
            return entry.value

    async def set(self, namespace: str, key: str, value: Any) -> None:
        full_key = self._make_key(namespace, key)
        ttl = TTL.get(namespace, 60)
        async with self._lock:
            self._store[full_key] = CacheEntry(
                value      = value,
                expires_at = time.monotonic() + ttl,
            )

    async def delete(self, namespace: str, key: str) -> None:
        full_key = self._make_key(namespace, key)
        async with self._lock:
            self._store.pop(full_key, None)

    async def cleanup(self) -> int:
        now = time.monotonic()
        async with self._lock:
            before = len(self._store)
            self._store = {
                k: v for k, v in self._store.items()
                if v.expires_at > now
            }
            removed = before - len(self._store)
        if removed:
            log.debug(f"CACHE CLEANUP: removed {removed} expired entries")
        return removed

    @property
    def stats(self) -> dict:
        total = self._hits + self._misses
        rate  = self._hits / total * 100 if total else 0
        return {
            "hits":     self._hits,
            "misses":   self._misses,
            "hit_rate": round(rate, 1),
            "size":     len(self._store),
        }


# singleton
_cache: CacheManager | None = None


def get_cache() -> CacheManager:
    global _cache
    if _cache is None:
        _cache = CacheManager()
    return _cache


async def cache_cleanup_loop() -> None:
    """Background task — cleans expired entries every 60s."""
    while True:
        try:
            await asyncio.sleep(60)
            await get_cache().cleanup()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log.error(f"Cache cleanup error: {exc}")
