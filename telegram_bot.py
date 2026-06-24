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
    dp: Dispatcher, stats: dict, queues: dict, exit_engine=None, cache=None
) -> None:

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
            "🔍 Bundle detection (% threshold)\n\n"
            "/stats /queue /filters /blacklist\n"
            "/topcreators /retry /health /cache /weights /last",
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
