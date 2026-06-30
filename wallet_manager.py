"""
wallet_manager.py — non-custodial Solana-кошелёк для реальной автоторговли.

Как это работает:
  1. PumpPortal Local Transaction API ("/api/trade-local") только СОБИРАЕТ
     неподписанную транзакцию buy/sell по bonding curve pump.fun. Приватный
     ключ им не передаётся вообще.
  2. Мы подписываем эту транзакцию локально, у себя в процессе, ключом
     из WALLET_PRIVATE_KEY (переменная окружения, никуда не логируется).
  3. Подписанную транзакцию отправляем через свой собственный Helius RPC
     (тот же, что уже используется для анализа токенов).

Это значит: ни PumpPortal, ни кто-либо ещё не получает контроль над
кошельком — только пользователь, у которого есть .env с ключом.

Модуль импортируется и кошелёк создаётся ТОЛЬКО когда включён реальный
режим (/paper off) — для paper-режима solders вообще не требуется.
"""
import base64
import logging

import aiohttp

import config

log = logging.getLogger("Wallet")


class Wallet:
    def __init__(self, private_key_b58: str) -> None:
        self.ready: bool = False
        self.pubkey: str | None = None
        self._keypair = None

        if not private_key_b58:
            log.error(
                "WALLET_PRIVATE_KEY не задан в .env — реальная торговля "
                "недоступна, бот останется в paper-режиме."
            )
            return
        try:
            from solders.keypair import Keypair  # soft-dependency
            self._keypair = Keypair.from_base58_string(private_key_b58)
            self.pubkey = str(self._keypair.pubkey())
            self.ready = True
            log.info(f"Кошелёк загружен: {self.pubkey[:4]}...{self.pubkey[-4:]}")
        except ImportError:
            log.error(
                "Пакет 'solders' не установлен — выполните "
                "`pip install solders` для реальной торговли."
            )
        except Exception as exc:
            log.error(f"Не удалось загрузить кошелёк из WALLET_PRIVATE_KEY: {exc}")

    async def get_balance_sol(self) -> float:
        if not self.ready:
            return 0.0
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    config.HELIUS_RPC_URL,
                    json={
                        "jsonrpc": "2.0", "id": 1,
                        "method": "getBalance", "params": [self.pubkey],
                    },
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as r:
                    data = await r.json()
            lamports = (data.get("result") or {}).get("value", 0)
            return lamports / 1_000_000_000
        except Exception as exc:
            log.error(f"getBalance failed: {exc}")
            return 0.0

    async def buy(
        self, mint: str, sol_amount: float,
        slippage: int = 15, priority_fee: float = 0.0005,
    ) -> dict:
        """Покупка на фиксированную сумму SOL."""
        return await self._trade(
            action="buy", mint=mint, amount=sol_amount,
            denominated_in_sol=True, slippage=slippage, priority_fee=priority_fee,
        )

    async def sell(
        self, mint: str, amount: str = "100%",
        slippage: int = 15, priority_fee: float = 0.0005,
    ) -> dict:
        """Продажа. amount по умолчанию '100%' — закрыть всю позицию по токену."""
        return await self._trade(
            action="sell", mint=mint, amount=amount,
            denominated_in_sol=False, slippage=slippage, priority_fee=priority_fee,
        )

    async def _trade(
        self, action: str, mint: str, amount, denominated_in_sol: bool,
        slippage: int, priority_fee: float,
    ) -> dict:
        if not self.ready:
            return {"ok": False, "error": "wallet_not_ready"}

        payload = {
            "publicKey":        self.pubkey,
            "action":           action,
            "mint":             mint,
            "amount":           amount,
            "denominatedInSol": "true" if denominated_in_sol else "false",
            "slippage":         slippage,
            "priorityFee":      priority_fee,
            "pool":             "auto",
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    config.PUMPPORTAL_TRADE_LOCAL_URL, data=payload,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as r:
                    if r.status != 200:
                        body = await r.text()
                        return {"ok": False, "error": f"pumpportal_{r.status}: {body[:200]}"}
                    raw_tx = await r.read()
        except Exception as exc:
            return {"ok": False, "error": f"pumpportal_request_failed: {exc}"}

        try:
            from solders.transaction import VersionedTransaction
            unsigned = VersionedTransaction.from_bytes(raw_tx)
            signed = VersionedTransaction(unsigned.message, [self._keypair])
            signed_b64 = base64.b64encode(bytes(signed)).decode()
        except Exception as exc:
            return {"ok": False, "error": f"sign_failed: {exc}"}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    config.HELIUS_RPC_URL,
                    json={
                        "jsonrpc": "2.0", "id": 1,
                        "method": "sendTransaction",
                        "params": [
                            signed_b64,
                            {"encoding": "base64", "skipPreflight": False,
                             "preflightCommitment": "confirmed"},
                        ],
                    },
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as r:
                    data = await r.json()
        except Exception as exc:
            return {"ok": False, "error": f"rpc_send_failed: {exc}"}

        if "error" in data:
            return {"ok": False, "error": str(data["error"])[:300]}

        return {"ok": True, "signature": data.get("result")}
