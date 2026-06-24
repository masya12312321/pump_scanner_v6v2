"""
creator_scoring.py v6 — uses db_pool singleton, no per-call connect.
"""
import logging
import time

import db_pool as pool

log = logging.getLogger("CreatorScore")


def _compute_score(s: dict) -> int:
    total    = max(s.get("tokens_created", 0), 1)
    rugged   = s.get("tokens_rugged", 0)
    x2       = s.get("tokens_x2",     0)
    x5       = s.get("tokens_x5",     0)
    x10      = s.get("tokens_x10",    0)
    rug_rate = rugged / total
    score    = 50

    if x10 > 2:   score += 15
    elif x10 > 0: score += 7
    if x5  > 5:   score += 10
    elif x5 > 2:  score += 5
    if x2  > 5:   score += 5
    elif x2 > 0:  score += 2

    if rug_rate > 0.9:   score -= 40
    elif rug_rate > 0.7: score -= 20
    elif rug_rate > 0.5: score -= 12
    elif rug_rate > 0.3: score -= 6

    if total < 3:
        score = max(score, 35)

    return max(0, min(100, score))


async def upsert_creator(wallet: str) -> None:
    if not wallet:
        return
    await pool.execute(
        "INSERT OR IGNORE INTO creator_stats (creator, last_update) VALUES (?, ?)",
        (wallet, int(time.time()))
    )
    await pool.execute(
        "UPDATE creator_stats SET tokens_created=tokens_created+1, last_update=? "
        "WHERE creator=?",
        (int(time.time()), wallet)
    )


async def record_outcome(wallet: str, outcome: str, lifetime_minutes: float = 0.0) -> None:
    if not wallet:
        return
    field_map = {"rug": "tokens_rugged", "x2": "tokens_x2",
                 "x5": "tokens_x5",      "x10": "tokens_x10"}
    field = field_map.get(outcome)
    if field:
        await pool.execute(
            f"UPDATE creator_stats SET {field}={field}+1 WHERE creator=?", (wallet,)
        )
    if lifetime_minutes > 0:
        await pool.execute(
            """UPDATE creator_stats
               SET avg_lifetime_minutes =
                   (avg_lifetime_minutes * tokens_created + ?) / (tokens_created + 1)
               WHERE creator=?""",
            (lifetime_minutes, wallet)
        )
    # recompute and persist score
    row = await pool.fetchone("SELECT * FROM creator_stats WHERE creator=?", (wallet,))
    if row:
        score = _compute_score(dict(row))
        await pool.execute(
            "UPDATE creator_stats SET score=?, last_update=? WHERE creator=?",
            (score, int(time.time()), wallet)
        )


async def get_creator_score(wallet: str) -> dict:
    if not wallet:
        return {"score": 50, "tokens_created": 0, "rug_rate": 0.0}
    await upsert_creator(wallet)
    row = await pool.fetchone("SELECT * FROM creator_stats WHERE creator=?", (wallet,))
    if not row:
        return {"score": 50, "tokens_created": 0, "rug_rate": 0.0}
    s        = dict(row)
    total    = max(s["tokens_created"], 1)
    rug_rate = s["tokens_rugged"] / total
    score    = _compute_score(s)
    return {
        "score":          score,
        "tokens_created": s["tokens_created"],
        "tokens_alerted": s["tokens_alerted"],
        "tokens_x10":     s["tokens_x10"],
        "rug_rate":       round(rug_rate, 3),
    }


def creator_flags(stats: dict | None, score: int) -> tuple[list[str], list[str]]:
    risks, greens = [], []
    if stats is None:
        risks.append("❓ Кошелёк создателя — нет истории")
        return risks, greens
    total    = max(stats.get("tokens_created", 0), 1)
    rugged   = stats.get("tokens_rugged", 0)
    x10      = stats.get("tokens_x10",    0)
    x5       = stats.get("tokens_x5",     0)
    rug_rate = rugged / total
    if rug_rate > 0.7:
        risks.append(f"💀 Создатель: {rugged}/{total} слито ({rug_rate*100:.0f}%)")
    elif rug_rate > 0.3:
        risks.append(f"⚠️ Создатель: rug-история {rug_rate*100:.0f}%")
    if x10 > 2:
        greens.append(f"🚀 Создатель: {x10} токенов x10 из {total}")
    elif x10 > 0:
        greens.append(f"✅ Создатель: есть x10 ({x10} раз)")
    if x5 > 5:
        greens.append(f"✅ Создатель: {x5} токенов x5")
    if score >= 70:
        pass   # уже отражено явным числом Creator Score в сообщении
    elif score <= 30:
        risks.append("🔴 Слабая репутация создателя")
    return risks, greens


def creator_confidence_delta(score: int) -> tuple[int, list[str], list[str]]:
    if score >= 80: return +15, [], [f"🏆 Creator Score {score}/100"]
    if score >= 65: return  +8, [], [f"✅ Creator Score {score}/100"]
    if score >= 50: return  +3, [], []
    if score >= 35: return  -8, [f"⚠️ Creator Score {score}/100"], []
    if score >= 20: return -20, [f"🔴 Creator Score {score}/100 — rug история"], []
    return -35, [f"💀 Creator Score {score}/100 — серийный rugger"], []


async def get_top_creators(limit: int = 10) -> list[dict]:
    rows = await pool.fetchall(
        """SELECT *, CAST(tokens_rugged AS REAL)/MAX(tokens_created,1)*100 AS rug_pct
           FROM creator_stats ORDER BY tokens_alerted DESC LIMIT ?""",
        (limit,)
    )
    return [dict(r) for r in rows]
