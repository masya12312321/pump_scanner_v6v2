"""
outcome_tracker.py — Automatic Outcome Tracking
Every 5 min: check alerted tokens older than 30 min.
Fetch current MCap via Dexscreener. Record x2/x5/x10/rug/dead.
Closes the ML feedback loop.
"""
import asyncio
import logging
import time

import aiohttp

import config
from database import (
    get_alerts_for_outcome_check,
    update_alert_outcome,
    update_sample_result,
    increment_creator_field,
)
from creator_scoring import record_outcome

log = logging.getLogger("OutcomeTracker")

CHECK_INTERVAL   = 300    # 5 min
MIN_AGE_SECONDS  = 1800   # 30 min before we check outcome
DEAD_THRESHOLD   = 0.10   # <10% of initial mcap = dead
RUG_THRESHOLD    = 0.05   # <5%  = rug

_dex_sem: asyncio.Semaphore | None = None


def init_outcome_tracker(dex_sem: asyncio.Semaphore) -> None:
    global _dex_sem
    _dex_sem = dex_sem


async def _fetch_current_mcap(session: aiohttp.ClientSession, ca: str) -> float:
    sem = _dex_sem or asyncio.Semaphore(3)
    async with sem:
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


def _classify_outcome(initial_mcap: float, current_mcap: float) -> str | None:
    if initial_mcap <= 0 or current_mcap <= 0:
        return None
    ratio = current_mcap / initial_mcap
    if ratio >= 10:   return "x10"
    if ratio >= 5:    return "x5"
    if ratio >= 2:    return "x2"
    if ratio <= RUG_THRESHOLD:  return "rug"
    if ratio <= DEAD_THRESHOLD: return "dead"
    return None   # still alive, check later


async def outcome_tracker_loop() -> None:
    log.info("Outcome tracker started")
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                await asyncio.sleep(CHECK_INTERVAL)
                await _run_check(session)
            except asyncio.CancelledError:
                log.info("Outcome tracker stopped")
                return
            except Exception as exc:
                log.error(f"Outcome tracker error: {exc}")


async def _run_check(session: aiohttp.ClientSession) -> None:
    pending = await get_alerts_for_outcome_check(MIN_AGE_SECONDS)
    if not pending:
        return

    log.info(f"OutcomeTracker: checking {len(pending)} tokens")

    tasks = [
        _check_one(session, row)
        for row in pending
    ]
    await asyncio.gather(*tasks, return_exceptions=True)


async def _check_one(session: aiohttp.ClientSession, row: dict) -> None:
    ca           = row["ca"]
    symbol       = row["symbol"]
    creator      = row.get("creator", "")
    initial_mcap = row.get("mcap", 0.0)
    sent_at      = row.get("sent_at", 0)

    current_mcap = await _fetch_current_mcap(session, ca)
    outcome      = _classify_outcome(initial_mcap, current_mcap)

    if outcome is None:
        # Token still alive — check again later (only record after 2h)
        age = int(time.time()) - sent_at
        if age > 7200 and current_mcap > 0:
            # 2h+ no clear outcome → mark dead
            outcome = "dead"
        else:
            return

    log.info(f"OUTCOME {symbol}: {outcome} "
             f"(${initial_mcap:,.0f} → ${current_mcap:,.0f})")

    await update_alert_outcome(ca, outcome, current_mcap)
    await update_sample_result(ca, outcome)

    if creator:
        field_map = {
            "x10": "tokens_x10",
            "x5":  "tokens_x5",
            "x2":  "tokens_x2",
            "rug": "tokens_rugged",
        }
        field = field_map.get(outcome)
        if field:
            await increment_creator_field(creator, field)
        # recompute creator score
        await record_outcome(creator, outcome)
