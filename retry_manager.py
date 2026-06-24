"""
retry_manager.py v6 — fixed: passes new_token_queue, not alert_queue.
"""
import asyncio
import logging
import time

import config
from database import get_due_retries, set_retry, mark_rejected

log = logging.getLogger("Retry")


async def retry_scheduler(
    new_token_queue: asyncio.Queue,   # ← FIXED: was alert_queue in v5
    stats:           dict,
) -> None:
    while True:
        try:
            await asyncio.sleep(10)
            now  = int(time.time())
            rows = await get_due_retries(now)
            for row in rows:
                ca          = row["ca"]
                symbol      = row["symbol"]
                retry_count = row["retry_count"]
                if retry_count >= config.MAX_RETRIES:
                    await mark_rejected(ca)
                    stats["retry_exhausted"] = stats.get("retry_exhausted", 0) + 1
                    continue
                delay    = config.RETRY_DELAYS[min(retry_count, len(config.RETRY_DELAYS)-1)]
                next_try = int(time.time()) + delay
                await set_retry(ca, retry_count + 1, next_try)
                event = {
                    "ca":           ca,
                    "symbol":       symbol,
                    "name":         symbol,
                    "creator":      "",
                    "timestamp_ms": int(time.time() * 1000),
                    "sol_amount":   0.0,
                    "retry_count":  retry_count + 1,
                }
                try:
                    new_token_queue.put_nowait(event)
                    stats["retry_sent"] = stats.get("retry_sent", 0) + 1
                    log.info(f"RETRY #{retry_count+1}: {symbol}")
                except asyncio.QueueFull:
                    log.warning(f"Queue full — retry {symbol} dropped")
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log.error(f"Retry scheduler: {exc}")


async def enqueue_retry(ca: str, symbol: str, retry_count: int) -> None:
    if retry_count >= config.MAX_RETRIES:
        await mark_rejected(ca)
        return
    delay    = config.RETRY_DELAYS[min(retry_count, len(config.RETRY_DELAYS)-1)]
    next_try = int(time.time()) + delay
    await set_retry(ca, retry_count + 1, next_try)
