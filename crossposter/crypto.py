"""Secret handling: the app secret, and encryption of stored tokens."""
import base64
import hashlib
import json
import secrets
from pathlib import Path

from cryptography.fernet import Fernet

from .config import settings


def app_secret() -> str:
    """SECRET_KEY from the environment, or one generated once and kept in the data dir."""
    if settings.secret_key:
        return settings.secret_key
    path = settings.data_dir / ".secret"
    if path.exists():
        return path.read_text().strip()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    key = secrets.token_urlsafe(32)
    path.write_text(key)
    return key


def _fernet(secret: str | None = None) -> Fernet:
    digest = hashlib.sha256(("token-encryption:" + (secret or app_secret())).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_json(value: dict, secret: str | None = None) -> str:
    return _fernet(secret).encrypt(json.dumps(value).encode()).decode()


def decrypt_json(token: str | None, secret: str | None = None) -> dict:
    if not token:
        return {}
    return json.loads(_fernet(secret).decrypt(token.encode()))


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge
