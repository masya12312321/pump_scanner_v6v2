"""
x_milestone_tracker.py — Live X-Multiplier Tracking
═════════════════════════════════════════════════════
После того как DEXMIND отправил сигнал на токен, этот модуль продолжает
следить за ним и шлёт отдельное короткое сообщение каждый раз, когда
цена пересекает новый множитель (x2, x3, x5, x10, x20, x50, x100) —
в формате как у "XXX Phantom Trade":

    🪙 SYMBOL
    ⚡ x2.0 за 25м
    🕐 5 06:38

Не дублирует уже достигнутые множители — каждый токен хранит свой
максимальный пойманный X (`max_x_hit` в таблице alerts), сообщение
шлётся только когда фактический X превышает сохранённый.
"""
import asyncio
import logging
import time
from datetime import datetime, timezone

import aiohttp

import config
from database import (
    get_active_alerts_for_milestones,
    update_max_x_hit,
    mark_forecast_hit,
)

log = logging.getLogger("XMilestone")

CHECK_INTERVAL_SEC = 25          # как часто опрашиваем живые токены
MAX_TRACK_SECONDS  = 7200        # перестаём следить через 2 часа
DEX_CONCURRENCY    = 8

# Пороги множителей, на которые шлём отдельное уведомление.
# Отсортированы по убыванию — нужно поймать самый высокий достигнутый сразу,
# а не слать x2, потом через секунду x3, потом x5 одним и тем же тиком.
X_MILESTONES = [100.0, 50.0, 20.0, 10.0, 5.0, 3.0, 2.0]

_dex_sem: asyncio.Semaphore | None = None
_bot = None


def init_milestone_tracker(bot, dex_sem: asyncio.Semaphore | None = None) -> None:
    global _bot, _dex_sem
    _bot     = bot
    _dex_sem = dex_sem or asyncio.Semaphore(DEX_CONCURRENCY)


async def _fetch_mcap(session: aiohttp.ClientSession, ca: str) -> float:
    async with _dex_sem:
        try:
            async with session.get(
                f"{config.DEXSCREENER_URL}/{ca}",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as r:
                if r.status != 200:
                    return 0.0
                data  = await r.json()
                pairs = data.get("pairs") or []
                if not pairs:
                    return 0.0
                return float(pairs[0].get("marketCap") or pairs[0].get("fdv") or 0)
        except Exception:
            return 0.0


def _format_elapsed(seconds: int) -> str:
    """Формат как на скриншоте: '25m', '6h:11m'."""
    if seconds < 3600:
        minutes = max(1, seconds // 60)
        return f"{minutes}m"
    hours   = seconds // 3600
    minutes = (seconds % 3600) // 60
    return f"{hours}h:{minutes:02d}m"


def _format_timestamp(ts: float) -> str:
    """Формат: '5 06:38' по МСК (UTC+3)."""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    from datetime import timedelta
    dt_msk = dt + timedelta(hours=3)
    return f"{dt_msk.day} {dt_msk.strftime('%H:%M')} МСК"


async def milestone_tracker_loop() -> None:
    log.info("X-Milestone tracker started")
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                await asyncio.sleep(CHECK_INTERVAL_SEC)
                await _check_all(session)
            except asyncio.CancelledError:
                log.info("X-Milestone tracker stopped")
                return
            except Exception as exc:
                log.error(f"X-Milestone tracker error: {exc}")


async def _check_all(session: aiohttp.ClientSession) -> None:
    alerts = await get_active_alerts_for_milestones(MAX_TRACK_SECONDS)
    if not alerts:
        return

    await asyncio.gather(
        *[_check_one(session, a) for a in alerts],
        return_exceptions=True,
    )


async def _check_one(session: aiohttp.ClientSession, row: dict) -> None:
    ca           = row["ca"]
    symbol       = row["symbol"]
    initial_mcap = row.get("mcap", 0.0) or 0.0
    sent_at      = row.get("sent_at", int(time.time()))
    already_hit  = row.get("max_x_hit", 0.0) or 0.0
    predicted_peak = row.get("predicted_peak_mcap", 0.0) or 0.0
    forecast_hit   = bool(row.get("forecast_hit", 0))

    if initial_mcap <= 0:
        return

    current_mcap = await _fetch_mcap(session, ca)
    if current_mcap <= 0:
        return

    current_x = current_mcap / initial_mcap

    # ── 1. Прогноз: уведомление шлётся при достижении СПРОГНОЗИРОВАННОГО пика,
    #    независимо от того, насколько мал фактический X. Раньше уведомление
    #    приходило только начиная с x2, поэтому слабые сигналы (Grade C/D
    #    с прогнозом вроде x1.4) никогда не получали подтверждение, даже когда
    #    прогноз полностью оправдался. Теперь — отдельный, более низкий триггер.
    if not forecast_hit and predicted_peak > initial_mcap and current_mcap >= predicted_peak:
        await mark_forecast_hit(ca)
        await _send_forecast_hit(symbol, ca, current_x, sent_at)
        # не return — тот же тик может ОДНОВРЕМЕННО пересечь и круглый порог x2+,
        # тогда придут оба сообщения, это нормально (разная информация)

    # ── 2. Круглые пороги x2/x3/x5/x10/x20/x50/x100 — для заметных движений ──
    new_milestone = None
    for threshold in X_MILESTONES:
        if current_x >= threshold and already_hit < threshold:
            new_milestone = threshold
            break

    if new_milestone is None:
        return

    await update_max_x_hit(ca, new_milestone)
    await _send_milestone(symbol, ca, new_milestone, current_x, sent_at)


async def _send_forecast_hit(
    symbol:   str,
    ca:       str,
    actual_x: float,
    sent_at:  float,
) -> None:
    """
    Подтверждение, что спрогнозированный пик достигнут — даже если
    фактический X меньше x2. Без этого слабые сигналы (Grade C/D)
    никогда не получали обратную связь, даже когда прогноз сбывался.
    """
    if _bot is None:
        return

    elapsed_sec = int(time.time() - sent_at)
    elapsed_str = _format_elapsed(elapsed_sec)
    time_str    = _format_timestamp(time.time())

    dex_url  = f"https://dexscreener.com/solana/{ca}"
    pump_url = f"https://pump.fun/{ca}"

    msg = (
        f"🎯 <b>DEXMIND — ЦЕЛЬ ДОСТИГНУТА</b>\n"
        f"🪙 <b>{symbol}</b>\n"
        f"⚡ <b>x{actual_x:.2f}</b> за {elapsed_str}\n"
        f"🕐 {time_str}\n\n"
        f"<a href='{dex_url}'>Dex</a> | <a href='{pump_url}'>Pump.fun</a>"
    )

    try:
        await _bot.send_message(
            chat_id=config.CHAT_ID,
            text=msg,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        log.info(f"FORECAST HIT: {symbol} reached predicted peak (x{actual_x:.2f}) in {elapsed_str}")
    except Exception as exc:
        log.error(f"Forecast-hit send failed for {symbol}: {exc}")


async def _send_milestone(
    symbol:       str,
    ca:           str,
    milestone:    float,
    actual_x:     float,
    sent_at:      float,
) -> None:
    if _bot is None:
        log.warning("Milestone tracker: bot не инициализирован")
        return

    elapsed_sec = int(time.time() - sent_at)
    elapsed_str = _format_elapsed(elapsed_sec)
    time_str    = _format_timestamp(time.time())

    dex_url  = f"https://dexscreener.com/solana/{ca}"
    pump_url = f"https://pump.fun/{ca}"

    msg = (
        f"🧠 <b>DEXMIND</b>\n"
        f"🪙 <b>{symbol}</b>\n"
        f"⚡ <b>x{actual_x:.1f}</b> за {elapsed_str}\n"
        f"🕐 {time_str}\n\n"
        f"<a href='{dex_url}'>Dex</a> | <a href='{pump_url}'>Pump.fun</a>"
    )

    try:
        await _bot.send_message(
            chat_id=config.CHAT_ID,
            text=msg,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        log.info(f"MILESTONE: {symbol} hit x{milestone:.0f} (actual x{actual_x:.1f}) in {elapsed_str}")
    except Exception as exc:
        log.error(f"Milestone send failed for {symbol}: {exc}")
