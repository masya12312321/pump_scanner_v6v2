"""
trading_engine.py — модуль автоторговли.

Режимы:
  paper (по умолчанию) — полная симуляция, реальные деньги не тратятся,
      сделки только пишутся в БД для оценки стратегии.
  real  — реальные сделки через wallet_manager.Wallet (PumpPortal Local
      Transaction API + подпись локально + отправка через Helius RPC).

Логика:
  1. maybe_open_position(sig) вызывается из telegram_bot.alert_worker сразу
     после отправки алерта в Telegram. Если автоторговля включена и сигнал
     прошёл по confidence — открывается позиция на фиксированную сумму SOL.
  2. monitor_loop() работает фоново и каждые TRADE_MONITOR_INTERVAL_SEC
     проверяет цену по всем открытым позициям; при достижении TP или SL —
     закрывает позицию (продажа) и шлёт уведомление в Telegram.

Защитные ограничения (чтобы бот не мог "улететь" без контроля):
  - max_positions          — лимит на одновременно открытые позиции
  - daily_loss_limit_sol   — суточный kill-switch: при превышении убытка
                              новые позиции не открываются до следующего дня
  - min_confidence_trade   — отдельный, обычно более строгий порог, чем
                              порог самого алерта (MIN_CONFIDENCE_SCORE)
"""
import asyncio
import logging
import time

import aiohttp

import config
from analysis import get_dex_data
from database import (
    get_trading_settings,
    count_open_positions,
    get_open_position,
    get_open_positions,
    open_position as db_open_position,
    update_position_peak,
    close_position as db_close_position,
    get_daily_pnl_sol,
)

log = logging.getLogger("TradingEngine")


class TradingEngine:
    def __init__(self, bot) -> None:
        self._bot = bot
        self._wallet = None
        self._lock = asyncio.Lock()

    # ── WALLET (lazy, только для real-режима) ─────────────────────────────────
    async def _get_wallet(self):
        if self._wallet is None:
            from wallet_manager import Wallet
            self._wallet = Wallet(config.WALLET_PRIVATE_KEY)
        return self._wallet

    # ── ОТКРЫТИЕ ПОЗИЦИИ ────────────────────────────────────────────────────────
    async def get_wallet_info(self) -> dict:
        """Используется командой /wallet."""
        settings = await get_trading_settings()
        if settings["paper_mode"]:
            return {"mode": "paper", "balance_sol": settings["paper_balance_sol"]}
        wallet = await self._get_wallet()
        if not wallet or not wallet.ready:
            return {"mode": "real", "ready": False}
        balance = await wallet.get_balance_sol()
        return {
            "mode": "real", "ready": True,
            "pubkey": wallet.pubkey, "balance_sol": balance,
        }

    async def maybe_open_position(self, sig) -> None:
        """Вызывается после каждого отправленного алерта."""
        try:
            settings = await get_trading_settings()
            if not settings["autotrade_enabled"]:
                return
            if sig.confidence < settings["min_confidence_trade"]:
                return

            async with self._lock:
                if await get_open_position(sig.ca) is not None:
                    return
                if await count_open_positions() >= settings["max_positions"]:
                    log.info(f"Лимит позиций достигнут — пропуск {sig.symbol}")
                    return

                since = int(time.time()) - 86400
                daily_pnl = await get_daily_pnl_sol(since)
                if daily_pnl <= -abs(settings["daily_loss_limit_sol"]):
                    log.warning(
                        f"Дневной лимит убытка ({settings['daily_loss_limit_sol']} SOL) "
                        f"достигнут — новые позиции не открываются"
                    )
                    return

                sol_amount = settings["position_size_sol"]
                mode = "paper" if settings["paper_mode"] else "real"
                buy_tx = None

                if mode == "paper":
                    entry_price = sig.price
                    if entry_price <= 0:
                        return
                    token_amount = sol_amount / entry_price
                else:
                    wallet = await self._get_wallet()
                    if not wallet or not wallet.ready:
                        await self._notify(
                            "⚠️ Реальный режим включён, но кошелёк не настроен "
                            "(WALLET_PRIVATE_KEY). Сделка по "
                            f"{sig.symbol} пропущена."
                        )
                        return
                    result = await wallet.buy(
                        sig.ca, sol_amount,
                        slippage=config.DEFAULT_SLIPPAGE_PCT,
                        priority_fee=config.DEFAULT_PRIORITY_FEE_SOL,
                    )
                    if not result.get("ok"):
                        await self._notify(
                            f"❌ Покупка не удалась: <b>{sig.symbol}</b>\n"
                            f"Причина: {result.get('error')}"
                        )
                        return
                    buy_tx = result.get("signature")
                    # На цепочке транзакция могла исполниться по другой цене —
                    # короткая пауза и реальный прайс с Dexscreener для оценки входа.
                    await asyncio.sleep(3)
                    async with aiohttp.ClientSession() as session:
                        dex = await get_dex_data(session, sig.ca)
                    entry_price = dex.get("price") or sig.price
                    if entry_price <= 0:
                        entry_price = sig.price
                    token_amount = sol_amount / entry_price if entry_price > 0 else 0

                tp_price = entry_price * (1 + settings["take_profit_pct"] / 100)
                sl_price = entry_price * (1 - settings["stop_loss_pct"] / 100)

                await db_open_position(
                    ca=sig.ca, symbol=sig.symbol, mode=mode,
                    sol_amount=sol_amount, token_amount=token_amount,
                    entry_price=entry_price, entry_mcap=sig.mcap,
                    tp_price=tp_price, sl_price=sl_price, buy_tx=buy_tx,
                )

            tag = "📝 PAPER" if mode == "paper" else "💸 РЕАЛЬНАЯ"
            await self._notify(
                f"{tag} ПОКУПКА: <b>{sig.symbol}</b>\n"
                f"💰 {sol_amount:g} SOL @ ${entry_price:.8f}\n"
                f"🎯 TP ${tp_price:.8f} (+{settings['take_profit_pct']:.0f}%) · "
                f"🛑 SL ${sl_price:.8f} (-{settings['stop_loss_pct']:.0f}%)\n"
                f"<code>{sig.ca}</code>"
            )
        except Exception as exc:
            log.error(f"maybe_open_position({getattr(sig, 'symbol', '?')}): {exc}")

    # ── МОНИТОРИНГ TP/SL ────────────────────────────────────────────────────────
    async def monitor_loop(self) -> None:
        log.info("Trading engine: мониторинг позиций запущен")
        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    await asyncio.sleep(config.TRADE_MONITOR_INTERVAL_SEC)
                    await self._check_positions(session)
                except asyncio.CancelledError:
                    return
                except Exception as exc:
                    log.error(f"monitor_loop: {exc}")

    async def _check_positions(self, session: aiohttp.ClientSession) -> None:
        positions = await get_open_positions()
        if not positions:
            return
        await asyncio.gather(
            *(self._check_one(session, p) for p in positions),
            return_exceptions=True,
        )

    async def _check_one(self, session: aiohttp.ClientSession, pos: dict) -> None:
        dex = await get_dex_data(session, pos["ca"])
        price = dex.get("price", 0.0)
        if price <= 0:
            return
        if price > pos.get("peak_price", 0):
            await update_position_peak(pos["ca"], price)

        reason = None
        if price >= pos["tp_price"]:
            reason = "take_profit"
        elif price <= pos["sl_price"]:
            reason = "stop_loss"
        if reason:
            await self._close_position(pos, price, reason)

    # ── ЗАКРЫТИЕ ПОЗИЦИИ ────────────────────────────────────────────────────────
    async def _close_position(self, pos: dict, price: float, reason: str) -> None:
        sell_tx = None
        entry_price = pos["entry_price"]

        if pos["mode"] == "real":
            wallet = await self._get_wallet()
            if not wallet or not wallet.ready:
                log.error(f"Нет кошелька для продажи {pos['symbol']} — повтор на след. тике")
                return
            result = await wallet.sell(
                pos["ca"], "100%",
                slippage=config.DEFAULT_SLIPPAGE_PCT,
                priority_fee=config.DEFAULT_PRIORITY_FEE_SOL,
            )
            if not result.get("ok"):
                log.error(f"Продажа не удалась {pos['symbol']}: {result.get('error')}")
                return  # попробуем снова на следующем тике мониторинга
            sell_tx = result.get("signature")

        pnl_pct = (price - entry_price) / entry_price * 100 if entry_price > 0 else 0
        pnl_sol = pos["sol_amount"] * (pnl_pct / 100)

        await db_close_position(
            ca=pos["ca"], exit_price=price, exit_reason=reason,
            pnl_sol=pnl_sol, pnl_pct=pnl_pct, sell_tx=sell_tx,
        )

        tag = "📝 PAPER" if pos["mode"] == "paper" else "💸 РЕАЛЬНАЯ"
        emoji = "🟢" if pnl_sol >= 0 else "🔴"
        reason_str = "🎯 Take Profit" if reason == "take_profit" else "🛑 Stop Loss"
        await self._notify(
            f"{tag} ПРОДАЖА: <b>{pos['symbol']}</b> — {reason_str}\n"
            f"{emoji} PnL: <b>{pnl_pct:+.1f}%</b> ({pnl_sol:+.4f} SOL)\n"
            f"<code>{pos['ca']}</code>"
        )

    # ── УВЕДОМЛЕНИЯ ──────────────────────────────────────────────────────────────
    async def _notify(self, text: str) -> None:
        try:
            await self._bot.send_message(
                chat_id=config.CHAT_ID, text=text,
                parse_mode="HTML", disable_web_page_preview=True,
            )
        except Exception as exc:
            log.error(f"Не удалось отправить уведомление о сделке: {exc}")
