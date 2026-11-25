"""Security helpers for secret encryption."""
import base64
import hashlib
import os
import re
from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))


def _get_fernet() -> Fernet:
    raw_key = os.getenv("TOKEN_ENCRYPTION_KEY", "").strip()
    if not raw_key:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY not set in .env")
    key = base64.urlsafe_b64encode(hashlib.sha256(raw_key.encode("utf-8")).digest())
    return Fernet(key)


def encrypt_token(token: str) -> str:
    if not token:
        return ""
    return _get_fernet().encrypt(token.encode("utf-8")).decode("utf-8")


def is_encrypted_token(token_value: str) -> bool:
    if not token_value:
        return False
    try:
        _get_fernet().decrypt(token_value.encode("utf-8"))
        return True
    except InvalidToken:
        return False


def decrypt_token(token_value: str) -> str:
    if not token_value:
        return ""
    try:
        return _get_fernet().decrypt(token_value.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise SecretDecryptionError("stored_token_decryption_failed") from exc
class SecretDecryptionError(RuntimeError):
    """Stored secret cannot be decrypted with the configured key."""


TELEGRAM_TOKEN_RE = re.compile(r"^\d{6,12}:[A-Za-z0-9_-]{20,}$")


def is_plausible_telegram_token(value: str) -> bool:
    return bool(TELEGRAM_TOKEN_RE.fullmatch((value or "").strip()))


def token_fingerprint(token: str) -> str:
    if not token:
        return ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
