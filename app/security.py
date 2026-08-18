import hashlib
import secrets
import string

import bcrypt

_RECOVERY_ALPHABET = "".join(c for c in string.ascii_uppercase + string.digits if c not in "01OIL")


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def generate_recovery_code() -> str:
    groups = ["".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(4)) for _ in range(3)]
    return "-".join(groups)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generate_reset_token() -> str:
    return secrets.token_urlsafe(32)


def normalize_recovery_code(raw: str) -> str:
    cleaned = "".join(ch for ch in raw.upper() if ch.isalnum())
    return cleaned
