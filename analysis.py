"""
analysis.py v6 — Full analysis engine.
Fixes:
  - All API calls go through CacheManager (no raw hits)
  - LRU-bounded _price_history (maxlen per token, global cap 5000 tokens)
  - Bundle detection rewritten: uses holding % threshold not address prefix
  - Volume uses m15 (not vol5m*0.6 hack)
  - Dead-token hard penalty: vol==0 and age>3min
  - Multi-pair / Raydium migration detection via Dexscreener pairs count
  - Self-learning weights feed into calculate_scores via get_dynamic_weights()
"""
import asyncio
import logging
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field

import aiohttp

import config
from cache_manager import get_cache
from creator_scoring import get_creator_score, creator_flags
from self_learning import get_dynamic_weights

log = logging.getLogger("Analysis")

# ─── SEMAPHORES (initialised in main.py) ──────────────────────────────────────
_helius_sem:      asyncio.Semaphore | None = None
_rugcheck_sem:    asyncio.Semaphore | None = None
_dexscreener_sem: asyncio.Semaphore | None = None


def init_semaphores() -> None:
    global _helius_sem, _rugcheck_sem, _dexscreener_sem
    _helius_sem      = asyncio.Semaphore(config.HELIUS_CONCURRENCY)
    _rugcheck_sem    = asyncio.Semaphore(config.RUGCHECK_CONCURRENCY)
    _dexscreener_sem = asyncio.Semaphore(config.DEXSCREENER_CONCURRENCY)


# ─── PRICE HISTORY (LRU-bounded, max 5000 tokens) ────────────────────────────

_MAX_TRACKED_TOKENS = 5000

class _LRUPriceStore:
    """Stores price history per token. Evicts oldest token when full."""
    def __init__(self, max_tokens: int = _MAX_TRACKED_TOKENS) -> None:
        self._data: OrderedDict[str, deque] = OrderedDict()
        self._ath:  dict[str, float]        = {}
        self._max   = max_tokens

    def record(self, ca: str, price: float) -> None:
        if ca not in self._data:
            if len(self._data) >= self._max:
                evicted, _ = self._data.popitem(last=False)
                self._ath.pop(evicted, None)
            self._data[ca] = deque(maxlen=config.PRICE_HISTORY_LIMIT)
        self._data.move_to_end(ca)
        self._data[ca].append((time.time(), price))
        if price > self._ath.get(ca, 0.0):
            self._ath[ca] = price

    def get_ath(self, ca: str) -> float:
        return self._ath.get(ca, 0.0)

    def get_history(self, ca: str) -> list[tuple[float, float]]:
        return list(self._data.get(ca, []))

    def is_cooled_down(self, ca: str, price: float) -> bool:
        ath = self._ath.get(ca, 0.0)
        if ath <= 0 or price <= 0:
            return False
        return (ath - price) / ath > config.AUTO_COOLDOWN_DROP

    def size(self) -> int:
        return len(self._data)


_price_store = _LRUPriceStore()


def record_price(ca: str, price: float) -> None:
    _price_store.record(ca, price)


def get_ath(ca: str) -> float:
    return _price_store.get_ath(ca)


def get_momentum(ca: str) -> tuple[str, float, float, float]:
    history = _price_store.get_history(ca)
    if len(history) < 2:
        return "⏳ НАКАПЛИВАЕМ", 0.0, 0.0, 0.0
    now   = time.time()
    price = history[-1][1]

    def pct(seconds: int) -> float:
        past = [p for t, p in history if t <= now - seconds]
        if not past or past[-1] == 0:
            return 0.0
        return (price - past[-1]) / past[-1] * 100

    c1m, c3m, c5m = pct(60), pct(180), pct(300)
    last3 = [p for _, p in history[-3:]]
    if len(last3) == 3:
        if last3[0] < last3[1] < last3[2]: direction = "📈 UP"
        elif last3[0] > last3[1] > last3[2]: direction = "📉 DOWN"
        else: direction = "➡️ FLAT"
    else:
        direction = "⏳ НАКАПЛИВАЕМ"
    return direction, c1m, c3m, c5m


def is_cooled_down(ca: str, price: float) -> bool:
    return _price_store.is_cooled_down(ca, price)


# ─── HELIUS RPC (with cache) ──────────────────────────────────────────────────

async def _helius_post(session: aiohttp.ClientSession, payload: dict) -> dict:
    async with _helius_sem:
        try:
            async with session.post(
                config.HELIUS_RPC_URL,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=config.HELIUS_TIMEOUT),
            ) as r:
                return await r.json() if r.status == 200 else {}
        except Exception:
            return {}


async def get_mint_info(session: aiohttp.ClientSession, ca: str) -> dict:
    cache = get_cache()
    cached = await cache.get("mint_info", ca)
    if cached is not None:
        return cached
    data = await _helius_post(session, {
        "jsonrpc": "2.0", "id": 1,
        "method": "getAccountInfo",
        "params": [ca, {"encoding": "jsonParsed"}],
    })
    parsed = (
        data.get("result", {})
            .get("value", {}) or {}
    )
    info = (parsed.get("data", {}) or {}).get("parsed", {}).get("info", {})
    result = {
        "mint_authority":   info.get("mintAuthority")   is not None,
        "freeze_authority": info.get("freezeAuthority") is not None,
        "supply":           int(info.get("supply", 0)),
        "decimals":         info.get("decimals", 6),
    }
    await cache.set("mint_info", ca, result)
    return result


async def get_largest_accounts(session: aiohttp.ClientSession, ca: str) -> dict:
    cache = get_cache()
    cached = await cache.get("largest_accounts", ca)
    if cached is not None:
        return cached
    # _helius_post уже захватывает _helius_sem внутри — не оборачиваем снаружи
    data = await _helius_post(session, {
        "jsonrpc": "2.0", "id": 1,
        "method": "getTokenLargestAccounts",
        "params": [ca],
    })
    value = data.get("result", {}).get("value", []) or []
    if not value:
        return {}
    amounts   = [int(v.get("amount", 0)) for v in value]
    addresses = [v.get("address", "") for v in value]
    total     = sum(amounts) or 1
    result = {
        "top1_pct":      amounts[0] / total * 100 if amounts else 0,
        "top5_pct":      sum(amounts[:5])  / total * 100,
        "top10_pct":     sum(amounts[:10]) / total * 100,
        "holders_count": len(value),
        "top_addresses": addresses[:10],
        "amounts":       amounts[:10],
        "total_supply":  total,
    }
    await cache.set("largest_accounts", ca, result)
    return result


# ─── RUGCHECK (with cache) ────────────────────────────────────────────────────

async def check_rugcheck(session: aiohttp.ClientSession, ca: str) -> dict:
    cache = get_cache()
    cached = await cache.get("rugcheck", ca)
    if cached is not None:
        return cached
    url = f"{config.RUGCHECK_URL}/{ca}/report/summary"
    async with _rugcheck_sem:
        try:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=config.RUGCHECK_TIMEOUT)
            ) as r:
                raw = await r.json() if r.status == 200 else {}
        except asyncio.TimeoutError:
            return {}
        except Exception:
            return {}
    await cache.set("rugcheck", ca, raw)
    return raw


def parse_rugcheck(data: dict) -> dict:
    if not data:
        return {"is_honeypot": False, "rugcheck_score": 0,
                "sell_tax": 0.0, "buy_tax": 0.0, "trade_frozen": False, "risks": []}
    risks    = data.get("risks", []) or []
    hp_kw    = ["honeypot", "cannot sell", "sell disabled", "transfer fee 100"]
    is_hp    = False
    frozen   = False
    sell_tax = 0.0
    buy_tax  = 0.0
    for r in risks:
        combined = (r.get("name", "") + " " + r.get("description", "")).lower()
        if any(k in combined for k in hp_kw): is_hp = True
        if "freeze" in combined and "trading" in combined: frozen = True
        if "sell tax" in combined:
            try: sell_tax = float(r.get("value", 0))
            except: sell_tax = 100.0
        if "buy tax" in combined:
            try: buy_tax = float(r.get("value", 0))
            except: buy_tax = 0.0
    return {
        "is_honeypot":    is_hp,
        "rugcheck_score": data.get("score", 0),
        "sell_tax":       sell_tax,
        "buy_tax":        buy_tax,
        "trade_frozen":   frozen,
        "risks":          risks,
    }


# ─── DEXSCREENER (with cache + multi-pair detection) ─────────────────────────

async def get_dex_data(session: aiohttp.ClientSession, ca: str) -> dict:
    cache  = get_cache()
    cached = await cache.get("dexscreener", ca)
    if cached is not None:
        return cached
    url = f"{config.DEXSCREENER_URL}/{ca}"
    async with _dexscreener_sem:
        try:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=config.DEXSCREENER_TIMEOUT)
            ) as r:
                data = await r.json() if r.status == 200 else {}
        except Exception:
            return {}
    pairs = data.get("pairs") or []
    if not pairs:
        return {}
    p = pairs[0]
    vol = p.get("volume") or {}

    # multi-dex detection: count distinct dexIds
    dex_ids        = list({x.get("dexId","") for x in pairs if x.get("dexId")})
    on_raydium     = any("raydium" in (x.get("dexId","")).lower() for x in pairs)
    on_meteora     = any("meteora" in (x.get("dexId","")).lower() for x in pairs)

    result = {
        "mcap":        p.get("marketCap") or p.get("fdv") or 0,
        "price":       float(p.get("priceUsd") or 0),
        "liquidity":   (p.get("liquidity") or {}).get("usd", 0),
        "volume_5m":   vol.get("m5",  0),
        "volume_15m":  vol.get("m15", 0),   # ← use m15 directly
        "volume_1h":   vol.get("h1",  0),
        "dex_url":     p.get("url", ""),
        "pair_count":  len(pairs),
        "on_raydium":  on_raydium,
        "on_meteora":  on_meteora,
        "dex_ids":     dex_ids,
    }
    await cache.set("dexscreener", ca, result)
    return result


def needs_retry(dex: dict) -> bool:
    return dex.get("mcap", 0) == 0 and dex.get("liquidity", 0) == 0


# ─── BUNDLE DETECTION (fixed — no prefix heuristic) ──────────────────────────

def detect_bundles(
    amounts: list[int],
    total_supply: int,
) -> tuple[int, list[str]]:
    """
    Detects suspicious holder concentration without address prefix tricks.
    Flags groups of wallets each holding 3-15% (coordinated sniper pattern).
    """
    flags        = []
    bundle_count = 0

    if not amounts or total_supply <= 0:
        return 0, []

    pcts = [a / total_supply * 100 for a in amounts]

    # sniper band: multiple wallets each holding 3-15% (coordinated buy-in)
    sniper_band  = [p for p in pcts if 3 <= p <= 15]
    if len(sniper_band) >= 3:
        combined = sum(sniper_band)
        bundle_count = len(sniper_band)
        flags.append(
            f"🎯 {len(sniper_band)} кошельков со схожими долями "
            f"({min(sniper_band):.1f}–{max(sniper_band):.1f}%), "
            f"суммарно {combined:.1f}%"
        )

    # single dominant wallet above 40% (but not 100% — that's just dev wallet)
    if pcts and 40 < pcts[0] < 95:
        flags.append(f"🐋 Топ-1 держит {pcts[0]:.1f}% — доминирование")
        bundle_count += 1

    return bundle_count, flags


# ─── CONFIDENCE + RUG SCORE (dynamic weights from self-learning) ──────────────

def calculate_scores(
    age_min:      float,
    mcap:         float,
    liquidity:    float,
    volume_15m:   float,
    volume_5m:    float,
    mint_auth:    bool,
    freeze_auth:  bool,
    top1_pct:     float,
    top5_pct:     float,
    top10_pct:    float,
    rc:           dict,
    momentum:     str,
    creator_score: int,
    bundle_flags: list[str],
    bundle_count: int,
    on_raydium:   bool,
    on_meteora:   bool,
    pair_count:   int,
    ca:           str,
    blacklist:    set[str],
    holders_count: int = 0,
) -> tuple[int, int, list[str], list[str]]:

    # pull live weights from self-learning module
    w = get_dynamic_weights()
    confidence  = 50
    rug_score   = 0
    risk_flags  = []
    green_flags = []

    if ca in blacklist:
        return 0, 100, ["🚫 CA в блэклисте"], []

    # Honeypot — больше не блокируем полностью, только сильный штраф.
    # Детектор иногда ошибается, лучше показать алерт с предупреждением.
    if rc["is_honeypot"]:
        confidence -= 40
        rug_score  += 50
        risk_flags.append("🚫 ВОЗМОЖНЫЙ HONEYPOT — продать может быть невозможно")

    # ── ЖЁСТКИЕ ФИЛЬТРЫ: если сработал любой — rug_score сразу ≥60 ──────────────
    # rug_score > 60 в config: бот их всё равно зарежет на MIN_CONFIDENCE, но
    # явно маркируем чтобы x_calculator тоже применил штраф
    hard_fail = False

    # Критически высокий rug_score от RugCheck → сразу мусор
    if rc.get("rugcheck_score", 100) < 20:
        hard_fail = True
        rug_score += 50
        risk_flags.append("🔴 RugCheck: крайне опасный токен")

    # Слишком мало холдеров — нет реального интереса, легко манипулировать
    if 0 < holders_count < 25:
        confidence -= 20
        rug_score  += 15
        risk_flags.append(f"👻 Всего {holders_count} холдеров — почти никого")
    elif 0 < holders_count < 50:
        confidence -= 10
        risk_flags.append(f"⚠️ Мало холдеров: {holders_count}")
    elif holders_count >= 200:
        confidence += 8
        green_flags.append(f"✅ {holders_count} холдеров — широкое распределение")

    # Ликвидность / MCap ratio — признак накрутки объёма без реальных покупателей
    if mcap > 0 and liquidity > 0:
        liq_ratio = liquidity / mcap
        if liq_ratio < 0.01:   # ликвидность < 1% от MCap — насос без основы
            confidence -= 15
            rug_score  += 15
            risk_flags.append("💧 Ликвидность < 1% от MCap — манипуляция ценой")
        elif liq_ratio > 0.10:
            confidence += 8
            green_flags.append("✅ Хорошая ликвидность относительно MCap")

    # Падающая цена у немолодого токена — пик уже прошёл
    if "DOWN" in momentum and age_min > 3:
        confidence -= 15
        rug_score  += 10
        risk_flags.append("📉 Цена падает после 3 мин — пик скорее всего позади")

    if hard_fail:
        confidence = min(confidence, 20)

    # ── DEAD TOKEN hard penalty ──
    if volume_5m == 0 and age_min > 3:
        confidence -= 35
        rug_score  += 20
        risk_flags.append("💀 Объём = 0 и токен >3 мин — скорее всего мёртв")

    # ── trade frozen / sell tax ──
    if rc["trade_frozen"]:
        confidence -= 30; rug_score += 30
        risk_flags.append("🔒 Торговля заморожена")
    if rc["sell_tax"] > 10:
        confidence -= 20; rug_score += 20
        risk_flags.append(f"💸 Sell tax: {rc['sell_tax']:.0f}%")
    elif rc["sell_tax"] > 0:
        confidence -= 5
        risk_flags.append(f"⚠️ Sell tax: {rc['sell_tax']:.0f}%")

    # ── mint / freeze — weighted ──
    mf_w = w.get("mint_authority", 0.05)
    if mint_auth:
        d = int(15 * (mf_w / 0.05))
        confidence -= d; rug_score += int(20 * mf_w / 0.05)
        risk_flags.append("⚠️ Mint authority активна")
    else:
        confidence += 8; green_flags.append("✅ Mint authority сожжена")
    if freeze_auth:
        confidence -= 12; rug_score += 15
        risk_flags.append("⚠️ Freeze authority активна")
    else:
        confidence += 5; green_flags.append("✅ Freeze authority сожжена")

    # ── holder concentration — weighted ──
    h_w = w.get("holder_top1", 0.08)
    if top1_pct > 50:
        d = int(25 * (h_w / 0.08))
        confidence -= d; rug_score += 35
        risk_flags.append("🐋 Топ-1 — критическая концентрация")
    elif top1_pct > 30:
        confidence -= 12; rug_score += 20
        risk_flags.append("🐋 Топ-1 — высокая концентрация")
    elif 0 < top1_pct < 10:
        confidence += 15
        green_flags.append("✅ Отличное распределение — топ-1 < 10%")
    elif 0 < top1_pct < 15:
        confidence += 10
    if top5_pct > 70:
        confidence -= 8; rug_score += 10
        risk_flags.append("⚠️ Топ-5 держат большую долю")

    # ── bundle / sniper ──
    for bf in bundle_flags:
        confidence -= 8; rug_score += 8; risk_flags.append(bf)

    # ── liquidity — weighted ──
    liq_w = w.get("liquidity", 0.12)
    if liquidity < 500:
        confidence -= int(15 * liq_w / 0.12)
        risk_flags.append("⚠️ Критически низкая ликвидность")
    elif liquidity > 5_000:
        confidence += int(15 * liq_w / 0.12)
        green_flags.append("✅ Высокая ликвидность")
    elif liquidity > 2_000:
        confidence += int(10 * liq_w / 0.12)
    elif liquidity > 1_000:
        confidence += int(6 * liq_w / 0.12)

    # ── volume ratio (use 15m for accuracy) — weighted ──
    vol_w = w.get("volume_ratio", 0.10)
    if mcap > 0 and volume_15m > 0:
        vol_ratio = volume_15m / mcap
        if vol_ratio > 2:
            confidence += int(10 * vol_w / 0.10)
            green_flags.append("✅ Высокий объём / MCap")
        elif vol_ratio > 0.5:
            confidence += int(5 * vol_w / 0.10)
        elif vol_ratio < 0.05:
            confidence -= int(8 * vol_w / 0.10)
            risk_flags.append("⚠️ Низкий объём относительно MCap")
    elif age_min > 5 and volume_15m == 0:
        confidence -= 12
        risk_flags.append("⚠️ Нет объёма за 15 мин")

    # ── volume spike: объём за 5м >> 3× от среднего за 15м → сильный сигнал ──
    if volume_5m > 0 and volume_15m > 0:
        avg_5m_rate = volume_15m / 3          # средний объём за 5м-интервал
        if volume_5m > avg_5m_rate * 4:
            confidence += 15
            green_flags.append("🚀 Объём за 5м в 4× выше среднего — ускорение!")
        elif volume_5m > avg_5m_rate * 2:
            confidence += 8
            green_flags.append("📈 Объём за 5м в 2× выше среднего")

    # ── age ──
    if age_min < 1:
        confidence -= 5   # слишком рано, данных почти нет
    elif age_min > 8:
        confidence -= 5   # слишком поздно, пик уже мог пройти
    elif age_min > 3:
        confidence += 7

    # ── momentum — weighted ──
    mom_w = w.get("momentum", 0.15)
    if "UP" in momentum:
        confidence += int(12 * mom_w / 0.15)
        green_flags.append("✅ Цена растёт")
    elif "DOWN" in momentum:
        confidence -= int(12 * mom_w / 0.15)
        # (флаг уже добавлен выше если age > 3)

    # ── rugcheck score ──
    rc_score = rc.get("rugcheck_score", 0)
    if rc_score > 80:
        confidence += 18; rug_score = max(0, rug_score - 10)
        green_flags.append("✅ RugCheck: чистый токен")
    elif rc_score > 60:
        confidence += 8
    elif 0 < rc_score < 40:
        confidence -= 18; rug_score += 12
        risk_flags.append("🔴 RugCheck отмечает риски")
    elif 0 < rc_score < 60:
        confidence -= 8; rug_score += 5

    # ── creator score — weighted (heaviest factor) ──
    cr_w = w.get("creator_score", 0.25)
    if creator_score >= 70:
        confidence += int(12 * cr_w / 0.25)
        green_flags.append("✅ Надёжный создатель")
    elif creator_score <= 20:
        confidence -= int(20 * cr_w / 0.25); rug_score += 20
        risk_flags.append("💀 Плохая история создателя")
    elif creator_score <= 40:
        confidence -= int(15 * cr_w / 0.25); rug_score += 15
        risk_flags.append("⚠️ Слабая история создателя")
    elif creator_score >= 50:
        confidence += int(5 * cr_w / 0.25)

    # ── multi-dex migration (bullish) ──
    if on_raydium:
        confidence += 14
        green_flags.append("🟢 Ликвидность на Raydium — бычий сигнал")
    if on_meteora:
        confidence += 8
        green_flags.append("🟢 Ликвидность на Meteora")
    if pair_count >= 3:
        confidence += 5
        green_flags.append(f"✅ {pair_count} торговых пар")

    return (
        max(0, min(100, confidence)),
        max(0, min(100, rug_score)),
        risk_flags,
        green_flags,
    )


# ─── DATACLASS ────────────────────────────────────────────────────────────────

@dataclass
class TokenAnalysis:
    ca:            str
    symbol:        str
    name:          str
    creator:       str
    age_min:       float
    mcap:          float
    price:         float
    ath:           float
    liquidity:     float
    volume_15m:    float
    volume_5m:     float
    dex_url:       str
    confidence:    int
    rug_score:     int
    risk_flags:    list[str] = field(default_factory=list)
    green_flags:   list[str] = field(default_factory=list)
    is_honeypot:   bool  = False
    mint_auth:     bool  = True
    freeze_auth:   bool  = True
    top1_pct:      float = 0.0
    top5_pct:      float = 0.0
    top10_pct:     float = 0.0
    holders_count: int   = 0
    momentum:      str   = "⏳"
    change_1m:     float = 0.0
    change_3m:     float = 0.0
    change_5m:     float = 0.0
    creator_score: int   = 50
    rugcheck_score: int  = 0
    sell_tax:      float = 0.0
    needs_retry:   bool  = False
    on_raydium:    bool  = False
    pair_count:    int   = 0
    volume_3m:     float = 0.0   # kept for x_calculator / telegram compat


async def analyze_token(
    token:     dict,
    session:   aiohttp.ClientSession,
    blacklist: set[str],
) -> "TokenAnalysis | None":
    ca         = token["ca"]
    symbol     = token["symbol"]
    name       = token["name"]
    creator    = token.get("creator", "")
    ts_ms      = token.get("timestamp_ms", int(time.time() * 1000))
    age_min    = (time.time() * 1000 - ts_ms) / 60_000
    sol_amount = float(token.get("sol_amount", 0.0))  # сколько SOL залил создатель при запуске

    if ca in blacklist:
        return None
    if ca in config.KNOWN_MAJOR_TOKENS:
        return None

    # ── ВСЕ 5 API-ВЫЗОВОВ ПАРАЛЛЕЛЬНО ─────────────────────────────────────────
    # Раньше: Dexscreener → потом gather(mint, holders, rugcheck, creator)
    # Теперь: gather(dex, mint, holders, rugcheck, creator) одновременно.
    # Экономия: время Dexscreener больше не добавляется к времени gather.
    results = await asyncio.gather(
        get_dex_data(session, ca),
        get_mint_info(session, ca),
        get_largest_accounts(session, ca),
        check_rugcheck(session, ca),
        get_creator_score(creator),
        return_exceptions=True,
    )

    dex         = results[0] if not isinstance(results[0], Exception) else {}
    mint_info   = results[1] if not isinstance(results[1], Exception) else {}
    holder_info = results[2] if not isinstance(results[2], Exception) else {}
    rc_raw      = results[3] if not isinstance(results[3], Exception) else {}
    creator_res = results[4] if not isinstance(results[4], Exception) else {"score": 50}

    # ── РАННЯЯ ПРОВЕРКА MCAP — после gather (нет смысла было делать это раньше) ─
    early_mcap = dex.get("mcap", 0.0) or 0.0
    if early_mcap > config.MAX_SAFE_MCAP_USD:
        return None

    rc             = parse_rugcheck(rc_raw)
    creator_score  = creator_res.get("score", 50) if isinstance(creator_res, dict) else 50

    price     = dex.get("price",      0.0)
    mcap      = dex.get("mcap",       0.0)
    liquidity = dex.get("liquidity",  0.0)
    vol_5m    = dex.get("volume_5m",  0.0)
    vol_15m   = dex.get("volume_15m", 0.0)
    on_raydium = dex.get("on_raydium", False)
    on_meteora = dex.get("on_meteora", False)
    pair_count = dex.get("pair_count", 0)

    if price > 0:
        record_price(ca, price)
    ath = get_ath(ca)

    _retry = needs_retry(dex)

    momentum, c1m, c3m, c5m = get_momentum(ca)

    if price > 0 and is_cooled_down(ca, price):
        return None

    top1  = holder_info.get("top1_pct",  0.0)
    top5  = holder_info.get("top5_pct",  0.0)
    top10 = holder_info.get("top10_pct", 0.0)
    amts  = holder_info.get("amounts",   [])
    total_s = holder_info.get("total_supply", 1)

    bundle_count, bundle_flags = detect_bundles(amts, total_s)

    confidence, rug_score, risk_flags, green_flags = calculate_scores(
        age_min      = age_min,
        mcap         = mcap,
        liquidity    = liquidity,
        volume_15m   = vol_15m,
        volume_5m    = vol_5m,
        mint_auth    = mint_info.get("mint_authority",   True),
        freeze_auth  = mint_info.get("freeze_authority", True),
        top1_pct     = top1,
        top5_pct     = top5,
        top10_pct    = top10,
        rc           = rc,
        momentum     = momentum,
        creator_score = creator_score,
        bundle_flags = bundle_flags,
        bundle_count = bundle_count,
        on_raydium   = on_raydium,
        on_meteora   = on_meteora,
        pair_count   = pair_count,
        ca           = ca,
        blacklist    = blacklist,
        holders_count = holder_info.get("holders_count", 0),
    )

    c_stats  = creator_res if isinstance(creator_res, dict) else None
    c_risks, c_greens = creator_flags(c_stats, creator_score)
    risk_flags  = c_risks  + risk_flags
    green_flags = c_greens + green_flags

    # ── SOL_AMOUNT БУСТ ────────────────────────────────────────────────────────
    # Сколько SOL создатель залил при запуске токена — это реальный skin-in-the-game.
    # Если создатель вложил много — у него есть мотивация не ругать сразу.
    # Данные приходят прямо из WebSocket payload (поле solAmount).
    if sol_amount >= 5.0:
        confidence = min(100, confidence + 12)
        green_flags.append(f"💰 Создатель залил {sol_amount:.1f} SOL — серьёзный старт")
    elif sol_amount >= 2.0:
        confidence = min(100, confidence + 7)
        green_flags.append(f"💰 Старт: {sol_amount:.1f} SOL")
    elif sol_amount >= 0.5:
        confidence = min(100, confidence + 3)

    return TokenAnalysis(
        ca             = ca,
        symbol         = symbol,
        name           = name,
        creator        = creator,
        age_min        = age_min,
        mcap           = mcap,
        price          = price,
        ath            = ath,
        liquidity      = liquidity,
        volume_15m     = vol_15m,
        volume_5m      = vol_5m,
        volume_3m      = vol_15m,   # alias for compat
        dex_url        = dex.get("dex_url", ""),
        confidence     = confidence,
        rug_score      = rug_score,
        risk_flags     = risk_flags,
        green_flags    = green_flags,
        is_honeypot    = rc["is_honeypot"],
        mint_auth      = mint_info.get("mint_authority",   True),
        freeze_auth    = mint_info.get("freeze_authority", True),
        top1_pct       = top1,
        top5_pct       = top5,
        top10_pct      = top10,
        holders_count  = holder_info.get("holders_count", 0),
        momentum       = momentum,
        change_1m      = c1m,
        change_3m      = c3m,
        change_5m      = c5m,
        creator_score  = creator_score,
        rugcheck_score = rc.get("rugcheck_score", 0),
        sell_tax       = rc.get("sell_tax", 0.0),
        needs_retry    = _retry,
        on_raydium     = on_raydium,
        pair_count     = pair_count,
    )
