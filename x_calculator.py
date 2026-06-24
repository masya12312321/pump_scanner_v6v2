"""
x_calculator.py — расчёт потенциального X и тайминга выхода.

Логика:
  1. Берём текущий MCap токена
  2. По историческим паттернам pump.fun считаем вероятность каждого X
  3. Считаем оптимальное время выхода на основе momentum и возраста
  4. Выдаём рекомендацию: "продавай на Xм минуте" или "цель X2/X5/X10"
"""

import logging
from dataclasses import dataclass

log = logging.getLogger("XCalc")


# ─── ИСТОРИЧЕСКИЕ ДАННЫЕ pump.fun (эмпирика) ──────────────────────────────────
# Источник: анализ тысяч токенов pump.fun
# MCap диапазоны → медианное время достижения пика → % токенов достигших X

MCAP_TIERS = [
    # (max_mcap, tier_name, median_peak_minutes, x2_prob, x5_prob, x10_prob, x50_prob, x100_prob)
    (2_000,    "Nano",   2.5,  0.45, 0.20, 0.10, 0.03, 0.01),
    (5_000,    "Micro",  3.5,  0.40, 0.18, 0.08, 0.02, 0.008),
    (10_000,   "Small",  5.0,  0.35, 0.14, 0.06, 0.015, 0.005),
    (25_000,   "Mid",    7.0,  0.28, 0.10, 0.04, 0.010, 0.003),
    (50_000,   "Large",  10.0, 0.20, 0.07, 0.02, 0.005, 0.001),
    (float("inf"), "Huge", 15.0, 0.12, 0.04, 0.01, 0.002, 0.0005),
]

# Множители momentum на вероятности
MOMENTUM_MULT = {
    "📈 UP":    1.4,
    "➡️ FLAT": 1.0,
    "📉 DOWN":  0.5,
    "⏳ НАКАПЛИВАЕМ": 1.1,
    "⏳":       1.1,
}

# Множители confidence на вероятности
def confidence_mult(conf: int) -> float:
    if conf >= 70: return 1.3
    if conf >= 50: return 1.1
    if conf >= 35: return 0.9
    return 0.7

# Множители creator score
def creator_mult(score: int) -> float:
    if score >= 70: return 1.2
    if score >= 50: return 1.0
    if score >= 30: return 0.85
    return 0.7

# Множители rug score (инвертированный — высокий rug = меньше шансов)
def rug_mult(rug: int) -> float:
    if rug >= 70: return 0.4
    if rug >= 50: return 0.65
    if rug >= 30: return 0.85
    return 1.0


@dataclass
class XPrediction:
    tier:             str          # Nano / Micro / Small / Mid
    current_mcap:     float
    median_peak_min:  float        # медианное время пика в минутах
    x2_prob:          float        # вероятность x2 (0-1)
    x5_prob:          float
    x10_prob:         float
    x50_prob:         float
    x100_prob:        float
    best_exit_window: str          # "2-4 мин", "3-7 мин" и т.д.
    exit_strategy:    str          # текстовая рекомендация
    peak_mcap_est:    float        # ожидаемый пик MCap
    risk_reward:      str          # "1:3", "1:8" и т.д.
    grade:            str          # A / B / C / D


def calculate_x(
    mcap:          float,
    confidence:    int,
    rug_score:     int,
    momentum:      str,
    age_min:       float,
    creator_score: int,
    liquidity:     float,
    volume_3m:     float,
) -> XPrediction:
    """
    Главная функция расчёта X-потенциала.
    """
    if mcap <= 0:
        mcap = 1_000   # fallback если Dexscreener ещё не видит

    # ── найти тир ──
    tier_data = MCAP_TIERS[-1]
    for max_mcap, name, med_peak, p2, p5, p10, p50, p100 in MCAP_TIERS:
        if mcap <= max_mcap:
            tier_data = (max_mcap, name, med_peak, p2, p5, p10, p50, p100)
            break

    _, tier_name, med_peak_min, base_p2, base_p5, base_p10, base_p50, base_p100 = tier_data

    # ── множители ──
    m_mom  = MOMENTUM_MULT.get(momentum, 1.0)
    m_conf = confidence_mult(confidence)
    m_rug  = rug_mult(rug_score)
    m_cre  = creator_mult(creator_score)

    # ликвидность — дополнительный множитель
    if liquidity > 2000:   m_liq = 1.1
    elif liquidity > 500:  m_liq = 1.0
    else:                  m_liq = 0.85

    # объём/MCap ratio
    if mcap > 0 and volume_3m > 0:
        vol_ratio = volume_3m / mcap
        if vol_ratio > 3:    m_vol = 1.3
        elif vol_ratio > 1:  m_vol = 1.15
        elif vol_ratio > 0.3: m_vol = 1.0
        else:                m_vol = 0.8
    else:
        m_vol = 1.0

    combined = m_mom * m_conf * m_rug * m_cre * m_liq * m_vol

    # ── итоговые вероятности (cap 95%) ──
    p2   = min(0.95, base_p2   * combined)
    p5   = min(0.90, base_p5   * combined)
    p10  = min(0.80, base_p10  * combined)
    p50  = min(0.60, base_p50  * combined)
    p100 = min(0.50, base_p100 * combined)

    # ── ожидаемый пик MCap ──
    # взвешенное среднее по X-таргетам
    expected_x = (
        1 * (1 - p2) +
        2 * (p2 - p5) +
        5 * (p5 - p10) +
        10 * (p10 - p50) +
        50 * (p50 - p100) +
        100 * p100
    )
    expected_x = max(1.0, expected_x)
    peak_mcap_est = mcap * expected_x

    # ── окно выхода ──
    # Корректируем медианное время на возраст токена
    # Если токен уже 3 минуты живёт — пик ближе
    adjusted_peak = max(0.5, med_peak_min - age_min * 0.3)

    if momentum == "📈 UP":
        # цена уже растёт — выходи быстрее
        exit_start = max(0.5, adjusted_peak * 0.5)
        exit_end   = adjusted_peak * 1.2
    elif momentum == "📉 DOWN":
        # качает вниз — либо уже прошло, либо нет смысла ждать
        exit_start = 0.5
        exit_end   = 1.5
    else:
        exit_start = adjusted_peak * 0.7
        exit_end   = adjusted_peak * 1.5

    # округляем до 0.5
    exit_start = round(exit_start * 2) / 2
    exit_end   = round(exit_end   * 2) / 2
    if exit_end <= exit_start:
        exit_end = exit_start + 1.0

    best_exit_window = f"{exit_start:.1f}–{exit_end:.1f} мин"

    # ── стратегия выхода ──
    if p10 > 0.15:
        strategy = (
            f"Агрессивный: продай 50% на x2, держи остаток до x5–x10. "
            f"Стоп-лосс на -30% от входа."
        )
    elif p5 > 0.12:
        strategy = (
            f"Умеренный: продай 70% на x2–x3, остаток на x5. "
            f"Стоп-лосс на -25%."
        )
    elif p2 > 0.20:
        strategy = (
            f"Консервативный: фиксируй на x1.5–x2, не жди больше. "
            f"Стоп-лосс на -20%."
        )
    else:
        strategy = (
            f"Высокий риск: быстрый скальп x1.3–x1.5, "
            f"жёсткий стоп-лосс -15%."
        )

    # ── risk/reward ──
    if p2 > 0:
        rr_ratio = round((expected_x - 1) / 0.5, 1)   # риск = -50% (стоп)
        risk_reward = f"1:{rr_ratio}"
    else:
        risk_reward = "N/A"

    # ── грейд ──
    score = (
        p2  * 20 +
        p5  * 25 +
        p10 * 30 +
        p50 * 15 +
        p100 * 10
    )
    if score > 15:   grade = "A"
    elif score > 8:  grade = "B"
    elif score > 4:  grade = "C"
    else:            grade = "D"

    return XPrediction(
        tier             = tier_name,
        current_mcap     = mcap,
        median_peak_min  = med_peak_min,
        x2_prob          = p2,
        x5_prob          = p5,
        x10_prob         = p10,
        x50_prob         = p50,
        x100_prob        = p100,
        best_exit_window = best_exit_window,
        exit_strategy    = strategy,
        peak_mcap_est    = peak_mcap_est,
        risk_reward      = risk_reward,
        grade            = grade,
    )


def _fmt_usd(v: float) -> str:
    """Компактный формат — локальная копия, чтобы не тянуть зависимость на telegram_bot.py."""
    v = float(v)
    if v >= 1_000_000_000:
        return f"${v/1_000_000_000:.1f}B"
    if v >= 1_000_000:
        return f"${v/1_000_000:.1f}M"
    if v >= 1_000:
        return f"${v/1_000:.1f}k"
    return f"${v:,.0f}"


def format_prob(p: float) -> str:
    """Форматирует вероятность как % с цветовым emoji."""
    pct = p * 100
    if pct >= 30:  emoji = "🟢"
    elif pct >= 10: emoji = "🟡"
    elif pct >= 3:  emoji = "🟠"
    else:           emoji = "🔴"
    return f"{emoji} {pct:.1f}%"


def format_x_block(pred: XPrediction) -> str:
    """
    Прогноз и стратегия выхода — финальный блок сообщения.
    x50/x100 не выводятся: при долях процента это визуальный шум,
    x2/x5/x10 достаточно чтобы оценить потенциал.
    """
    grade_emoji = {"A": "🏆", "B": "🥈", "C": "🥉", "D": "💀"}.get(pred.grade, "❓")

    return (
        f"📊 <b>ПРОГНОЗ</b> {grade_emoji} Grade: <b>{pred.grade}</b>\n\n"
        f"🎯 Ожидаемый пик MCap: <b>{_fmt_usd(pred.peak_mcap_est)}</b>\n"
        f"⚖️ Risk/Reward: <b>{pred.risk_reward}</b>\n\n"
        f"🎲 <b>Вероятности:</b>\n"
        f"  x2:  {format_prob(pred.x2_prob)}\n"
        f"  x5:  {format_prob(pred.x5_prob)}\n"
        f"  x10: {format_prob(pred.x10_prob)}\n\n"
        f"⏱ <b>Окно выхода:</b> <b>{pred.best_exit_window}</b>\n\n"
        f"📋 <b>Стратегия:</b>\n"
        f"  {pred.exit_strategy}"
    )
