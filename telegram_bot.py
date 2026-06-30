"""
telegram_bot.py v6
New command: /last — shows 5 recent alerts with PnL
"""
import asyncio
import io
import logging
import time

from aiogram import Bot, Dispatcher
from aiogram.types import Message, BufferedInputFile
from aiogram.filters import Command

import config
from x_calculator import calculate_x, format_x_block
from pnl_chart import build_pnl_chart, MATPLOTLIB_AVAILABLE
from database import (
    get_alert_count, get_seen_count,
    get_blacklist_entries, get_top_creators,
    save_alert, mark_alerted, add_blacklist,
    increment_creator_field, get_recent_alerts,
    get_trading_settings, update_trading_settings,
    get_open_positions, get_closed_positions,
    get_trading_stats, count_open_positions,
    get_daily_pnl_sol,
)

log = logging.getLogger("TelegramBot")


def confidence_bar(score: int) -> str:
    filled = round(score / 10)
    return f"[{'█'*filled}{'░'*(10-filled)}] {score}/100"


def fmt_usd(v: float) -> str:
    """Компактный формат денежных сумм: $4,200 / $74.8k / $102.3M / $1.2B."""
    v = float(v)
    if v >= 1_000_000_000:
        return f"${v/1_000_000_000:.1f}B"
    if v >= 1_000_000:
        return f"${v/1_000_000:.1f}M"
    if v >= 1_000:
        return f"${v/1_000:.1f}k"
    return f"${v:,.0f}"


def pct_arrow(v: float) -> str | None:
    """Возвращает None для значений около нуля — такие строки не выводим вовсе."""
    if abs(v) < 1:
        return None
    if v > 0: return f"▲{v:+.1f}%"
    return f"▼{v:+.1f}%"


def get_predicted_peak_mcap(sig) -> float:
    """
    Считает ожидаемый пик MCap отдельно от рендера сообщения —
    alert_worker использует это значение чтобы сохранить прогноз в БД
    и потом сверять с фактическим пиком (forecast_hit).
    """
    x_pred = calculate_x(
        mcap=sig.mcap, confidence=sig.confidence, rug_score=sig.rug_score,
        momentum=sig.momentum, age_min=sig.age_min, creator_score=sig.creator_score,
        liquidity=sig.liquidity, volume_3m=sig.volume_15m,
    )
    return x_pred.peak_mcap_est


def build_alert_message(sig) -> str:
    rug_emoji = "🔴" if sig.rug_score > 60 else "🟠" if sig.rug_score > 30 else "🟢"

    pump_url = f"https://pump.fun/{sig.ca}"
    dex_url  = sig.dex_url or f"https://dexscreener.com/solana/{sig.ca}"
    scan_url = f"https://solscan.io/token/{sig.ca}"
    rug_url  = f"https://rugcheck.xyz/tokens/{sig.ca}"

    sell_tax_str = f" · Sell tax: <b>{sig.sell_tax:.0f}%</b>" if sig.sell_tax > 0 else ""
    raydium_str  = " · 🟢 Raydium" if sig.on_raydium else ""
    pairs_str    = f" ({sig.pair_count} пар)" if sig.pair_count > 1 else ""

    # ATH и Объём(15м) — только если токену есть что показывать.
    # ATH — это цена за токен (доли цента), не MCap, поэтому форматируем отдельно.
    has_history = sig.age_min >= 3
    ath_line    = f"🏆 ATH цена: <b>${sig.ath:.8f}</b>\n" if has_history and sig.ath > 0 else ""
    volume_line = ""
    if has_history and sig.volume_15m > 0:
        vol_ratio_str = ""
        if sig.mcap > 0:
            vr = sig.volume_15m / sig.mcap * 100
            vol_ratio_str = f" ({vr:.0f}% от MCap)"
        volume_line = f"📈 Объём (15м): <b>{fmt_usd(sig.volume_15m)}</b>{vol_ratio_str}\n"

    # Momentum: если все три интервала около нуля — просто "накапливаем",
    # без строки из трёх нулей, которая ничего не говорит
    a1, a3, a5 = pct_arrow(sig.change_1m), pct_arrow(sig.change_3m), pct_arrow(sig.change_5m)
    parts = [p for p in [
        f"1м {a1}" if a1 else None,
        f"3м {a3}" if a3 else None,
        f"5м {a5}" if a5 else None,
    ] if p]
    momentum_line = (
        f"📉 Momentum: <b>{sig.momentum}</b> ({' · '.join(parts)})\n"
        if parts else
        f"📉 Momentum: <b>⏳ Накапливаем</b>\n"
    )

    x_pred  = calculate_x(
        mcap=sig.mcap, confidence=sig.confidence, rug_score=sig.rug_score,
        momentum=sig.momentum, age_min=sig.age_min, creator_score=sig.creator_score,
        liquidity=sig.liquidity, volume_3m=sig.volume_15m,
    )
    x_block = format_x_block(x_pred)

    return (
        # ── BRAND HEADER ──
        f"🧠 <b>DEXMIND SIGNAL</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🪙 <b>{sig.symbol}</b> — {sig.name}{raydium_str}\n\n"

        # ── 1. БЕЗОПАСНОСТЬ (можно ли это покупать) ──
        f"🛡 <b>БЕЗОПАСНОСТЬ</b>\n"
        f"{rug_emoji} Rug Score: <b>{sig.rug_score}/100</b>{sell_tax_str}\n"
        f"🍯 Honeypot: <b>{'🚫 ДА' if sig.is_honeypot else '✅ Нет'}</b>\n"
        f"🧑‍💻 Creator Score: <b>{sig.creator_score}/100</b>\n\n"

        # ── 2. РЫНОК (текущая ситуация) ──
        f"📍 <b>РЫНОК</b>\n"
        f"💰 MCap: <b>{fmt_usd(sig.mcap)}</b>{pairs_str}\n"
        f"{ath_line}"
        f"{volume_line}"
        f"💧 Ликвидность: <b>{fmt_usd(sig.liquidity)}</b>\n"
        f"⏳ Возраст: <b>{sig.age_min:.1f} мин</b>\n"
        f"{momentum_line}"
        f"🐋 Топ-1: <b>{sig.top1_pct:.1f}%</b> · Топ-5: <b>{sig.top5_pct:.1f}%</b> · "
        f"Топ-10: <b>{sig.top10_pct:.1f}%</b> · 👥 {sig.holders_count} холдеров\n\n"

        # ── 3. ПРОГНОЗ И СТРАТЕГИЯ ──
        f"{x_block}\n\n"

        f"📋 <code>{sig.ca}</code>\n\n"
        f"<a href='{pump_url}'>Pump.fun</a> | <a href='{dex_url}'>Dex</a> | "
        f"<a href='{scan_url}'>Solscan</a> | <a href='{rug_url}'>RugCheck</a>"
    )


async def alert_worker(
    bot: Bot, alert_queue: asyncio.Queue,
    blacklist: set[str], stats: dict, worker_id: int,
    trading_engine=None,
) -> None:
    log.info(f"Alert worker #{worker_id} started")
    while True:
        sig = None
        try:
            sig = await alert_queue.get()
            if sig.is_honeypot:
                blacklist.add(sig.ca)
                await add_blacklist(sig.ca, "honeypot")
                continue
            msg = build_alert_message(sig)
            await bot.send_message(
                chat_id=config.CHAT_ID, text=msg,
                parse_mode="HTML", disable_web_page_preview=True,
            )
            await mark_alerted(sig.ca, mcap=sig.mcap, creator=sig.creator)

            # ФИКС: predicted_peak_mcap не передавался — forecast_hit никогда
            # не мог сработать, потому что в БД всегда лежал 0.0
            predicted_peak = get_predicted_peak_mcap(sig)
            await save_alert(
                sig.ca, sig.symbol, sig.confidence,
                sig.rug_score, sig.mcap, sig.creator,
                predicted_peak_mcap=predicted_peak,
            )
            if sig.creator:
                await increment_creator_field(sig.creator, "tokens_alerted")

            # ── АВТОТОРГОВЛЯ ──
            # Не блокируем alert_worker на время покупки (особенно в real-режиме,
            # где есть сетевые вызовы) — запускаем как отдельную задачу.
            if trading_engine is not None:
                asyncio.create_task(trading_engine.maybe_open_position(sig))

            stats["alerts_sent"] = stats.get("alerts_sent", 0) + 1
            log.info(f"ALERT #{stats['alerts_sent']}: {sig.symbol} conf={sig.confidence}")
        except asyncio.CancelledError:
            if sig is not None:
                alert_queue.task_done()
            return
        except Exception as exc:
            log.exception(f"Alert worker #{worker_id}: {exc}")
        finally:
            try:
                alert_queue.task_done()
            except ValueError:
                pass
        await asyncio.sleep(0.3)


def _pnl_emoji(pnl: float) -> str:
    if pnl >= 100:  return "🚀"
    if pnl >= 50:   return "🟢"
    if pnl >= 0:    return "🟡"
    if pnl >= -50:  return "🟠"
    return "🔴"


def _build_last_text(alerts: list[dict]) -> str:
    lines = []
    for a in alerts:
        ca      = a["ca"]
        symbol  = a["symbol"]
        mcap    = a.get("mcap", 0)
        outcome = a.get("outcome")
        out_mcp = a.get("outcome_mcap")
        sent_at = a.get("sent_at", 0)
        age_min = int((time.time() - sent_at) / 60)

        if outcome and out_mcp and mcap > 0:
            pnl = (out_mcp - mcap) / mcap * 100
            pnl_str = f"{_pnl_emoji(pnl)} {pnl:+.0f}% ({outcome.upper()})"
        elif outcome:
            pnl_str = f"📋 {outcome.upper()}"
        else:
            pnl_str = "⏳ Ещё живёт"

        dex_url = f"https://dexscreener.com/solana/{ca}"
        lines.append(
            f"🪙 <a href='{dex_url}'>{symbol}</a> | "
            f"${mcap:,.0f} | {age_min}м назад\n"
            f"   {pnl_str}"
        )
    return "📋 <b>Последние 5 алертов</b>\n\n" + "\n\n".join(lines)


def _build_last_caption(alerts: list[dict]) -> str:
    """Short caption under the PnL chart image."""
    wins  = 0
    total_labeled = 0
    for a in alerts:
        outcome = a.get("outcome")
        if outcome:
            total_labeled += 1
            if outcome in ("x2", "x5", "x10"):
                wins += 1
    summary = f"✅ {wins}/{total_labeled} в плюсе" if total_labeled else "⏳ Результаты ещё не готовы"
    return f"📋 <b>Последние 5 алертов</b>\n{summary}"


def register_commands(
    dp: Dispatcher, stats: dict, queues: dict, exit_engine=None, cache=None,
    trading_engine=None,
) -> None:

    _pending_paper_off = {"ts": 0.0}

    @dp.message(Command("start"))
    async def cmd_start(m: Message) -> None:
        await m.answer(
            "🧠 <b>DEXMIND</b>\n\n"
            "📡 pump.fun WebSocket\n"
            "🔗 Helius RPC + RugCheck + Dexscreener (cached)\n"
            "📊 X-Multiplier + Exit Timing\n"
            "⚠️ Exit Signals (whale snapshot fix)\n"
            "🧠 Self-Learning weights → X-Calculator\n"
            "💾 Smart Cache (TTL per source)\n"
            "🏆 Creator Reputation (outcome tracking)\n"
            "🔌 DB Pool (WAL, no lock contention)\n"
            "🔍 Bundle detection (% threshold)\n"
            "🤖 Автоторговля (paper/real, TP/SL)\n\n"
            "/stats /queue /filters /blacklist\n"
            "/topcreators /retry /health /cache /weights /last\n\n"
            "🤖 <b>Торговля:</b> /trading /autotrade /paper /amount "
            "/tp /sl /maxpos /minconf /dailylimit /positions /pnl /wallet",
            parse_mode="HTML",
        )

    @dp.message(Command("stats"))
    async def cmd_stats(m: Message) -> None:
        seen    = await get_seen_count()
        alerted = await get_alert_count()
        await m.answer(
            f"📊 <b>Статистика v6</b>\n\n"
            f"📥 WS: <b>{stats.get('ws_received',0)}</b>\n"
            f"🔍 Просмотрено: <b>{seen}</b>\n"
            f"🚨 Алертов: <b>{alerted}</b>\n"
            f"🍯 Honeypot: <b>{stats.get('blocked_honeypot',0)}</b>\n"
            f"📉 Low conf: <b>{stats.get('blocked_confidence',0)}</b>\n"
            f"🔄 Retry: <b>{stats.get('retry_sent',0)}</b>\n"
            f"📡 WS реконнект: <b>{stats.get('ws_reconnects',0)}</b>",
            parse_mode="HTML",
        )

    @dp.message(Command("queue"))
    async def cmd_queue(m: Message) -> None:
        await m.answer(
            f"📦 <b>Очереди</b>\n\n"
            f"new_token: <b>{queues['new'].qsize()}</b> / {config.NEW_TOKEN_QUEUE_SIZE}\n"
            f"alert: <b>{queues['alert'].qsize()}</b> / {config.ALERT_QUEUE_SIZE}\n"
            f"Воркеров: <b>{config.ANALYSIS_WORKER_COUNT}</b>",
            parse_mode="HTML",
        )

    @dp.message(Command("filters"))
    async def cmd_filters(m: Message) -> None:
        await m.answer(
            f"⚙️ <b>Фильтры</b>\n\n"
            f"⏳ Макс возраст: <b>{config.MAX_AGE_MINUTES} мин</b>\n"
            f"💧 Мин ликвидность: <b>${config.MIN_LIQUIDITY_USD:,.0f}</b>\n"
            f"🎯 Мин Confidence: <b>{config.MIN_CONFIDENCE_SCORE}/100</b>\n"
            f"📉 Cooldown: <b>{int(config.AUTO_COOLDOWN_DROP*100)}% от ATH</b>",
            parse_mode="HTML",
        )

    @dp.message(Command("blacklist"))
    async def cmd_blacklist(m: Message) -> None:
        entries = await get_blacklist_entries()
        if not entries:
            await m.answer("🚫 Блэклист пуст")
            return
        lines = "\n".join(
            f"<code>{e['ca'][:16]}...</code> — {e['reason']}"
            for e in entries[:15]
        )
        await m.answer(f"🚫 <b>Блэклист ({len(entries)})</b>\n\n{lines}", parse_mode="HTML")

    @dp.message(Command("topcreators"))
    async def cmd_topcreators(m: Message) -> None:
        creators = await get_top_creators(10)
        if not creators:
            await m.answer("📭 Пусто")
            return
        lines = [
            f"<code>{c['creator'][:12]}...</code> | "
            f"✅{c['tokens_alerted']} 💀{c['tokens_rugged']} x10:{c['tokens_x10']} | "
            f"Score:{c['score']}"
            for c in creators
        ]
        await m.answer("🏆 <b>Топ создатели</b>\n\n" + "\n".join(lines), parse_mode="HTML")

    @dp.message(Command("retry"))
    async def cmd_retry(m: Message) -> None:
        await m.answer(
            f"🔄 <b>Retry</b>\n\n"
            f"Отправлено: <b>{stats.get('retry_sent',0)}</b>\n"
            f"Exhaust: <b>{stats.get('retry_exhausted',0)}</b>\n"
            f"Delays: <b>{config.RETRY_DELAYS} сек</b>",
            parse_mode="HTML",
        )

    @dp.message(Command("health"))
    async def cmd_health(m: Message) -> None:
        uptime = int(time.time()) - stats.get("start_time", int(time.time()))
        h, mi  = uptime // 3600, (uptime % 3600) // 60
        await m.answer(
            f"💚 <b>Health</b>\n\n"
            f"⏱ Uptime: <b>{h}ч {mi}м</b>\n"
            f"📡 WS реконнект: <b>{stats.get('ws_reconnects',0)}</b>\n"
            f"📦 new_queue: <b>{queues['new'].qsize()}</b>\n"
            f"📤 alert_queue: <b>{queues['alert'].qsize()}</b>",
            parse_mode="HTML",
        )

    @dp.message(Command("cache"))
    async def cmd_cache(m: Message) -> None:
        if cache is None:
            await m.answer("Cache не инициализирован")
            return
        s = cache.stats
        await m.answer(
            f"💾 <b>Cache</b>\n\n"
            f"✅ Hits: <b>{s['hits']}</b>\n"
            f"❌ Misses: <b>{s['misses']}</b>\n"
            f"📈 Hit rate: <b>{s['hit_rate']}%</b>\n"
            f"📦 Entries: <b>{s['size']}</b>",
            parse_mode="HTML",
        )

    @dp.message(Command("weights"))
    async def cmd_weights(m: Message) -> None:
        from self_learning import get_dynamic_weights
        w     = get_dynamic_weights()
        lines = "\n".join(
            f"  {k}: <b>{v:.3f}</b>"
            for k, v in sorted(w.items(), key=lambda x: -x[1])
        )
        await m.answer(
            f"🧠 <b>Self-Learning Weights</b>\n\n{lines}\n\n"
            f"<i>Обновляются раз в 24ч</i>",
            parse_mode="HTML",
        )

    @dp.message(Command("last"))
    async def cmd_last(m: Message) -> None:
        alerts = await get_recent_alerts(5)
        if not alerts:
            await m.answer("📭 Алертов ещё нет")
            return

        # ── try chart first ──
        if MATPLOTLIB_AVAILABLE:
            try:
                png_bytes = build_pnl_chart(alerts)
            except Exception as exc:
                log.error(f"PnL chart render failed: {exc}")
                png_bytes = None
            if png_bytes:
                caption = _build_last_caption(alerts)
                photo = BufferedInputFile(png_bytes, filename="pnl.png")
                await m.answer_photo(photo=photo, caption=caption, parse_mode="HTML")
                return

        # ── text fallback ──
        await m.answer(_build_last_text(alerts), parse_mode="HTML", disable_web_page_preview=True)

    # ═══════════════════════════ АВТОТОРГОВЛЯ ═══════════════════════════════════

    @dp.message(Command("trading"))
    async def cmd_trading(m: Message) -> None:
        s = await get_trading_settings()
        open_n = await count_open_positions()
        st = await get_trading_stats()
        daily_pnl = await get_daily_pnl_sol(int(time.time()) - 86400)
        mode_str = "📝 PAPER (симуляция)" if s["paper_mode"] else "💸 РЕАЛЬНЫЙ"
        auto_str = "✅ ВКЛ" if s["autotrade_enabled"] else "⛔ ВЫКЛ"
        await m.answer(
            f"🤖 <b>Автоторговля</b>\n\n"
            f"Режим: <b>{mode_str}</b>\n"
            f"Статус: <b>{auto_str}</b>\n"
            f"💰 Сумма/сделку: <b>{s['position_size_sol']:g} SOL</b>\n"
            f"🎯 Take Profit: <b>+{s['take_profit_pct']:.0f}%</b>\n"
            f"🛑 Stop Loss: <b>-{s['stop_loss_pct']:.0f}%</b>\n"
            f"📦 Позиций открыто: <b>{open_n}/{s['max_positions']}</b>\n"
            f"🎚 Мин. confidence: <b>{s['min_confidence_trade']}/100</b>\n"
            f"🛑 Дневной лимит убытка: <b>{s['daily_loss_limit_sol']:g} SOL</b> "
            f"(за 24ч: <b>{daily_pnl:+.4f}</b>)\n\n"
            f"📊 Сделок всего: <b>{st['trades_count']}</b> · "
            f"Винрейт: <b>{st['win_rate']:.0f}%</b> · "
            f"Total PnL: <b>{st['total_pnl_sol']:+.4f} SOL</b>\n\n"
            f"⚙️ /autotrade on|off · /paper on|off\n"
            f"⚙️ /amount /tp /sl /maxpos /minconf /dailylimit\n"
            f"📂 /positions · 📊 /pnl · 👛 /wallet",
            parse_mode="HTML",
        )

    @dp.message(Command("autotrade"))
    async def cmd_autotrade(m: Message) -> None:
        args = m.text.split()[1:]
        s = await get_trading_settings()
        if not args:
            state = "✅ ВКЛЮЧЕНА" if s["autotrade_enabled"] else "⛔ ВЫКЛЮЧЕНА"
            await m.answer(
                f"Автоторговля: <b>{state}</b>\n\n/autotrade on · /autotrade off",
                parse_mode="HTML",
            )
            return
        sub = args[0].lower()
        if sub not in ("on", "off"):
            await m.answer("Использование: /autotrade on | /autotrade off")
            return
        enabled = sub == "on"
        await update_trading_settings(autotrade_enabled=enabled)
        if enabled:
            mode_str = "📝 PAPER" if s["paper_mode"] else "💸 РЕАЛЬНЫЙ"
            await m.answer(
                f"✅ Автоторговля включена ({mode_str}).\n"
                f"💰 {s['position_size_sol']:g} SOL/сделку · "
                f"🎯 TP +{s['take_profit_pct']:.0f}% · 🛑 SL -{s['stop_loss_pct']:.0f}% · "
                f"🎚 Мин. confidence {s['min_confidence_trade']}/100",
                parse_mode="HTML",
            )
        else:
            await m.answer(
                "⛔ Автоторговля выключена. Новые позиции не открываются, "
                "но открытые продолжат мониториться на TP/SL."
            )

    @dp.message(Command("paper"))
    async def cmd_paper(m: Message) -> None:
        args = m.text.split()[1:]
        s = await get_trading_settings()
        if not args:
            mode = "📝 PAPER (симуляция)" if s["paper_mode"] else "💸 РЕАЛЬНЫЙ"
            await m.answer(
                f"Текущий режим: <b>{mode}</b>\n\n/paper on · /paper off",
                parse_mode="HTML",
            )
            return
        sub = args[0].lower()
        if sub == "on":
            await update_trading_settings(paper_mode=True)
            _pending_paper_off["ts"] = 0
            await m.answer("📝 Включён paper-режим. Реальные деньги не используются.")
            return
        if sub == "off":
            if len(args) > 1 and args[1].lower() == "confirm":
                if time.time() - _pending_paper_off["ts"] > 120:
                    await m.answer("⏱ Подтверждение устарело. Отправь /paper off ещё раз.")
                    return
                if not config.WALLET_PRIVATE_KEY:
                    await m.answer(
                        "❌ WALLET_PRIVATE_KEY не задан в .env / Environment — "
                        "реальная торговля невозможна. Добавь переменную и "
                        "перезапусти бота."
                    )
                    return
                await update_trading_settings(paper_mode=False)
                _pending_paper_off["ts"] = 0
                await m.answer(
                    "💸 <b>ВКЛЮЧЁН РЕАЛЬНЫЙ РЕЖИМ.</b>\n"
                    "Бот будет тратить реальные SOL с твоего кошелька. "
                    "Если автоторговля выключена — сделок не будет, пока "
                    "не отправишь /autotrade on.",
                    parse_mode="HTML",
                )
                return
            _pending_paper_off["ts"] = time.time()
            await m.answer(
                "⚠️ <b>Внимание!</b> Ты собираешься включить торговлю "
                "РЕАЛЬНЫМИ деньгами. Бот будет автоматически покупать и "
                "продавать токены с твоего кошелька без подтверждения на "
                "каждую сделку.\n\n"
                "Если уверен — отправь:\n<code>/paper off confirm</code>\n"
                "(действует 2 минуты)",
                parse_mode="HTML",
            )
            return
        await m.answer("Использование: /paper on | /paper off")

    @dp.message(Command("amount"))
    async def cmd_amount(m: Message) -> None:
        args = m.text.split()[1:]
        if not args:
            s = await get_trading_settings()
            await m.answer(
                f"💰 Сумма на сделку: <b>{s['position_size_sol']:g} SOL</b>\n"
                f"Изменить: /amount 0.1",
                parse_mode="HTML",
            )
            return
        try:
            val = float(args[0].replace(",", "."))
            if val <= 0:
                raise ValueError
        except ValueError:
            await m.answer("Укажи положительное число, например: /amount 0.1")
            return
        await update_trading_settings(position_size_sol=val)
        await m.answer(f"✅ Сумма на сделку: <b>{val:g} SOL</b>", parse_mode="HTML")

    @dp.message(Command("tp"))
    async def cmd_tp(m: Message) -> None:
        args = m.text.split()[1:]
        if not args:
            s = await get_trading_settings()
            await m.answer(
                f"🎯 Take Profit: <b>+{s['take_profit_pct']:.0f}%</b>\n"
                f"Изменить: /tp 100",
                parse_mode="HTML",
            )
            return
        try:
            val = float(args[0].replace(",", "."))
            if val <= 0:
                raise ValueError
        except ValueError:
            await m.answer("Укажи положительный процент, например: /tp 100")
            return
        await update_trading_settings(take_profit_pct=val)
        await m.answer(f"✅ Take Profit: <b>+{val:.0f}%</b>", parse_mode="HTML")

    @dp.message(Command("sl"))
    async def cmd_sl(m: Message) -> None:
        args = m.text.split()[1:]
        if not args:
            s = await get_trading_settings()
            await m.answer(
                f"🛑 Stop Loss: <b>-{s['stop_loss_pct']:.0f}%</b>\n"
                f"Изменить: /sl 30",
                parse_mode="HTML",
            )
            return
        try:
            val = float(args[0].replace(",", "."))
            if not (0 < val < 100):
                raise ValueError
        except ValueError:
            await m.answer("Укажи процент от 0 до 100, например: /sl 30")
            return
        await update_trading_settings(stop_loss_pct=val)
        await m.answer(f"✅ Stop Loss: <b>-{val:.0f}%</b>", parse_mode="HTML")

    @dp.message(Command("maxpos"))
    async def cmd_maxpos(m: Message) -> None:
        args = m.text.split()[1:]
        if not args:
            s = await get_trading_settings()
            await m.answer(
                f"📦 Максимум открытых позиций: <b>{s['max_positions']}</b>\n"
                f"Изменить: /maxpos 5",
                parse_mode="HTML",
            )
            return
        try:
            val = int(args[0])
            if val <= 0:
                raise ValueError
        except ValueError:
            await m.answer("Укажи целое число > 0, например: /maxpos 5")
            return
        await update_trading_settings(max_positions=val)
        await m.answer(f"✅ Максимум позиций: <b>{val}</b>", parse_mode="HTML")

    @dp.message(Command("minconf"))
    async def cmd_minconf(m: Message) -> None:
        args = m.text.split()[1:]
        if not args:
            s = await get_trading_settings()
            await m.answer(
                f"🎚 Мин. confidence для автоторговли: <b>{s['min_confidence_trade']}/100</b>\n"
                f"Изменить: /minconf 60",
                parse_mode="HTML",
            )
            return
        try:
            val = int(args[0])
            if not (0 <= val <= 100):
                raise ValueError
        except ValueError:
            await m.answer("Укажи число от 0 до 100, например: /minconf 60")
            return
        await update_trading_settings(min_confidence_trade=val)
        await m.answer(f"✅ Мин. confidence: <b>{val}/100</b>", parse_mode="HTML")

    @dp.message(Command("dailylimit"))
    async def cmd_dailylimit(m: Message) -> None:
        args = m.text.split()[1:]
        if not args:
            s = await get_trading_settings()
            await m.answer(
                f"🛑 Дневной лимит убытка: <b>{s['daily_loss_limit_sol']:g} SOL</b>\n"
                f"Изменить: /dailylimit 1.0",
                parse_mode="HTML",
            )
            return
        try:
            val = float(args[0].replace(",", "."))
            if val <= 0:
                raise ValueError
        except ValueError:
            await m.answer("Укажи положительное число, например: /dailylimit 1.0")
            return
        await update_trading_settings(daily_loss_limit_sol=val)
        await m.answer(f"✅ Дневной лимит убытка: <b>{val:g} SOL</b>", parse_mode="HTML")

    @dp.message(Command("positions"))
    async def cmd_positions(m: Message) -> None:
        positions = await get_open_positions()
        if not positions:
            await m.answer("📭 Открытых позиций нет")
            return
        import aiohttp
        from analysis import get_dex_data
        lines = []
        async with aiohttp.ClientSession() as session:
            for p in positions:
                dex = await get_dex_data(session, p["ca"])
                price = dex.get("price") or p["entry_price"]
                pnl_pct = (
                    (price - p["entry_price"]) / p["entry_price"] * 100
                    if p["entry_price"] else 0
                )
                tag = "📝" if p["mode"] == "paper" else "💸"
                emoji = "🟢" if pnl_pct >= 0 else "🔴"
                lines.append(
                    f"{tag} <b>{p['symbol']}</b> {emoji} {pnl_pct:+.1f}%\n"
                    f"   Вход ${p['entry_price']:.8f} → Сейчас ${price:.8f}\n"
                    f"   {p['sol_amount']:g} SOL · TP ${p['tp_price']:.8f} · "
                    f"SL ${p['sl_price']:.8f}\n"
                    f"   <code>{p['ca']}</code>"
                )
        await m.answer(
            f"📂 <b>Открытые позиции ({len(positions)})</b>\n\n" + "\n\n".join(lines),
            parse_mode="HTML", disable_web_page_preview=True,
        )

    @dp.message(Command("pnl"))
    async def cmd_pnl(m: Message) -> None:
        st = await get_trading_stats()
        closed = await get_closed_positions(10)
        lines = []
        for p in closed:
            tag = "📝" if p["mode"] == "paper" else "💸"
            pnl_sol = p["pnl_sol"] or 0.0
            emoji = "🟢" if pnl_sol >= 0 else "🔴"
            reason = "🎯TP" if p["exit_reason"] == "take_profit" else "🛑SL"
            lines.append(
                f"{tag} {p['symbol']} {emoji} {p['pnl_pct']:+.1f}% "
                f"({pnl_sol:+.4f} SOL) {reason}"
            )
        body = "\n".join(lines) if lines else "Пока нет закрытых сделок"
        await m.answer(
            f"📊 <b>Итоги торговли</b>\n\n"
            f"Сделок: <b>{st['trades_count']}</b> · "
            f"Win/Loss: <b>{st['wins']}/{st['losses']}</b> ({st['win_rate']:.0f}%)\n"
            f"Total PnL: <b>{st['total_pnl_sol']:+.4f} SOL</b>\n\n"
            f"<b>Последние сделки:</b>\n{body}",
            parse_mode="HTML",
        )

    @dp.message(Command("wallet"))
    async def cmd_wallet(m: Message) -> None:
        if trading_engine is None:
            await m.answer("Торговый модуль не инициализирован")
            return
        info = await trading_engine.get_wallet_info()
        if info["mode"] == "paper":
            await m.answer(
                f"📝 Paper-режим — реальный кошелёк не используется.\n"
                f"Виртуальный стартовый баланс: <b>{info['balance_sol']:g} SOL</b>",
                parse_mode="HTML",
            )
            return
        if not info.get("ready"):
            await m.answer(
                "❌ Кошелёк не настроен. Задай WALLET_PRIVATE_KEY в .env / "
                "Environment и перезапусти бота."
            )
            return
        pk = info["pubkey"]
        await m.answer(
            f"💸 Кошелёк: <code>{pk[:4]}...{pk[-4:]}</code>\n"
            f"Баланс: <b>{info['balance_sol']:.4f} SOL</b>",
            parse_mode="HTML",
        )
