import hashlib
import hmac
import secrets
from datetime import datetime, timezone, timedelta

PBKDF2_ITERATIONS = 260_000
SESSION_TTL_HOURS = 24 * 7  # 7 days


def hash_password(password: str) -> str:
    """Hash a password with PBKDF2-HMAC-SHA256 and a random salt. Format: '<salt_hex>$<digest_hex>'."""
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ITERATIONS)
    return f"{salt}${digest.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Constant-time verification of a password against a stored '<salt>$<digest>' hash."""
    try:
        salt, digest_hex = stored_hash.split("$", 1)
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ITERATIONS)
        return hmac.compare_digest(expected, actual)
    except (ValueError, TypeError):
        return False


def generate_session_token() -> str:
    return secrets.token_urlsafe(32)


def session_expiry() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=SESSION_TTL_HOURS)
