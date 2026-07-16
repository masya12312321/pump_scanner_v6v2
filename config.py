import os
import sys
from dotenv import load_dotenv

load_dotenv()

# ── КЛЮЧИ — ТОЛЬКО ИЗ ENVIRONMENT ──────────────────────────────────────────────
# Раньше тут были вшиты реальные токены прямо в код. Это нельзя пушить в git —
# любой с доступом к репозиторию получит контроль над ботом и API-ключами.
# На Render: Dashboard → Environment → добавить эти три переменные вручную.
# Локально: скопировать .env.example в .env и заполнить.
BOT_TOKEN:  str = os.getenv("BOT_TOKEN",  "")
CHAT_ID:    str = os.getenv("CHAT_ID",    "")
HELIUS_KEY: str = os.getenv("HELIUS_KEY", "")
DB_PATH:    str = os.getenv("DB_PATH",    "pump_scanner.db")
LOG_LEVEL:  str = os.getenv("LOG_LEVEL",  "INFO")

if not BOT_TOKEN or not CHAT_ID or not HELIUS_KEY:
    sys.exit(
        "ОШИБКА: не заданы переменные окружения BOT_TOKEN / CHAT_ID / HELIUS_KEY.\n"
        "Локально: создайте .env (см. .env.example).\n"
        "На Render: Dashboard → ваш сервис → Environment → добавьте переменные."
    )

HELIUS_RPC_URL:  str = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_KEY}"
PUMP_WS_URL:     str = "wss://pumpportal.fun/api/data"
RUGCHECK_URL:    str = "https://api.rugcheck.xyz/v1/tokens"
DEXSCREENER_URL: str = "https://api.dexscreener.com/latest/dex/tokens"

# Queue sizes
NEW_TOKEN_QUEUE_SIZE:  int = 2000
RETRY_QUEUE_SIZE:      int = 500
ALERT_QUEUE_SIZE:      int = 200

# Workers
ANALYSIS_WORKER_COUNT: int = 20   # было 10
ALERT_WORKER_COUNT:    int = 3

# Semaphore limits
HELIUS_CONCURRENCY:      int = 20
RUGCHECK_CONCURRENCY:    int = 8
DEXSCREENER_CONCURRENCY: int = 10

# Retry
RETRY_DELAYS: list[int] = [30, 60, 120]
MAX_RETRIES:  int = 3

# Filters
MIN_CONFIDENCE_SCORE: int   = 40       # было 50 — снижаем чтобы больше алертов
MAX_AGE_MINUTES:      float = 30.0     # было 15 — даём токенам больше времени
MIN_LIQUIDITY_USD:    float = 500.0       # подняли с 200 — меньше pump-and-dump
AUTO_COOLDOWN_DROP:   float = 0.20

# ── ЖЁСТКИЙ ПОТОЛОК MCAP ──────────────────────────────────────────────────────
# Бот анализирует только свежие микро-монеты pump.fun.
# Всё что крупнее этого порога — устоявшийся токен, не наша зона.
MAX_SAFE_MCAP_USD: float = 2_000_000.0   # $2M — выше уже не "свежий мемкоин"

# ── ХАРДКОД-БЛЭКЛИСТ ИЗВЕСТНЫХ ТОКЕНОВ ────────────────────────────────────────
# Стейблкоины и мейджоры с official mint authority никогда не должны попадать
# в анализатор как "новый мемкоин", независимо от того, что говорит pump.fun WS.
# Это защита от ложного срабатывания типа "USDC дал прогноз x1.4 за 3 минуты".
KNOWN_MAJOR_TOKENS: set[str] = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",   # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",   # USDT
    "So11111111111111111111111111111111111111112",   # Wrapped SOL
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",    # mSOL
    "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",   # ETH (Wormhole)
    "9n4nbM75f5Ui33ZbPYXn59EwSgE8CGsHtAeTH5YFeJ9E",   # BTC (Wormhole)
    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",    # JUP
    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",   # BONK (established, not a fresh micro-cap)
}

# Timeouts
RUGCHECK_TIMEOUT:     float = 3.5
HELIUS_TIMEOUT:       float = 4.0
DEXSCREENER_TIMEOUT:  float = 3.5
WS_HEARTBEAT:         float = 30.0
WS_RECONNECT_DELAY:   float = 3.0

# Price history
PRICE_HISTORY_LIMIT:  int = 20

# X-Multiplier targets для расчёта тайминга выхода
X_TARGETS: list[float] = [2.0, 5.0, 10.0, 50.0, 100.0]

# ── АВТОТОРГОВЛЯ ──────────────────────────────────────────────────────────────
# WALLET_PRIVATE_KEY нужен ТОЛЬКО для реального режима (/paper off).
# Формат — base58 строка приватного ключа (как при экспорте из Phantom).
# Ключ НИКОГДА не уходит на сторонние серверы: PumpPortal Local Transaction
# API только собирает неподписанную транзакцию, подписываем её сами здесь
# и отправляем через свой Helius RPC. Если переменная не задана — бот может
# работать только в paper-режиме (симуляция, без реальных денег).
WALLET_PRIVATE_KEY: str = os.getenv("WALLET_PRIVATE_KEY", "")

PUMPPORTAL_TRADE_LOCAL_URL: str = "https://pumpportal.fun/api/trade-local"

# Параметры по умолчанию — все переопределяются пользователем через Telegram
# (/amount, /tp, /sl, /maxpos, /dailylimit) и хранятся в таблице trading_settings.
DEFAULT_PAPER_MODE:            bool  = True
DEFAULT_AUTOTRADE_ENABLED:     bool  = False
DEFAULT_POSITION_SIZE_SOL:     float = 0.05
DEFAULT_TAKE_PROFIT_PCT:       float = 100.0
DEFAULT_STOP_LOSS_PCT:         float = 30.0
DEFAULT_MAX_POSITIONS:         int   = 5
DEFAULT_MIN_CONFIDENCE_TRADE:  int   = 55
DEFAULT_DAILY_LOSS_LIMIT_SOL:  float = 1.0
DEFAULT_PAPER_BALANCE_SOL:     float = 5.0

DEFAULT_SLIPPAGE_PCT:     int   = 15
DEFAULT_PRIORITY_FEE_SOL: float = 0.0005

TRADE_MONITOR_INTERVAL_SEC: float = 15.0

# Trailing stop — активируется при росте на X%, SL подтягивается до пика*(1-Y%)
TRAILING_ACTIVATE_PCT: float = 50.0   # +50% от входа (было +30% — слишком агрессивно)
TRAILING_PULLBACK_PCT: float = 30.0   # 30% откат от пика допускаем (было 20%)
