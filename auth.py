import csv
import io
import os
from typing import Optional, Dict
import bcrypt
import requests
import base64
import time

MANAGEMENT_REPO = os.getenv("MANAGEMENT_REPO")  # "BattusaiKuroKame/colordockmanagement"
MANAGEMENT_CREDENTIALS_PATH = os.getenv("MANAGEMENT_CREDENTIALS_PATH", "credentials.csv")
MANAGEMENT_GITHUB_TOKEN = os.getenv("MANAGEMENT_GITHUB_TOKEN")  # PAT with read:contents

GITHUB_API_URL = "https://api.github.com"

def fetch_credentials_csv() -> str:
    """Fetch latest credentials.csv from management repo."""
    if not all([MANAGEMENT_REPO, MANAGEMENT_GITHUB_TOKEN]):
        raise RuntimeError("Missing MANAGEMENT_REPO or MANAGEMENT_GITHUB_TOKEN env vars")

    url = f"{GITHUB_API_URL}/repos/{MANAGEMENT_REPO}/contents/{MANAGEMENT_CREDENTIALS_PATH}"
    headers = {
        "Authorization": f"token {MANAGEMENT_GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    resp = requests.get(url, headers=headers, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    if data.get("encoding") != "base64":
        raise RuntimeError(f"Unexpected encoding: {data.get('encoding')}")

    content_bytes = base64.b64decode(data["content"])
    return content_bytes.decode("utf-8")

def load_credentials() -> Dict[str, str]:
    """Parse live CSV → {email: password_hash}."""
    csv_text = fetch_credentials_csv()
    creds: Dict[str, str] = {}

    f = io.StringIO(csv_text)
    reader = csv.DictReader(f)
    for row in reader:
        email = row.get("email", "").strip().lower()
        pw_hash = row.get("password_hash", "").strip()
        if email and pw_hash:
            creds[email] = pw_hash
    return creds

_CACHED_CREDS: Dict[str, str] | None = None
_CACHE_TIME: float = 0
_CACHE_TTL = 300  # 5 minutes

def verify_user(email: str, password: str) -> bool:
    """Fetch latest creds from management repo and verify."""
    global _CACHED_CREDS, _CACHE_TIME

    now = time.time()
    if _CACHED_CREDS is None or now - _CACHE_TIME > _CACHE_TTL:
        try:
            _CACHED_CREDS = load_credentials()
            _CACHE_TIME = now
        except Exception as e:
            print("Failed to fetch credentials:", e)
            return False

    pw_hash: Optional[str] = _CACHED_CREDS.get(email.lower())
    if not pw_hash:
        return False

    pwd_bytes = password.encode("utf-8")[:72]
    hash_bytes = pw_hash.encode("utf-8")

    try:
        return bcrypt.checkpw(pwd_bytes, hash_bytes)
    except ValueError:
        return False
