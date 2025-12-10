# auth.py
import csv
import os
from typing import Optional
import bcrypt

MANAGEMENT_CREDENTIALS_FILE = os.getenv("MANAGEMENT_CREDENTIALS_FILE", "credentials.csv")

def load_credentials() -> dict[str, str]:
    """Return {email: password_hash} from CSV."""
    creds: dict[str, str] = {}
    if not os.path.exists(MANAGEMENT_CREDENTIALS_FILE):
        return creds
    with open(MANAGEMENT_CREDENTIALS_FILE, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            email = row.get("email", "").strip().lower()
            pw_hash = row.get("password_hash", "").strip()
            if email and pw_hash:
                creds[email] = pw_hash
    return creds

_CACHED_CREDS: dict[str, str] | None = None

def verify_user(email: str, password: str) -> bool:
    """Verify plain password against bcrypt hash from CSV."""
    global _CACHED_CREDS
    if _CACHED_CREDS is None:
        _CACHED_CREDS = load_credentials()

    pw_hash: Optional[str] = _CACHED_CREDS.get(email.lower())
    if not pw_hash:
        return False

    # bcrypt only considers first 72 bytes, so truncate to avoid ValueError
    pwd_bytes = password.encode("utf-8")[:72]
    hash_bytes = pw_hash.encode("utf-8")

    try:
        return bcrypt.checkpw(pwd_bytes, hash_bytes)
    except ValueError:
        # Malformed hash → treat as invalid credentials
        return False
