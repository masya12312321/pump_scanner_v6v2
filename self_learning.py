"""
self_learning.py v6 — uses db_pool, fixed global declaration.
"""
import asyncio
import logging
import math
import time

import db_pool as pool

log = logging.getLogger("SelfLearning")

DEFAULT_WEIGHTS = {
    "creator_score":    0.25,
    "rug_score":        0.20,
    "momentum":         0.15,
    "liquidity":        0.12,
    "volume_ratio":     0.10,
    "holder_top1":      0.08,
    "mint_authority":   0.05,
    "freeze_authority": 0.05,
}

TRAIN_INTERVAL_SEC = 21_600   # каждые 6 часов (было 24ч)
MIN_SAMPLES        = 20
LEARNING_RATE      = 0.05

POSITIVE_OUTCOMES = {"x2", "x5", "x10"}
NEGATIVE_OUTCOMES = {"rug", "dead"}

_weights: dict[str, float] = DEFAULT_WEIGHTS.copy()


def _sigmoid(x: float) -> float:
    try:
        return 1.0 / (1.0 + math.exp(-x))
    except OverflowError:
        return 0.0 if x < 0 else 1.0


def _predict(sample: dict, weights: dict[str, float]) -> float:
    score = 0.0
    score += weights.get("creator_score", 0) * (sample.get("creator_score", 50) / 100)
    score += weights.get("rug_score",     0) * ((100 - sample.get("rug_score", 0)) / 100)
    score += weights.get("volume_ratio",  0) * min(sample.get("volume_ratio", 0), 5) / 5
    score += weights.get("liquidity",     0) * min(sample.get("liquidity", 0), 10_000) / 10_000
    return _sigmoid((score - 0.5) * 8)


def _train(samples: list[dict], weights: dict[str, float]) -> dict[str, float]:
    global _weights
    new_w   = weights.copy()
    labeled = [s for s in samples if s.get("result") in POSITIVE_OUTCOMES | NEGATIVE_OUTCOMES]
    if len(labeled) < MIN_SAMPLES:
        log.info(f"SelfLearning: {len(labeled)} samples — skip train")
        return new_w
    log.info(f"SelfLearning: training on {len(labeled)} samples")
    for s in labeled:
        y     = 1.0 if s.get("result") in POSITIVE_OUTCOMES else 0.0
        p     = _predict(s, new_w)
        error = p - y
        new_w["creator_score"] -= LEARNING_RATE * error * (s.get("creator_score", 50) / 100)
        new_w["rug_score"]     -= LEARNING_RATE * error * ((100 - s.get("rug_score", 0)) / 100)
        new_w["volume_ratio"]  -= LEARNING_RATE * error * min(s.get("volume_ratio", 0), 5) / 5
        new_w["liquidity"]     -= LEARNING_RATE * error * min(s.get("liquidity", 0), 10_000) / 10_000
    total = sum(abs(v) for v in new_w.values()) or 1.0
    new_w = {k: max(0.01, v / total) for k, v in new_w.items()}
    changes = sorted(
        {k: round(new_w[k] - weights[k], 4) for k in new_w}.items(),
        key=lambda x: abs(x[1]), reverse=True
    )[:3]
    log.info(f"SelfLearning: top shifts — {changes}")
    return new_w


async def load_weights() -> None:
    global _weights
    rows = await pool.fetchall("SELECT key, value FROM learned_weights")
    if rows:
        _weights = {r[0]: r[1] for r in rows}
    log.info(f"Weights loaded: {_weights}")


async def _save_weights() -> None:
    now = int(time.time())
    await pool.executemany(
        "INSERT OR REPLACE INTO learned_weights (key, value, updated_at) VALUES (?, ?, ?)",
        [(k, v, now) for k, v in _weights.items()]
    )


async def save_sample(
    token: str, confidence: int, creator_score: int,
    rug_score: int, volume_ratio: float, liquidity: float, holders: int,
) -> None:
    await pool.execute(
        "INSERT INTO learning_samples "
        "(token, confidence, creator_score, rug_score, volume_ratio, liquidity, holders, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (token, confidence, creator_score, rug_score,
         volume_ratio, liquidity, holders, int(time.time()))
    )


def get_dynamic_weights() -> dict[str, float]:
    return _weights.copy()


async def training_loop() -> None:
    global _weights
    await load_weights()
    while True:
        try:
            await asyncio.sleep(TRAIN_INTERVAL_SEC)
            log.info("SelfLearning: daily training")
            rows    = await pool.fetchall(
                "SELECT * FROM learning_samples ORDER BY created_at DESC LIMIT 5000"
            )
            samples = [dict(r) for r in rows]
            new_w   = _train(samples, _weights)
            _weights = new_w
            await _save_weights()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log.error(f"SelfLearning: {exc}")
