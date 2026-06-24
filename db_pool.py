"""
db_pool.py — Singleton SQLite connection pool via aiosqlite.
Replaces per-call `async with aiosqlite.connect()` pattern.
All writes go through a single serialised writer queue — eliminates
file-lock contention under 10+ concurrent workers.
"""
import asyncio
import logging

import aiosqlite

from config import DB_PATH

log = logging.getLogger("DBPool")

_conn:        aiosqlite.Connection | None = None
_write_lock:  asyncio.Lock               = asyncio.Lock()
_ready:       asyncio.Event              = asyncio.Event()


async def open_pool() -> None:
    global _conn
    _conn = await aiosqlite.connect(DB_PATH, timeout=30)
    _conn.row_factory = aiosqlite.Row
    await _conn.execute("PRAGMA journal_mode=WAL")
    await _conn.execute("PRAGMA synchronous=NORMAL")
    await _conn.execute("PRAGMA cache_size=-32000")   # 32 MB page cache
    await _conn.commit()
    _ready.set()
    log.info("DB pool open (WAL mode)")


async def close_pool() -> None:
    global _conn
    if _conn:
        await _conn.close()
        _conn = None
    log.info("DB pool closed")


def get_conn() -> aiosqlite.Connection:
    if _conn is None:
        raise RuntimeError("DB pool not initialised — call open_pool() first")
    return _conn


async def execute(sql: str, params: tuple = ()) -> None:
    """Single write — serialised through lock."""
    await _ready.wait()
    async with _write_lock:
        await _conn.execute(sql, params)
        await _conn.commit()


async def executemany(sql: str, params_list: list[tuple]) -> None:
    await _ready.wait()
    async with _write_lock:
        await _conn.executemany(sql, params_list)
        await _conn.commit()


async def executescript(script: str) -> None:
    await _ready.wait()
    async with _write_lock:
        await _conn.executescript(script)
        await _conn.commit()


async def fetchone(sql: str, params: tuple = ()) -> aiosqlite.Row | None:
    await _ready.wait()
    async with _conn.execute(sql, params) as cur:
        return await cur.fetchone()


async def fetchall(sql: str, params: tuple = ()) -> list[aiosqlite.Row]:
    await _ready.wait()
    async with _conn.execute(sql, params) as cur:
        return await cur.fetchall()
