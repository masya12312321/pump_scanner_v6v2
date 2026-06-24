"""
websocket.py — pump.fun WebSocket listener
Подписка на subscribeNewToken, автореконнект, кладёт события в new_token_queue.
"""
import asyncio
import json
import logging
import time

import aiohttp

import config
from database import is_seen, mark_pending

log = logging.getLogger("WS")


async def pump_ws_listener(
    new_token_queue: asyncio.Queue,
    stats: dict,
) -> None:
    """
    Слушает pump.fun WS бесконечно.
    При разрыве переподключается через WS_RECONNECT_DELAY секунд.
    """
    subscribe_payload = json.dumps({"method": "subscribeNewToken"})

    while True:
        try:
            log.info("WS: подключаемся к pump.fun...")
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(
                    config.PUMP_WS_URL,
                    heartbeat=config.WS_HEARTBEAT,
                    timeout=aiohttp.ClientTimeout(total=None),
                ) as ws:
                    await ws.send_str(subscribe_payload)
                    log.info("WS: подписка активна")

                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await _handle_message(
                                msg.data, new_token_queue, stats
                            )
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            log.error(f"WS ошибка: {ws.exception()}")
                            break
                        elif msg.type == aiohttp.WSMsgType.CLOSED:
                            log.warning("WS: соединение закрыто сервером")
                            break

        except aiohttp.ClientError as exc:
            log.error(f"WS connection error: {exc}")
        except asyncio.CancelledError:
            log.info("WS listener отменён — завершение")
            return
        except Exception as exc:
            log.exception(f"WS неожиданная ошибка: {exc}")

        stats["ws_reconnects"] = stats.get("ws_reconnects", 0) + 1
        log.info(f"WS: переподключение через {config.WS_RECONNECT_DELAY} сек...")
        await asyncio.sleep(config.WS_RECONNECT_DELAY)


async def _handle_message(
    raw: str,
    queue: asyncio.Queue,
    stats: dict,
) -> None:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return

    tx_type = data.get("txType") or data.get("type") or ""
    mint     = data.get("mint") or data.get("ca") or ""

    if tx_type not in ("create", "newToken") and "mint" not in data:
        return

    if not mint:
        return

    # быстрый фильтр без DB — если уже видели в этой сессии пропускаем
    if await is_seen(mint):
        return

    symbol = data.get("symbol") or data.get("ticker") or "???"
    name   = data.get("name") or symbol

    token_event = {
        "ca":           mint,
        "symbol":       symbol,
        "name":         name,
        "creator":      data.get("traderPublicKey") or data.get("creator") or "",
        "timestamp_ms": data.get("timestamp") or int(time.time() * 1000),
        "sol_amount":   float(data.get("solAmount") or 0),
        "retry_count":  0,
    }

    await mark_pending(mint, symbol)
    stats["ws_received"] = stats.get("ws_received", 0) + 1

    try:
        queue.put_nowait(token_event)
    except asyncio.QueueFull:
        log.warning(f"new_token_queue переполнена — {symbol} пропущен")
