import json
import os
import time
from threading import RLock
from typing import Any, Dict
import secrets

TOKEN_STORE_FILE = os.getenv("TOKEN_STORE_FILE", "tokens.json")
TOKEN_EXPIRY = int(os.getenv("TOKEN_EXPIRY", "900"))

_lock = RLock()

def _load_store() -> Dict[str, Any]:
    if not os.path.exists(TOKEN_STORE_FILE):
        return {}
    with open(TOKEN_STORE_FILE, encoding="utf-8") as f:
        return json.load(f)

def _save_store(data: Dict[str, Any]) -> None:
    with open(TOKEN_STORE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def issue_token_for_user(user_email: str,
                         gh_token: str,
                         gh_expires_at: str) -> dict:
    """Create new short-lived token for user, revoke any existing one."""
    now = int(time.time())
    expires_at = now + TOKEN_EXPIRY
    short_token = secrets.token_urlsafe(32)

    with _lock:
        store = _load_store()
        # Revoke existing token(s) for this user
        for t in store.values():
            if t.get("user") == user_email:
                t["revoked"] = True
        # Create new entry
        store[short_token] = {
            "user": user_email,
            "issued_at": now,
            "expires_at": expires_at,
            "revoked": False,
            "github_token": gh_token,
            "github_expires_at": gh_expires_at,
        }
        _save_store(store)

    return {"token": short_token, "expires_in": TOKEN_EXPIRY}

def validate_short_token(short_token: str) -> dict | None:
    """Validate short token (for future /files endpoints)."""
    now = int(time.time())
    with _lock:
        store = _load_store()
        data = store.get(short_token)
        if not data:
            return None
        if data.get("revoked"):
            return None
        if data.get("expires_at", 0) < now:
            return None
        return data
