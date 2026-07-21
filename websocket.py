"""
websocket.py — pump.fun WebSocket listener
Подписка на subscribeNewToken, автореконнект, кладёт события в new_token_queue.
"""
import asyncio
import json
import logging
import re
import time
from collections import defaultdict

import aiohttp

import config
from database import is_seen, mark_pending

log = logging.getLogger("WS")

# Дедупликация клонов: PEPE / PEPE2 / PEPE_v2 → один базовый символ
# Не более 10 токенов с одним именем за скользящий час (было 3 — слишком мало)
_symbol_seen: dict[str, list[float]] = defaultdict(list)
_CLONE_WINDOW = 3600
_CLONE_MAX    = 10


def _base_symbol(symbol: str) -> str:
    s = symbol.upper().strip()
    s = re.sub(r"[^A-Z]", "", s)
    return s[:10]


def _is_clone_spam(symbol: str) -> bool:
    base = _base_symbol(symbol)
    if not base:
        return False
    now    = time.time()
    cutoff = now - _CLONE_WINDOW
    _symbol_seen[base] = [t for t in _symbol_seen[base] if t > cutoff]
    if len(_symbol_seen[base]) >= _CLONE_MAX:
        return True
    _symbol_seen[base].append(now)
    return False


async def pump_ws_listener(
    new_token_queue: asyncio.Queue,
    stats: dict,
) -> None:
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
                            await _handle_message(msg.data, new_token_queue, stats)
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

    # ── ПРИНИМАЕМ ЛЮБОЕ СООБЩЕНИЕ С ПОЛЕМ mint ────────────────────────────────
    # Раньше фильтровали по txType — pump.fun иногда меняет формат,
    # и строгая проверка отбрасывала все новые токены. Теперь главный
    # критерий: есть поле mint (адрес токена) — значит это токен.
    mint = data.get("mint") or data.get("ca") or ""
    if not mint:
        return

    # Пропускаем торговые события (buy/sell) — нам нужны только создания
    tx_type = data.get("txType") or data.get("type") or ""
    if tx_type in ("buy", "sell"):
        return

    # Если уже видели этот токен — пропускаем
    if await is_seen(mint):
        return

    symbol = data.get("symbol") or data.get("ticker") or "???"
    name   = data.get("name") or symbol

    # Символ из одного символа или только цифры — очевидный мусор
    if len(symbol.strip()) < 2 or symbol.strip().isdigit():
        return

    # Фильтр клонов
    if _is_clone_spam(symbol):
        log.debug(f"CLONE SKIP: {symbol}")
        return

    # УБРАЛИ фильтр по solAmount == 0:
    # pump.fun часто присылает solAmount=0 в create-событии даже для
    # нормальных токенов (creator купил отдельной транзакцией). Этот
    # фильтр блокировал ~90% реальных токенов.

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
        log.debug(f"WS queued: {symbol} ({mint[:8]}...)")
    except asyncio.QueueFull:
        log.warning(f"new_token_queue переполнена — {symbol} пропущен")
