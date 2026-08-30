"""Simple password hashing for non-Steam (CZE) user accounts."""
import hashlib
import os

_ITERATIONS = 200000  # ~0.1s on modern CPUs; tune if too slow


def hash_password(password: str) -> str:
    """Return a PBKDF2-SHA256 hash as hex:salt:iterations."""
    salt = os.urandom(16).hex()
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt),
                             _ITERATIONS)
    return f"{dk.hex()}:{salt}:{_ITERATIONS}"


def verify_password(password: str, stored: str) -> bool:
    """Check *password* against a string returned by hash_password."""
    try:
        dk_hex, salt_hex, iterations = stored.split(":")
        dk_expected = hashlib.pbkdf2_hmac(
            "sha256", password.encode(),
            bytes.fromhex(salt_hex), int(iterations))
        return dk_expected.hex() == dk_hex
    except (ValueError, AttributeError):
        return False
