import os
import time
from typing import Any

import jwt
import requests

GITHUB_APP_ID = os.getenv("GITHUB_APP_ID")
GITHUB_PRIVATE_KEY = os.getenv("GITHUB_PRIVATE_KEY")
INSTALLATION_ID = os.getenv("INSTALLATION_ID")
GITHUB_API_URL = "https://api.github.com"
TOKEN_EXPIRY = int(os.getenv("TOKEN_EXPIRY", "900"))

def _build_app_jwt() -> str:
    now = int(time.time())
    payload = {
        "iat": now - 60,
        "exp": now + TOKEN_EXPIRY,
        "iss": str(GITHUB_APP_ID),
    }
    private_key = GITHUB_PRIVATE_KEY.encode("utf-8")
    return jwt.encode(payload, private_key, algorithm="RS256")

def generate_installation_token() -> dict[str, Any]:
    """Generate GitHub App installation access token for DATABASE_REPO."""
    jwt_token = _build_app_jwt()
    headers = {
        "Authorization": f"Bearer {jwt_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    url = f"{GITHUB_API_URL}/app/installations/{INSTALLATION_ID}/access_tokens"
    resp = requests.post(url, headers=headers, json={})
    resp.raise_for_status()
    return resp.json()
