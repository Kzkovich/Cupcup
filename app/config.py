import os
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


class Settings:
    secret_key: str = os.environ.get("SECRET_KEY", "dev-insecure-key")
    database_url: str = os.environ.get("DATABASE_URL", "sqlite:///./data/app.db")
    admin_emails: set[str] = {
        e.strip().lower()
        for e in os.environ.get("ADMIN_EMAILS", "").split(",")
        if e.strip()
    }
    invite_code: str = os.environ.get("INVITE_CODE", "").strip()
    tg_bot_token: str = os.environ.get("TG_BOT_TOKEN", "").strip()
    tg_bot_username: str = os.environ.get("TG_BOT_USERNAME", "").strip()
    org_contact_text: str = os.environ.get(
        "ORG_CONTACT_TEXT", "Напишите организаторам турнира."
    )
    session_cookie_secure: bool = os.environ.get(
        "SESSION_COOKIE_SECURE", "false"
    ).lower() in ("1", "true", "yes")
    env: str = os.environ.get("ENV", "dev")

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.tg_bot_token)


@lru_cache
def get_settings() -> Settings:
    return Settings()
