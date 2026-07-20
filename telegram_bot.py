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
from x_calculator import calculate_x
from database import (
    get_alert_count, get_seen_count,
    get_blacklist_entries,
    save_alert, mark_alerted, add_blacklist,
    increment_creator_field, get_recent_alerts,
    get_trading_settings, update_trading_settings,
    get_open_positions, get_closed_positions,
    get_trading_stats, count_open_positions,
    get_daily_pnl_sol, update_open_positions_tpsl,
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
    x_pred  = calculate_x(
        mcap=sig.mcap, confidence=sig.confidence, rug_score=sig.rug_score,
        momentum=sig.momentum, age_min=sig.age_min, creator_score=sig.creator_score,
        liquidity=sig.liquidity, volume_3m=sig.volume_15m,
    )

    # Грейд и уровень уверенности
    grade_emoji = {"A": "🏆", "B": "🥈", "C": "🥉", "D": "💀"}.get(x_pred.grade, "❓")
    conf_bar    = "█" * (sig.confidence // 10) + "░" * (10 - sig.confidence // 10)

    # Momentum коротко
    mom_parts = []
    if abs(sig.change_1m)  >= 1: mom_parts.append(f"1м:{sig.change_1m:+.0f}%")
    if abs(sig.change_3m)  >= 1: mom_parts.append(f"3м:{sig.change_3m:+.0f}%")
    if abs(sig.change_5m)  >= 1: mom_parts.append(f"5м:{sig.change_5m:+.0f}%")
    mom_str = " ".join(mom_parts) if mom_parts else "накапливаем"

    # Rug/security одной строкой
    rug_emoji = "🟢" if sig.rug_score < 20 else "🟡" if sig.rug_score < 45 else "🔴"
    tax_str   = f" · Tax:{sig.sell_tax:.0f}%" if sig.sell_tax > 0 else ""
    honey_str = " · 🚫HONEYPOT" if sig.is_honeypot else ""
    raydium   = " · 🟢Ray" if sig.on_raydium else ""

    # Топ-флаги риска (max 2 строки чтобы не раздувать)
    top_risks   = sig.risk_flags[:2]
    top_greens  = sig.green_flags[:2]
    flags_lines = ""
    for f in top_greens:
        flags_lines += f"{f}\n"
    for f in top_risks:
        flags_lines += f"{f}\n"

    pump_url = f"https://pump.fun/{sig.ca}"
    dex_url  = sig.dex_url or f"https://dexscreener.com/solana/{sig.ca}"
    scan_url = f"https://solscan.io/token/{sig.ca}"

    return (
        f"{grade_emoji} <b>{sig.symbol}</b> — {x_pred.grade} Grade"
        f"{raydium}\n"
        f"<code>{conf_bar}</code> {sig.confidence}/100\n\n"

        f"💰 MCap: <b>{fmt_usd(sig.mcap)}</b>  "
        f"💧 Liq: <b>{fmt_usd(sig.liquidity)}</b>  "
        f"⏳ <b>{sig.age_min:.1f}м</b>\n"

        f"📈 {sig.momentum}  {mom_str}\n"
        f"👥 {sig.holders_count} холд  "
        f"T1:{sig.top1_pct:.0f}%  T5:{sig.top5_pct:.0f}%\n\n"

        f"{rug_emoji} Rug:{sig.rug_score}/100"
        f"{tax_str}{honey_str}  "
        f"👤 Creator:{sig.creator_score}/100\n"

        f"{flags_lines}\n"

        f"🎯 x2:<b>{x_pred.x2_prob*100:.0f}%</b>  "
        f"x5:<b>{x_pred.x5_prob*100:.0f}%</b>  "
        f"x10:<b>{x_pred.x10_prob*100:.0f}%</b>\n"
        f"⏱ Выход: <b>{x_pred.best_exit_window}</b>  "
        f"Пик~<b>{fmt_usd(x_pred.peak_mcap_est)}</b>\n\n"

        f"<code>{sig.ca}</code>\n"
        f"<a href='{pump_url}'>Pump</a> · "
        f"<a href='{dex_url}'>Dex</a> · "
        f"<a href='{scan_url}'>Scan</a>"
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
            # honeypot больше не блокирует алерт — показываем с предупреждением
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


def register_commands(
    dp: Dispatcher, stats: dict, queues: dict, exit_engine=None, cache=None,
    trading_engine=None,
) -> None:

    _pending_paper_off = {"ts": 0.0}

    @dp.message(Command("start"))
    async def cmd_start(m: Message) -> None:
        await m.answer(
            "🧠 <b>DEXMIND</b> — pump.fun автосканер\n\n"
            "<b>Мониторинг:</b>\n"
            "/stats — общая статистика\n"
            "/health — статус бота\n"
            "/last — последние 5 алертов\n"
            "/creators — топ создателей\n"
            "/bl — блэклист токенов\n\n"
            "<b>Автоторговля:</b>\n"
            "/trading — настройки и итоги\n"
            "/autotrade on|off — вкл/выкл\n"
            "/paper on|off — симуляция / реал\n"
            "/amount /tp /sl /maxpos /minconf /dailylimit\n"
            "/positions — открытые позиции\n"
            "/pnl — история сделок\n"
            "/wallet — баланс кошелька",
            parse_mode="HTML",
        )

    @dp.message(Command("stats"))
    async def cmd_stats(m: Message) -> None:
        seen    = await get_seen_count()
        alerted = await get_alert_count()
        uptime  = int(time.time()) - stats.get("start_time", int(time.time()))
        h, mi   = uptime // 3600, (uptime % 3600) // 60
        await m.answer(
            f"📊 <b>DEXMIND — статистика</b>\n\n"
            f"⏱ Аптайм: <b>{h}ч {mi}м</b>\n"
            f"📡 WS реконнект: <b>{stats.get('ws_reconnects',0)}</b>\n\n"
            f"📥 Получено токенов: <b>{stats.get('ws_received',0)}</b>\n"
            f"🔍 Проанализировано: <b>{seen}</b>\n"
            f"🚨 Алертов отправлено: <b>{alerted}</b>\n"
            f"🍯 Honeypot заблок.: <b>{stats.get('blocked_honeypot',0)}</b>\n"
            f"📉 Низкий conf: <b>{stats.get('blocked_confidence',0)}</b>\n"
            f"🔄 Retry: <b>{stats.get('retry_sent',0)}</b>\n\n"
            f"⚙️ <b>Фильтры:</b>\n"
            f"Мин. confidence: <b>{config.MIN_CONFIDENCE_SCORE}/100</b>\n"
            f"Мин. ликвидность: <b>${config.MIN_LIQUIDITY_USD:,.0f}</b>\n"
            f"Макс. возраст: <b>{config.MAX_AGE_MINUTES} мин</b>",
            parse_mode="HTML",
        )

    @dp.message(Command("health"))
    async def cmd_health(m: Message) -> None:
        uptime = int(time.time()) - stats.get("start_time", int(time.time()))
        h, mi  = uptime // 3600, (uptime % 3600) // 60
        q_new  = queues['new'].qsize()
        q_alert = queues['alert'].qsize()
        ws_status = "🟢 OK" if stats.get('ws_reconnects', 0) < 5 else "🟡 Нестабильно"
        await m.answer(
            f"💚 <b>Health</b>\n\n"
            f"⏱ Uptime: <b>{h}ч {mi}м</b>\n"
            f"📡 WebSocket: <b>{ws_status}</b> (реконн: {stats.get('ws_reconnects',0)})\n"
            f"📦 Очередь токенов: <b>{q_new}</b>\n"
            f"📤 Очередь алертов: <b>{q_alert}</b>",
            parse_mode="HTML",
        )

    @dp.message(Command("bl"))
    async def cmd_blacklist(m: Message) -> None:
        entries = await get_blacklist_entries()
        if not entries:
            await m.answer("🚫 Блэклист пуст — honeypot-токены сюда попадают автоматически")
            return
        lines = "\n".join(
            f"<code>{e['ca'][:20]}…</code> {e['reason']}"
            for e in entries[:15]
        )
        await m.answer(
            f"🚫 <b>Блэклист</b> ({len(entries)} токенов)\n\n{lines}",
            parse_mode="HTML",
        )

    @dp.message(Command("creators"))
    async def cmd_creators(m: Message) -> None:
        from creator_scoring import get_top_creators
        creators = await get_top_creators(8)
        # показываем только тех у кого хоть что-то есть
        creators = [c for c in creators if c.get("tokens_created", 0) > 0]
        if not creators:
            await m.answer(
                "📭 История создателей пока пуста.\n"
                "Она наполняется автоматически по мере работы бота — "
                "обычно через несколько часов активного сканирования."
            )
            return
        lines = []
        for c in creators:
            total  = c.get("tokens_created", 0)
            rugged = c.get("tokens_rugged", 0)
            x10    = c.get("tokens_x10", 0)
            score  = c.get("score", 50)
            rug_pct = rugged / max(total, 1) * 100
            addr   = c.get("creator", "")[:10] + "…"
            score_emoji = "🟢" if score >= 70 else "🟡" if score >= 50 else "🔴"
            lines.append(
                f"{score_emoji} <code>{addr}</code> "
                f"Score:{score} | {total} токенов | "
                f"rug:{rug_pct:.0f}% x10:{x10}"
            )
        await m.answer(
            f"🏆 <b>Топ создатели</b>\n\n" + "\n".join(lines),
            parse_mode="HTML",
        )

    @dp.message(Command("last"))
    async def cmd_last(m: Message) -> None:
        alerts = await get_recent_alerts(5)
        if not alerts:
            await m.answer(
                "📭 Алертов ещё нет.\n"
                "Бот активно сканирует — первый сигнал появится как только "
                "токен пройдёт все фильтры (confidence ≥ 50, liq ≥ $500)."
            )
            return
        lines = []
        for a in alerts:
            ca      = a["ca"]
            symbol  = a.get("symbol", "???")
            mcap    = a.get("mcap") or 0
            outcome = a.get("outcome")
            out_mcp = a.get("outcome_mcap")
            sent_at = a.get("sent_at", 0)
            age_min = max(0, int((time.time() - sent_at) / 60))

            if outcome and out_mcp and mcap > 0:
                pnl = (out_mcp - mcap) / mcap * 100
                if pnl >= 100: e = "🚀"
                elif pnl >= 0: e = "🟢"
                else:          e = "🔴"
                result = f"{e} {pnl:+.0f}% ({outcome.upper()})"
            elif outcome:
                result = f"📋 {outcome.upper()}"
            else:
                result = "⏳ активен"

            dex = f"https://dexscreener.com/solana/{ca}"
            mcap_str = fmt_usd(mcap) if mcap else "—"
            lines.append(
                f"<a href='{dex}'><b>{symbol}</b></a> {mcap_str} · {age_min}м назад\n"
                f"  {result}"
            )
        await m.answer(
            "📋 <b>Последние алерты</b>\n\n" + "\n\n".join(lines),
            parse_mode="HTML", disable_web_page_preview=True,
        )

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
        s = await get_trading_settings()
        if not args:
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
        # сразу обновляем открытые позиции
        updated = await update_open_positions_tpsl(val, s["stop_loss_pct"])
        note = f"\n📂 Обновлено {updated} открытых позиций" if updated else ""
        await m.answer(f"✅ Take Profit: <b>+{val:.0f}%</b>{note}", parse_mode="HTML")

    @dp.message(Command("sl"))
    async def cmd_sl(m: Message) -> None:
        args = m.text.split()[1:]
        s = await get_trading_settings()
        if not args:
            await m.answer(
                f"🛑 Stop Loss: <b>-{s['stop_loss_pct']:.0f}%</b>\n"
                f"Изменить: /sl 30",
                parse_mode="HTML",
            )
            return
        try:
            val = float(args[0].replace(",", "."))
            if not (0 < val <= 99):
                raise ValueError
        except ValueError:
            await m.answer("Укажи процент от 1 до 99, например: /sl 30")
            return
        await update_trading_settings(stop_loss_pct=val)
        # сразу обновляем открытые позиции
        updated = await update_open_positions_tpsl(s["take_profit_pct"], val)
        note = f"\n📂 Обновлено {updated} открытых позиций" if updated else ""
        await m.answer(f"✅ Stop Loss: <b>-{val:.0f}%</b>{note}", parse_mode="HTML")

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
