"""
database.py v6 — All DB calls go through db_pool singleton.
No per-call aiosqlite.connect() — no file-lock contention.
"""
import time
import logging

import db_pool as pool

log = logging.getLogger("DB")


async def init_db() -> None:
    await pool.open_pool()
    await pool.executescript("""
        CREATE TABLE IF NOT EXISTS seen_tokens (
            ca          TEXT PRIMARY KEY,
            symbol      TEXT NOT NULL DEFAULT '???',
            first_seen  INTEGER NOT NULL,
            status      TEXT NOT NULL DEFAULT 'pending',
            retry_count INTEGER NOT NULL DEFAULT 0,
            next_retry  INTEGER,
            initial_mcap REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS blacklist (
            ca      TEXT PRIMARY KEY,
            reason  TEXT NOT NULL,
            added   INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS creator_stats (
            creator              TEXT PRIMARY KEY,
            tokens_created       INTEGER NOT NULL DEFAULT 0,
            tokens_alerted       INTEGER NOT NULL DEFAULT 0,
            tokens_rugged        INTEGER NOT NULL DEFAULT 0,
            tokens_x2            INTEGER NOT NULL DEFAULT 0,
            tokens_x5            INTEGER NOT NULL DEFAULT 0,
            tokens_x10           INTEGER NOT NULL DEFAULT 0,
            avg_lifetime_minutes REAL    NOT NULL DEFAULT 0.0,
            score                INTEGER NOT NULL DEFAULT 50,
            last_update          INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS alerts (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            ca                TEXT NOT NULL,
            symbol            TEXT NOT NULL,
            creator           TEXT NOT NULL DEFAULT '',
            confidence        INTEGER NOT NULL,
            rug_score         INTEGER NOT NULL,
            mcap              REAL NOT NULL,
            predicted_peak_mcap REAL NOT NULL DEFAULT 0,
            forecast_hit      INTEGER NOT NULL DEFAULT 0,
            outcome           TEXT DEFAULT NULL,
            outcome_mcap      REAL DEFAULT NULL,
            max_x_hit         REAL NOT NULL DEFAULT 0,
            sent_at           INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS learning_samples (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            token         TEXT NOT NULL,
            confidence    INTEGER NOT NULL,
            creator_score INTEGER NOT NULL DEFAULT 50,
            rug_score     INTEGER NOT NULL DEFAULT 0,
            volume_ratio  REAL NOT NULL DEFAULT 0,
            liquidity     REAL NOT NULL DEFAULT 0,
            holders       INTEGER NOT NULL DEFAULT 0,
            result        TEXT,
            created_at    INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS learned_weights (
            key        TEXT PRIMARY KEY,
            value      REAL NOT NULL,
            updated_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS trading_settings (
            id                    INTEGER PRIMARY KEY CHECK (id = 1),
            paper_mode            INTEGER NOT NULL DEFAULT 1,
            autotrade_enabled     INTEGER NOT NULL DEFAULT 0,
            position_size_sol     REAL    NOT NULL DEFAULT 0.05,
            take_profit_pct       REAL    NOT NULL DEFAULT 100.0,
            stop_loss_pct         REAL    NOT NULL DEFAULT 30.0,
            max_positions         INTEGER NOT NULL DEFAULT 5,
            min_confidence_trade  INTEGER NOT NULL DEFAULT 55,
            daily_loss_limit_sol  REAL    NOT NULL DEFAULT 1.0,
            paper_balance_sol     REAL    NOT NULL DEFAULT 5.0,
            updated_at            INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS positions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ca            TEXT NOT NULL,
            symbol        TEXT NOT NULL,
            mode          TEXT NOT NULL DEFAULT 'paper',
            status        TEXT NOT NULL DEFAULT 'open',
            sol_amount    REAL NOT NULL,
            token_amount  REAL NOT NULL DEFAULT 0,
            entry_price   REAL NOT NULL,
            entry_mcap    REAL NOT NULL DEFAULT 0,
            tp_price      REAL NOT NULL DEFAULT 0,
            sl_price      REAL NOT NULL DEFAULT 0,
            peak_price    REAL NOT NULL DEFAULT 0,
            exit_price    REAL,
            exit_reason   TEXT,
            pnl_sol       REAL,
            pnl_pct       REAL,
            buy_tx        TEXT,
            sell_tx       TEXT,
            opened_at     INTEGER NOT NULL,
            closed_at     INTEGER
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_positions_open_ca ON positions(ca) WHERE status='open';
        CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
        CREATE INDEX IF NOT EXISTS idx_seen_status   ON seen_tokens(status);
        CREATE INDEX IF NOT EXISTS idx_seen_retry    ON seen_tokens(next_retry);
        CREATE INDEX IF NOT EXISTS idx_alerts_ca     ON alerts(ca);
        CREATE INDEX IF NOT EXISTS idx_alerts_sent   ON alerts(sent_at);
        CREATE INDEX IF NOT EXISTS idx_alerts_outcome ON alerts(outcome);
        CREATE INDEX IF NOT EXISTS idx_samples_result ON learning_samples(result);
    """)
    import config as _cfg
    await pool.execute(
        "INSERT OR IGNORE INTO trading_settings "
        "(id, paper_mode, autotrade_enabled, position_size_sol, take_profit_pct, "
        " stop_loss_pct, max_positions, min_confidence_trade, daily_loss_limit_sol, "
        " paper_balance_sol, updated_at) "
        "VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            1 if _cfg.DEFAULT_PAPER_MODE else 0,
            1 if _cfg.DEFAULT_AUTOTRADE_ENABLED else 0,
            _cfg.DEFAULT_POSITION_SIZE_SOL,
            _cfg.DEFAULT_TAKE_PROFIT_PCT,
            _cfg.DEFAULT_STOP_LOSS_PCT,
            _cfg.DEFAULT_MAX_POSITIONS,
            _cfg.DEFAULT_MIN_CONFIDENCE_TRADE,
            _cfg.DEFAULT_DAILY_LOSS_LIMIT_SOL,
            _cfg.DEFAULT_PAPER_BALANCE_SOL,
            int(time.time()),
        )
    )
    log.info("DB initialised (v6)")


# ─── SEEN TOKENS ──────────────────────────────────────────────────────────────

async def is_seen(ca: str) -> bool:
    row = await pool.fetchone("SELECT 1 FROM seen_tokens WHERE ca=?", (ca,))
    return row is not None


async def mark_pending(ca: str, symbol: str) -> None:
    await pool.execute(
        "INSERT OR IGNORE INTO seen_tokens (ca, symbol, first_seen, status) "
        "VALUES (?, ?, ?, 'pending')",
        (ca, symbol, int(time.time()))
    )


async def mark_alerted(ca: str, mcap: float = 0.0, creator: str = "") -> None:
    await pool.execute(
        "UPDATE seen_tokens SET status='alerted' WHERE ca=?", (ca,)
    )


async def mark_rejected(ca: str) -> None:
    await pool.execute(
        "UPDATE seen_tokens SET status='rejected' WHERE ca=?", (ca,)
    )


async def set_retry(ca: str, retry_count: int, next_retry: int) -> None:
    await pool.execute(
        "UPDATE seen_tokens SET retry_count=?, next_retry=?, status='pending' WHERE ca=?",
        (retry_count, next_retry, ca)
    )


async def get_due_retries(now: int) -> list[dict]:
    rows = await pool.fetchall(
        "SELECT ca, symbol, retry_count FROM seen_tokens "
        "WHERE status='pending' AND next_retry IS NOT NULL AND next_retry<=?",
        (now,)
    )
    return [dict(r) for r in rows]


# ─── BLACKLIST ────────────────────────────────────────────────────────────────

async def load_blacklist() -> set[str]:
    rows = await pool.fetchall("SELECT ca FROM blacklist")
    return {r[0] for r in rows}


async def add_blacklist(ca: str, reason: str) -> None:
    await pool.execute(
        "INSERT OR IGNORE INTO blacklist (ca, reason, added) VALUES (?, ?, ?)",
        (ca, reason, int(time.time()))
    )


async def get_blacklist_entries() -> list[dict]:
    rows = await pool.fetchall(
        "SELECT ca, reason, added FROM blacklist ORDER BY added DESC LIMIT 50"
    )
    return [dict(r) for r in rows]


# ─── CREATOR STATS ────────────────────────────────────────────────────────────

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


async def get_creator_raw(wallet: str) -> dict | None:
    row = await pool.fetchone(
        "SELECT * FROM creator_stats WHERE creator=?", (wallet,)
    )
    return dict(row) if row else None


async def increment_creator_field(wallet: str, field: str) -> None:
    allowed = {"tokens_alerted","tokens_rugged","tokens_x2","tokens_x5","tokens_x10"}
    if field not in allowed:
        return
    await pool.execute(
        f"UPDATE creator_stats SET {field}={field}+1 WHERE creator=?", (wallet,)
    )


async def update_creator_score(wallet: str, score: int) -> None:
    await pool.execute(
        "UPDATE creator_stats SET score=?, last_update=? WHERE creator=?",
        (score, int(time.time()), wallet)
    )


async def get_top_creators(limit: int = 10) -> list[dict]:
    rows = await pool.fetchall(
        """SELECT *, CAST(tokens_alerted AS REAL)/MAX(tokens_created,1)*100 AS hit_rate
           FROM creator_stats ORDER BY tokens_alerted DESC LIMIT ?""",
        (limit,)
    )
    return [dict(r) for r in rows]


# ─── ALERTS ───────────────────────────────────────────────────────────────────

async def save_alert(
    ca: str, symbol: str, confidence: int,
    rug_score: int, mcap: float, creator: str = "",
    predicted_peak_mcap: float = 0.0,
) -> None:
    await pool.execute(
        "INSERT INTO alerts "
        "(ca, symbol, creator, confidence, rug_score, mcap, predicted_peak_mcap, sent_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ca, symbol, creator, confidence, rug_score, mcap,
         predicted_peak_mcap, int(time.time()))
    )


async def update_alert_outcome(ca: str, outcome: str, outcome_mcap: float) -> None:
    await pool.execute(
        "UPDATE alerts SET outcome=?, outcome_mcap=? WHERE ca=? AND outcome IS NULL",
        (outcome, outcome_mcap, ca)
    )


async def get_alerts_for_outcome_check(min_age_sec: int = 1800) -> list[dict]:
    """Returns alerted tokens older than min_age_sec without outcome yet."""
    cutoff = int(time.time()) - min_age_sec
    rows = await pool.fetchall(
        "SELECT ca, symbol, creator, mcap, sent_at FROM alerts "
        "WHERE outcome IS NULL AND sent_at <= ? ORDER BY sent_at DESC LIMIT 200",
        (cutoff,)
    )
    return [dict(r) for r in rows]


async def get_active_alerts_for_milestones(max_age_sec: int = 7200) -> list[dict]:
    """
    Returns all alerts not yet marked dead/rug, younger than max_age_sec.
    Used by x_milestone_tracker for live X-multiplier monitoring.
    """
    cutoff = int(time.time()) - max_age_sec
    rows = await pool.fetchall(
        "SELECT ca, symbol, mcap, predicted_peak_mcap, forecast_hit, max_x_hit, sent_at "
        "FROM alerts WHERE (outcome IS NULL OR outcome NOT IN ('rug','dead')) "
        "AND sent_at >= ? ORDER BY sent_at DESC LIMIT 300",
        (cutoff,)
    )
    return [dict(r) for r in rows]


async def mark_forecast_hit(ca: str) -> None:
    """One-time flag: прогнозируемый пик достигнут, не слать повторно."""
    await pool.execute(
        "UPDATE alerts SET forecast_hit=1 "
        "WHERE ca=? AND sent_at=(SELECT MAX(sent_at) FROM alerts WHERE ca=?)",
        (ca, ca)
    )


async def update_max_x_hit(ca: str, x_value: float) -> None:
    await pool.execute(
        "UPDATE alerts SET max_x_hit=? WHERE ca=? AND sent_at=(SELECT MAX(sent_at) FROM alerts WHERE ca=?)",
        (x_value, ca, ca)
    )


async def get_recent_alerts(limit: int = 5) -> list[dict]:
    rows = await pool.fetchall(
        "SELECT ca, symbol, confidence, rug_score, mcap, outcome, outcome_mcap, sent_at "
        "FROM alerts ORDER BY sent_at DESC LIMIT ?",
        (limit,)
    )
    return [dict(r) for r in rows]


async def get_alert_count() -> int:
    row = await pool.fetchone("SELECT COUNT(*) FROM alerts")
    return row[0] if row else 0


async def get_seen_count() -> int:
    row = await pool.fetchone("SELECT COUNT(*) FROM seen_tokens")
    return row[0] if row else 0


# ─── LEARNING ─────────────────────────────────────────────────────────────────

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


async def update_sample_result(token: str, result: str) -> None:
    await pool.execute(
        "UPDATE learning_samples SET result=? WHERE token=? AND result IS NULL",
        (result, token)
    )


async def load_learning_samples(limit: int = 5000) -> list[dict]:
    rows = await pool.fetchall(
        "SELECT * FROM learning_samples ORDER BY created_at DESC LIMIT ?", (limit,)
    )
    return [dict(r) for r in rows]


async def load_learned_weights() -> dict[str, float]:
    rows = await pool.fetchall("SELECT key, value FROM learned_weights")
    return {r[0]: r[1] for r in rows} if rows else {}


async def save_learned_weights(weights: dict[str, float]) -> None:
    now = int(time.time())
    await pool.executemany(
        "INSERT OR REPLACE INTO learned_weights (key, value, updated_at) VALUES (?, ?, ?)",
        [(k, v, now) for k, v in weights.items()]
    )


# ─── TRADING: SETTINGS ─────────────────────────────────────────────────────────

_TRADING_FIELDS = {
    "paper_mode", "autotrade_enabled", "position_size_sol", "take_profit_pct",
    "stop_loss_pct", "max_positions", "min_confidence_trade",
    "daily_loss_limit_sol", "paper_balance_sol",
}


async def get_trading_settings() -> dict:
    row = await pool.fetchone("SELECT * FROM trading_settings WHERE id=1")
    d = dict(row)
    d["paper_mode"]        = bool(d["paper_mode"])
    d["autotrade_enabled"] = bool(d["autotrade_enabled"])
    return d


async def update_trading_settings(**kwargs) -> None:
    """Partial update — e.g. update_trading_settings(take_profit_pct=80.0)."""
    fields = {k: v for k, v in kwargs.items() if k in _TRADING_FIELDS}
    if not fields:
        return
    if "paper_mode" in fields:
        fields["paper_mode"] = 1 if fields["paper_mode"] else 0
    if "autotrade_enabled" in fields:
        fields["autotrade_enabled"] = 1 if fields["autotrade_enabled"] else 0
    set_sql = ", ".join(f"{k}=?" for k in fields)
    params = list(fields.values()) + [int(time.time())]
    await pool.execute(
        f"UPDATE trading_settings SET {set_sql}, updated_at=? WHERE id=1", tuple(params)
    )


# ─── TRADING: POSITIONS ────────────────────────────────────────────────────────

async def get_open_position(ca: str) -> dict | None:
    row = await pool.fetchone(
        "SELECT * FROM positions WHERE ca=? AND status='open'", (ca,)
    )
    return dict(row) if row else None


async def get_open_positions() -> list[dict]:
    rows = await pool.fetchall(
        "SELECT * FROM positions WHERE status='open' ORDER BY opened_at DESC"
    )
    return [dict(r) for r in rows]


async def count_open_positions() -> int:
    row = await pool.fetchone("SELECT COUNT(*) FROM positions WHERE status='open'")
    return row[0] if row else 0


async def open_position(
    ca: str, symbol: str, mode: str, sol_amount: float, token_amount: float,
    entry_price: float, entry_mcap: float, tp_price: float, sl_price: float,
    buy_tx: str | None = None,
) -> None:
    await pool.execute(
        "INSERT INTO positions "
        "(ca, symbol, mode, status, sol_amount, token_amount, entry_price, entry_mcap, "
        " tp_price, sl_price, peak_price, buy_tx, opened_at) "
        "VALUES (?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (ca, symbol, mode, sol_amount, token_amount, entry_price, entry_mcap,
         tp_price, sl_price, entry_price, buy_tx, int(time.time()))
    )


async def update_position_peak(ca: str, peak_price: float) -> None:
    await pool.execute(
        "UPDATE positions SET peak_price=? WHERE ca=? AND status='open'",
        (peak_price, ca)
    )


async def update_trailing_sl(ca: str, new_sl: float) -> None:
    """Поднимаем SL вверх (trailing). Никогда не опускаем — только если new_sl > текущего."""
    await pool.execute(
        "UPDATE positions SET sl_price=? WHERE ca=? AND status='open' AND sl_price < ?",
        (new_sl, ca, new_sl)
    )


async def close_position(
    ca: str, exit_price: float, exit_reason: str,
    pnl_sol: float, pnl_pct: float, sell_tx: str | None = None,
) -> None:
    await pool.execute(
        "UPDATE positions SET status='closed', exit_price=?, exit_reason=?, "
        "pnl_sol=?, pnl_pct=?, sell_tx=?, closed_at=? "
        "WHERE ca=? AND status='open'",
        (exit_price, exit_reason, pnl_sol, pnl_pct, sell_tx, int(time.time()), ca)
    )


async def get_closed_positions(limit: int = 20) -> list[dict]:
    rows = await pool.fetchall(
        "SELECT * FROM positions WHERE status='closed' ORDER BY closed_at DESC LIMIT ?",
        (limit,)
    )
    return [dict(r) for r in rows]


async def get_trading_stats() -> dict:
    """Общая статистика по закрытым позициям: общий PnL, винрейт, кол-во сделок."""
    row = await pool.fetchone(
        "SELECT COUNT(*) AS n, "
        "       COALESCE(SUM(pnl_sol), 0) AS total_pnl_sol, "
        "       SUM(CASE WHEN pnl_sol > 0 THEN 1 ELSE 0 END) AS wins "
        "FROM positions WHERE status='closed'"
    )
    n = row["n"] or 0
    wins = row["wins"] or 0
    return {
        "trades_count":  n,
        "wins":          wins,
        "losses":        n - wins,
        "win_rate":      (wins / n * 100) if n else 0.0,
        "total_pnl_sol": row["total_pnl_sol"] or 0.0,
    }


async def get_daily_pnl_sol(since_ts: int) -> float:
    row = await pool.fetchone(
        "SELECT COALESCE(SUM(pnl_sol), 0) FROM positions "
        "WHERE status='closed' AND closed_at >= ?",
        (since_ts,)
    )
    return row[0] if row else 0.0
