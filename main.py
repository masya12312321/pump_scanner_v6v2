"""
main.py v6 — wires all components together.
Fixes: retry_scheduler gets new_token_queue (not alert_queue).
New: outcome_tracker_loop, db_pool open/close, exit_engine gets initial_top1_pct.
"""
import asyncio
import logging
import time

from aiogram import Bot, Dispatcher

import config
import db_pool
from database import init_db, load_blacklist
from websocket import pump_ws_listener
from analysis import analyze_token, init_semaphores, _helius_sem, _dexscreener_sem
from retry_manager import retry_scheduler, enqueue_retry
from telegram_bot import alert_worker, register_commands
from cache_manager import cache_cleanup_loop, get_cache
from exit_signals import ExitSignalEngine
from self_learning import training_loop, load_weights, save_sample
from outcome_tracker import outcome_tracker_loop, init_outcome_tracker
from x_milestone_tracker import milestone_tracker_loop, init_milestone_tracker
from trading_engine import TradingEngine

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("Main")


async def analysis_worker(
    worker_id:       int,
    new_token_queue: asyncio.Queue,
    alert_queue:     asyncio.Queue,
    blacklist:       set[str],
    stats:           dict,
    exit_engine:     ExitSignalEngine,
) -> None:
    import aiohttp
    log.info(f"Analysis worker #{worker_id} started")
    async with aiohttp.ClientSession() as session:
        while True:
            token = None
            try:
                token = await new_token_queue.get()
                ca          = token["ca"]
                symbol      = token["symbol"]
                retry_count = token.get("retry_count", 0)

                result = await analyze_token(token, session, blacklist)

                if result is None:
                    await __import__("database").mark_rejected(ca)
                    stats["blocked_cooldown"] = stats.get("blocked_cooldown", 0) + 1
                    continue

                if result.needs_retry:
                    if retry_count < config.MAX_RETRIES:
                        await enqueue_retry(ca, symbol, retry_count)
                        stats["retry_sent"] = stats.get("retry_sent", 0) + 1
                    else:
                        await __import__("database").mark_rejected(ca)
                        stats["retry_exhausted"] = stats.get("retry_exhausted", 0) + 1
                    continue

                if result.is_honeypot:
                    blacklist.add(ca)
                    from database import add_blacklist, mark_rejected
                    await add_blacklist(ca, "honeypot")
                    await mark_rejected(ca)
                    stats["blocked_honeypot"] = stats.get("blocked_honeypot", 0) + 1
                    continue

                # ── ВТОРОЙ ПОЯС ЗАЩИТЫ: MCap потолок ──────────────────────────
                # analysis.py уже отсёк это раньше, но если MCap вырос за время
                # анализа (миграция на DEX заняла секунды) — режем тут же,
                # не доходя до алерта. Лучше перестраховаться дважды, чем
                # один раз пропустить устоявшийся токен типа USDC в сигналы.
                if result.mcap > config.MAX_SAFE_MCAP_USD:
                    from database import mark_rejected
                    await mark_rejected(ca)
                    stats["blocked_oversized"] = stats.get("blocked_oversized", 0) + 1
                    log.warning(
                        f"BLOCKED oversized token reached alert stage: "
                        f"{symbol} MCap=${result.mcap:,.0f}"
                    )
                    continue

                if result.confidence < config.MIN_CONFIDENCE_SCORE:
                    from database import mark_rejected
                    await mark_rejected(ca)
                    stats["blocked_confidence"] = stats.get("blocked_confidence", 0) + 1
                    continue

                # Жёсткий блок: слишком высокий rug_score — скам/honeypot
                if result.rug_score > 70:
                    from database import mark_rejected, add_blacklist
                    await mark_rejected(ca)
                    # добавляем в блэклист чтобы не анализировать повторно
                    await add_blacklist(ca, f"rug_score={result.rug_score}")
                    blacklist.add(ca)
                    stats["blocked_rug"] = stats.get("blocked_rug", 0) + 1
                    continue

                if result.liquidity < config.MIN_LIQUIDITY_USD:
                    from database import mark_rejected
                    await mark_rejected(ca)
                    stats["blocked_liquidity"] = stats.get("blocked_liquidity", 0) + 1
                    continue

                if result.age_min > config.MAX_AGE_MINUTES:
                    from database import mark_rejected
                    await mark_rejected(ca)
                    continue

                # save learning sample
                vol_ratio = result.volume_15m / result.mcap if result.mcap > 0 else 0
                await save_sample(
                    token         = ca,
                    confidence    = result.confidence,
                    creator_score = result.creator_score,
                    rug_score     = result.rug_score,
                    volume_ratio  = vol_ratio,
                    liquidity     = result.liquidity,
                    holders       = result.holders_count,
                )

                # register with exit engine — pass initial top1% for whale comparison
                await exit_engine.register(
                    ca, symbol,
                    token.get("creator", ""),
                    initial_top1_pct=result.top1_pct,
                )

                try:
                    alert_queue.put_nowait(result)
                except asyncio.QueueFull:
                    log.warning(f"alert_queue full — {symbol} dropped")

            except asyncio.CancelledError:
                if token is not None:
                    new_token_queue.task_done()
                log.info(f"Analysis worker #{worker_id} cancelled")
                return
            except Exception as exc:
                log.exception(
                    f"W#{worker_id} error on "
                    f"{token.get('symbol','???') if token else '???'}: {exc}"
                )
            finally:
                try:
                    new_token_queue.task_done()
                except ValueError:
                    pass


async def main() -> None:
    log.info("Pump Scanner v6 — starting")

    await init_db()                           # opens db_pool internally
    blacklist = await load_blacklist()
    log.info(f"Blacklist: {len(blacklist)} entries")

    await load_weights()
    init_semaphores()

    new_token_queue = asyncio.Queue(maxsize=config.NEW_TOKEN_QUEUE_SIZE)
    alert_queue     = asyncio.Queue(maxsize=config.ALERT_QUEUE_SIZE)

    stats: dict = {
        "start_time":         int(time.time()),
        "ws_received":        0,
        "ws_reconnects":      0,
        "alerts_sent":        0,
        "blocked_honeypot":   0,
        "blocked_confidence": 0,
        "blocked_cooldown":   0,
        "blocked_liquidity":  0,
        "retry_sent":         0,
        "retry_exhausted":    0,
    }

    queues = {"new": new_token_queue, "alert": alert_queue}
    bot    = Bot(token=config.BOT_TOKEN)
    dp     = Dispatcher()

    # init exit engine with correct semaphores
    from analysis import _helius_sem, _dexscreener_sem
    exit_engine = ExitSignalEngine(bot, _helius_sem)

    # init outcome tracker
    init_outcome_tracker(_dexscreener_sem)

    # init live X-multiplier milestone tracker
    init_milestone_tracker(bot, _dexscreener_sem)

    # init торгового движка (paper/real, TP/SL мониторинг)
    trading_engine = TradingEngine(bot)

    cache = get_cache()
    register_commands(dp, stats, queues, exit_engine, cache, trading_engine)

    async def bl_tracker() -> None:
        while True:
            stats["blacklist_size"] = len(blacklist)
            await asyncio.sleep(30)

    tasks = [
        asyncio.create_task(pump_ws_listener(new_token_queue, stats),  name="ws"),
        asyncio.create_task(retry_scheduler(new_token_queue, stats),   name="retry"),   # FIXED
        asyncio.create_task(exit_engine.run(),                          name="exit"),
        asyncio.create_task(cache_cleanup_loop(),                       name="cache"),
        asyncio.create_task(training_loop(),                            name="learning"),
        asyncio.create_task(outcome_tracker_loop(),                     name="outcomes"),
        asyncio.create_task(milestone_tracker_loop(),                   name="milestones"),
        asyncio.create_task(trading_engine.monitor_loop(),               name="trading"),
        asyncio.create_task(bl_tracker(),                               name="bl"),
        *[
            asyncio.create_task(
                analysis_worker(i+1, new_token_queue, alert_queue,
                                blacklist, stats, exit_engine),
                name=f"analysis_{i+1}"
            )
            for i in range(config.ANALYSIS_WORKER_COUNT)
        ],
        *[
            asyncio.create_task(
                alert_worker(bot, alert_queue, blacklist, stats, i+1, trading_engine),
                name=f"alert_{i+1}"
            )
            for i in range(config.ALERT_WORKER_COUNT)
        ],
        asyncio.create_task(dp.start_polling(bot, skip_updates=True), name="tg"),
    ]

    log.info(f"Started {len(tasks)} tasks")
    try:
        await asyncio.gather(*tasks)
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("Shutting down...")
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await db_pool.close_pool()
        log.info("Pump Scanner v6 stopped")


if __name__ == "__main__":
    asyncio.run(main())
