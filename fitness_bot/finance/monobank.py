from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

API_BASE = "https://api.monobank.ua"
logger = logging.getLogger(__name__)


class MonobankAPIError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _secret(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    output = bytearray()
    counter = 0
    while len(output) < length:
        output.extend(hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest())
        counter += 1
    return bytes(output[:length])


def encrypt_token(token: str) -> str:
    secret = os.getenv("MONOBANK_TOKEN_SECRET") or os.getenv("BOT_TOKEN")
    if not secret:
        raise RuntimeError("Для безпечного збереження токена задайте MONOBANK_TOKEN_SECRET у .env.")
    raw = token.encode("utf-8")
    key = _secret(secret)
    encryption_key = hmac.new(key, b"fitness-bot/monobank/encryption", hashlib.sha256).digest()
    authentication_key = hmac.new(key, b"fitness-bot/monobank/authentication", hashlib.sha256).digest()
    nonce = secrets.token_bytes(16)
    stream = _keystream(encryption_key, nonce, len(raw))
    encrypted = bytes(value ^ stream[index] for index, value in enumerate(raw))
    tag = hmac.new(authentication_key, b"v2" + nonce + encrypted, hashlib.sha256).digest()
    return "v2:" + base64.urlsafe_b64encode(nonce + encrypted + tag).decode("ascii")


def decrypt_token(ciphertext: str) -> str:
    if ciphertext.startswith("v2:"):
        try:
            payload = base64.urlsafe_b64decode(ciphertext[3:].encode("ascii"))
            nonce, encrypted, tag = payload[:16], payload[16:-32], payload[-32:]
            if len(payload) < 48 or len(nonce) != 16 or len(tag) != 32:
                raise ValueError
            secrets_to_try = tuple(dict.fromkeys(value for value in (os.getenv("MONOBANK_TOKEN_SECRET"), os.getenv("BOT_TOKEN")) if value))
            if not secrets_to_try:
                raise RuntimeError("Задайте MONOBANK_TOKEN_SECRET, щоб розшифрувати токен Monobank.")
            for secret in secrets_to_try:
                key = _secret(secret)
                encryption_key = hmac.new(key, b"fitness-bot/monobank/encryption", hashlib.sha256).digest()
                authentication_key = hmac.new(key, b"fitness-bot/monobank/authentication", hashlib.sha256).digest()
                expected_tag = hmac.new(authentication_key, b"v2" + nonce + encrypted, hashlib.sha256).digest()
                if hmac.compare_digest(tag, expected_tag):
                    stream = _keystream(encryption_key, nonce, len(encrypted))
                    return bytes(value ^ stream[index] for index, value in enumerate(encrypted)).decode("utf-8")
            raise ValueError("Токен не пройшов перевірку цілісності.")
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError("Збережений токен Monobank пошкоджений або ключ шифрування змінився.") from exc

    encrypted = base64.urlsafe_b64decode(ciphertext.encode("ascii"))
    legacy_secrets = (os.getenv("MONOBANK_TOKEN_SECRET"), os.getenv("BOT_TOKEN"), "fitness-bot-token-secret")
    for secret in dict.fromkeys(value for value in legacy_secrets if value):
        key = _secret(secret)
        try:
            token = bytes(value ^ key[index % len(key)] for index, value in enumerate(encrypted)).decode("utf-8")
        except UnicodeDecodeError:
            continue
        if len(token) >= 20 and not any(char.isspace() for char in token):
            return token
    raise ValueError("Не вдалося розшифрувати старий токен. Підключіть Monobank повторно.")


def token_hint(token: str) -> str:
    return f"…{token[-4:]}" if len(token) >= 4 else "••••"


def category_for_mcc(mcc: int | None) -> str:
    groups = {
        "Продукти": {5411, 5422, 5441, 5451, 5462, 5499},
        "Транспорт": {4011, 4111, 4121, 4131, 4511, 5541, 5542, 5571, 5599},
        "Ресторани": {5812, 5813, 5814},
        "Комунальні": {4814, 4900},
        "Здоровʼя": {5912, 8011, 8021, 8031, 8041, 8042, 8043, 8050, 8062, 8099},
        "Розваги": {7832, 7911, 7922, 7929, 7932, 7933, 7941, 7991, 7994, 7995, 7996, 7998, 7999},
        "Покупки": {5311, 5331, 5399, 5611, 5621, 5631, 5641, 5651, 5661, 5691, 5699, 5712, 5732, 5734, 5942, 5999},
    }
    for name, codes in groups.items():
        if mcc in codes:
            return name
    return "Інше"


class MonobankClient:
    def __init__(self, token: str, timeout: float = 20.0):
        self.token = token
        self.timeout = timeout

    async def _get(self, path: str) -> Any:
        try:
            import httpx
        except ImportError as exc:
            raise RuntimeError("Для роботи з Monobank встановіть залежності: pip install -r requirements.txt") from exc
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(f"{API_BASE}{path}", headers={"X-Token": self.token})
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            if status_code == 429:
                raise MonobankAPIError("Monobank тимчасово обмежив кількість запитів.", status_code) from exc
            raise MonobankAPIError(f"Monobank повернув помилку API ({status_code}).", status_code) from exc
        except httpx.RequestError as exc:
            raise MonobankAPIError("Monobank недоступний.") from exc

    async def client_info(self) -> dict[str, Any]:
        info = await self._get("/personal/client-info")
        if not isinstance(info, dict):
            logger.error("Monobank client-info returned non-object JSON (%s)", type(info).__name__)
            raise MonobankAPIError("Monobank повернув некоректну відповідь.")
        logger.info(
            "Monobank client-info received: accounts=%r, jars=%r",
            len(info.get("accounts", [])) if isinstance(info.get("accounts"), list) else type(info.get("accounts")).__name__,
            len(info.get("jars", [])) if isinstance(info.get("jars"), list) else type(info.get("jars")).__name__,
        )
        return info

    async def statement(self, account: str, days: int = 31) -> list[dict[str, Any]]:
        days = max(1, min(days, 31))
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        from_timestamp = int(start.timestamp())
        to_timestamp = int(end.timestamp())
        return await self._get(f"/personal/statement/{account}/{from_timestamp}/{to_timestamp}")
