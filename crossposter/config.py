"""Settings, read once from the environment."""
import os
from dataclasses import dataclass, field
from pathlib import Path


def _bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    # Public URL the app is reached at. On a laptop this is http://127.0.0.1:5001;
    # Bluesky's localhost OAuth mode needs 127.0.0.1 rather than "localhost".
    base_url: str = field(default_factory=lambda: os.environ.get(
        "BASE_URL", f"http://127.0.0.1:{os.environ.get('PORT', '5001')}").rstrip("/"))
    port: int = field(default_factory=lambda: int(os.environ.get("PORT", "5001")))
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("DATA_DIR", "data")))
    feeds_dir: Path = field(default_factory=lambda: Path(os.environ.get("FEEDS_DIR", "feeds")))
    # Used to sign the session cookie and to encrypt tokens at rest.
    secret_key: str = field(default_factory=lambda: os.environ.get("SECRET_KEY", ""))
    # Password for the app itself. Empty means no login (fine on localhost only).
    app_password: str = field(default_factory=lambda: os.environ.get("APP_PASSWORD", ""))
    contact_email: str = field(default_factory=lambda: os.environ.get("CONTACT_EMAIL", "admin@example.com"))
    poll_seconds: int = field(default_factory=lambda: int(os.environ.get("POLL_SECONDS", "60")))
    cache_hours: int = field(default_factory=lambda: int(os.environ.get("CACHE_HOURS", "48")))
    feed_timeout_seconds: float = field(default_factory=lambda: float(os.environ.get("FEED_TIMEOUT_SECONDS", "5")))
    demo: bool = field(default_factory=lambda: _bool("DEMO"))
    # Set by the desktop launcher, which signs its own window in with APP_PASSWORD at /login/desktop.
    desktop: bool = field(default_factory=lambda: _bool("DESKTOP"))
    # X is experimental and off unless these are set.
    x_enabled: bool = field(default_factory=lambda: _bool("X_ENABLED"))
    x_client_id: str = field(default_factory=lambda: os.environ.get("X_CLIENT_ID", ""))
    x_client_secret: str = field(default_factory=lambda: os.environ.get("X_CLIENT_SECRET", ""))
    x_monthly_cap_usd: float = field(default_factory=lambda: float(os.environ.get("X_MONTHLY_CAP_USD", "10")))

    @property
    def is_loopback(self) -> bool:
        return self.base_url.startswith(("http://127.0.0.1", "http://localhost", "http://[::1]"))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "crossposter.db"


settings = Settings()
