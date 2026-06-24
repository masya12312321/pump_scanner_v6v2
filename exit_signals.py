"""
exit_signals.py v6
Fixes:
  - Rate-limited _check_all via Semaphore (max 5 concurrent Dex calls)
  - Whale Exit: compares CURRENT % vs INITIAL % snapshot taken at alert time
  - One alert per event type per token
"""
import asyncio
import logging
import time
from dataclasses import dataclass, field

import aiohttp

import config

log = logging.getLogger("ExitSignals")

CHECK_INTERVAL_SEC = 20
MAX_TRACK_MINUTES  = 30
_DEX_CONCURRENCY   = 5   # rate-limit Dex calls in exit engine


@dataclass
class TokenSnapshot:
    ca:              str
    symbol:          str
    creator:         str
    alerted_at:      float = field(default_factory=time.time)
    price:           float = 0.0
    liquidity:       float = 0.0
    volume_5m:       float = 0.0
    peak_price:      float = 0.0
    peak_liq:        float = 0.0
    peak_vol5m:      float = 0.0
    # Whale: store initial top-1 % to compare against
    initial_top1_pct: float = 0.0
    fired:           set   = field(default_factory=set)


class ExitSignalEngine:
    def __init__(self, bot, helius_sem: asyncio.Semaphore) -> None:
        self._bot      = bot
        self._tracked: dict[str, TokenSnapshot] = {}
        self._h_sem    = helius_sem
        self._d_sem    = asyncio.Semaphore(_DEX_CONCURRENCY)
        self._lock     = asyncio.Lock()

    async def register(
        self, ca: str, symbol: str, creator: str, initial_top1_pct: float = 0.0
    ) -> None:
        async with self._lock:
            if ca not in self._tracked:
                snap = TokenSnapshot(
                    ca=ca, symbol=symbol, creator=creator,
                    initial_top1_pct=initial_top1_pct,
                )
                self._tracked[ca] = snap
                log.debug(f"ExitEngine: tracking {symbol}")

    async def run(self) -> None:
        log.info("Exit signal engine started")
        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    await asyncio.sleep(CHECK_INTERVAL_SEC)
                    await self._check_all(session)
                except asyncio.CancelledError:
                    return
                except Exception as exc:
                    log.error(f"Exit engine: {exc}")

    async def _check_all(self, session: aiohttp.ClientSession) -> None:
        now = time.time()
        async with self._lock:
            expired = [
                ca for ca, s in self._tracked.items()
                if now - s.alerted_at > MAX_TRACK_MINUTES * 60
            ]
            for ca in expired:
                del self._tracked[ca]
            active = list(self._tracked.values())

        # Rate-limited gather — semaphore inside _check_one
        await asyncio.gather(
            *[self._check_one(session, s) for s in active],
            return_exceptions=True,
        )

    async def _check_one(
        self, session: aiohttp.ClientSession, snap: TokenSnapshot
    ) -> None:
        async with self._d_sem:
            dex = await self._fetch_dex(session, snap.ca)
        if not dex:
            return

        price     = dex.get("price",     0.0)
        liquidity = dex.get("liquidity", 0.0)
        vol5m     = dex.get("volume_5m", 0.0)

        snap.price     = price
        snap.liquidity = liquidity
        snap.volume_5m = vol5m
        if price     > snap.peak_price: snap.peak_price = price
        if liquidity > snap.peak_liq:   snap.peak_liq   = liquidity
        if vol5m     > snap.peak_vol5m: snap.peak_vol5m = vol5m

        reasons = []

        # Liquidity Drop >30%
        if "liq_drop" not in snap.fired and snap.peak_liq > 0:
            if liquidity < snap.peak_liq * 0.70:
                pct = (snap.peak_liq - liquidity) / snap.peak_liq * 100
                reasons.append(f"💧 Liquidity Drop {pct:.0f}% (${liquidity:,.0f} ← ${snap.peak_liq:,.0f})")
                snap.fired.add("liq_drop")

        # Momentum Collapse >25%
        if "mom_collapse" not in snap.fired and snap.peak_price > 0:
            if price < snap.peak_price * 0.75:
                pct = (snap.peak_price - price) / snap.peak_price * 100
                reasons.append(f"📉 Momentum Collapse {pct:.0f}% от пика")
                snap.fired.add("mom_collapse")

        # Volume Collapse >70%
        if "vol_collapse" not in snap.fired and snap.peak_vol5m > 0:
            if vol5m < snap.peak_vol5m * 0.30:
                pct = (snap.peak_vol5m - vol5m) / snap.peak_vol5m * 100
                reasons.append(f"📊 Volume Collapse {pct:.0f}%")
                snap.fired.add("vol_collapse")

        # Whale Exit: current top1% dropped significantly vs INITIAL snapshot
        if "whale_exit" not in snap.fired:
            whale_reason = await self._check_whale(session, snap)
            if whale_reason:
                reasons.append(whale_reason)
                snap.fired.add("whale_exit")

        if reasons:
            await self._send(snap, reasons)

    async def _check_whale(
        self, session: aiohttp.ClientSession, snap: TokenSnapshot
    ) -> str | None:
        """
        Compare current top-1 % with initial_top1_pct.
        Only fire if drop > 50% of initial holding (not just absolute <5%).
        """
        if snap.initial_top1_pct <= 0:
            return None
        async with self._h_sem:
            try:
                async with session.post(
                    config.HELIUS_RPC_URL,
                    json={"jsonrpc":"2.0","id":1,
                          "method":"getTokenLargestAccounts","params":[snap.ca]},
                    timeout=aiohttp.ClientTimeout(total=6),
                ) as r:
                    if r.status != 200:
                        return None
                    data  = await r.json()
                    value = data.get("result", {}).get("value", []) or []
            except Exception:
                return None
        if not value:
            return None
        amounts  = [int(v.get("amount", 0)) for v in value]
        total    = sum(amounts) or 1
        cur_top1 = amounts[0] / total * 100
        # fire only if current top1 dropped >50% relative to initial
        if cur_top1 < snap.initial_top1_pct * 0.50:
            return (f"🐋 Whale Exit: топ-1 упал с {snap.initial_top1_pct:.1f}% "
                    f"→ {cur_top1:.1f}%")
        return None

    async def _fetch_dex(
        self, session: aiohttp.ClientSession, ca: str
    ) -> dict:
        try:
            async with session.get(
                f"{config.DEXSCREENER_URL}/{ca}",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as r:
                if r.status != 200:
                    return {}
                data  = await r.json()
                pairs = data.get("pairs") or []
                if not pairs:
                    return {}
                p = pairs[0]
                return {
                    "price":     float(p.get("priceUsd") or 0),
                    "liquidity": (p.get("liquidity") or {}).get("usd", 0),
                    "volume_5m": (p.get("volume") or {}).get("m5", 0),
                }
        except Exception:
            return {}

    async def _send(self, snap: TokenSnapshot, reasons: list[str]) -> None:
        risk       = "HIGH" if len(reasons) >= 2 else "MEDIUM"
        risk_emoji = "🔴" if risk == "HIGH" else "🟠"
        lines      = "\n".join(f"• {r}" for r in reasons)
        msg = (
            f"⚠️ <b>EXIT SIGNAL</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🪙 Token: <b>{snap.symbol}</b>\n\n"
            f"🚨 <b>Причина:</b>\n{lines}\n\n"
            f"{risk_emoji} Risk: <b>{risk}</b>\n\n"
            f"💰 Цена: <b>${snap.price:.8f}</b>\n"
            f"💧 Ликвидность: <b>${snap.liquidity:,.0f}</b>\n\n"
            f"<code>{snap.ca}</code>\n\n"
            f"<a href='https://pump.fun/{snap.ca}'>Pump.fun</a> | "
            f"<a href='https://dexscreener.com/solana/{snap.ca}'>Dex</a>\n"
            f"━━━━━━━━━━━━━━━━━━━━"
        )
        try:
            await self._bot.send_message(
                chat_id=config.CHAT_ID, text=msg,
                parse_mode="HTML", disable_web_page_preview=True,
            )
        except Exception as exc:
            log.error(f"Exit alert failed: {exc}")
