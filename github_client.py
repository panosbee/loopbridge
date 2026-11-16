"""
GitHub API client για το AAPS Wizard.
Χειρίζεται: OAuth, fork, secrets (LibSodium sealed box), workflow dispatch, polling.
"""
import base64
import logging
import secrets
from typing import Optional

import httpx
from nacl import encoding, public

from config import settings

logger = logging.getLogger(__name__)

GH_API_BASE = "https://api.github.com"
UPSTREAM_REPO = "nightscout/AndroidAPS"
WORKFLOW_NAME = "aaps-ci.yml"

# OAuth app config
OAUTH_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
OAUTH_TOKEN_URL = "https://github.com/login/oauth/access_token"


def get_oauth_url(user_id: str = "") -> tuple[str, str]:
    """Επιστρέφει το GitHub OAuth URL και το state token (CSRF)."""
    state_parts = [secrets.token_hex(16)]
    if user_id:
        state_parts.append(user_id)
    state = ":".join(state_parts)
    if settings.domain == "localhost":
        redirect_uri = "http://localhost:8000/auth/github/callback"
    else:
        redirect_uri = f"https://{settings.domain}/api/auth/github/callback"
    url = (
        f"{OAUTH_AUTHORIZE_URL}"
        f"?client_id={settings.github_client_id}"
        f"&redirect_uri={redirect_uri}"
        f"&scope=repo%20workflow"
        f"&state={state}"
    )
    return url, state


async def exchange_code_for_token(code: str) -> Optional[dict]:
    """Ανταλλάσσει το OAuth code για access token."""
    async with httpx.AsyncClient() as client:
        r = await client.post(
            OAUTH_TOKEN_URL,
            data={
                "client_id": settings.github_client_id,
                "client_secret": settings.github_client_secret,
                "code": code,
            },
            headers={"Accept": "application/json"},
            timeout=15,
        )
        if r.status_code != 200:
            logger.error(f"Token exchange failed: {r.status_code} {r.text}")
            return None
        data = r.json()
        if "error" in data:
            logger.error(f"Token exchange error: {data}")
            return None
        return data


async def get_user_login(access_token: str) -> Optional[str]:
    """Παίρνει το GitHub username."""
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{GH_API_BASE}/user",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/vnd.github+json",
            },
            timeout=15,
        )
        if r.status_code != 200:
            return None
        return r.json()["login"]


async def fork_repo(access_token: str, user_login: str) -> Optional[dict]:
    """Κάνει fork το AndroidAPS στον λογαριασμό του χρήστη."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
    }
    async with httpx.AsyncClient() as client:
        # Πρώτα έλεγξε αν υπάρχει ήδη fork
        r = await client.get(
            f"{GH_API_BASE}/repos/{user_login}/AndroidAPS",
            headers=headers,
            timeout=15,
        )
        if r.status_code == 200:
            logger.info(f"Fork already exists for {user_login}")
            return r.json()

        # Δημιουργία fork
        r = await client.post(
            f"{GH_API_BASE}/repos/{UPSTREAM_REPO}/forks",
            json={"default_branch_only": False},
            headers=headers,
            timeout=30,
        )
        if r.status_code not in (200, 202):
            logger.error(f"Fork failed: {r.status_code} {r.text}")
            return None

        # Poll μέχρι να είναι έτοιμο (async fork)
        import asyncio
        for _ in range(24):  # 24 * 5s = 120s timeout
            await asyncio.sleep(5)
            r2 = await client.get(
                f"{GH_API_BASE}/repos/{user_login}/AndroidAPS",
                headers=headers,
                timeout=15,
            )
            if r2.status_code == 200:
                return r2.json()

        logger.error("Fork creation timed out")
        return None


async def enable_actions(access_token: str, user_login: str) -> bool:
    """Ενεργοποιεί τα Actions στο fork."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
    }
    async with httpx.AsyncClient() as client:
        r = await client.put(
            f"{GH_API_BASE}/repos/{user_login}/AndroidAPS/actions/permissions",
            json={"enabled": True, "allowed_actions": "all"},
            headers=headers,
            timeout=15,
        )
        return r.status_code in (200, 204)


async def get_repo_public_key(access_token: str, user_login: str) -> Optional[dict]:
    """Παίρνει το public key για encrypt secrets (LibSodium)."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
    }
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{GH_API_BASE}/repos/{user_login}/AndroidAPS/actions/secrets/public-key",
            headers=headers,
            timeout=15,
        )
        if r.status_code != 200:
            return None
        return r.json()


def encrypt_secret(public_key_b64: str, value: str) -> str:
    """Κρυπτογραφεί secret με LibSodium sealed box (για GitHub secrets API)."""
    public_key_bytes = base64.b64decode(public_key_b64)
    sealed_box = public.SealedBox(public.PublicKey(public_key_bytes))
    encrypted = sealed_box.encrypt(value.encode("utf-8"))
    return base64.b64encode(encrypted).decode("utf-8")


async def set_repo_secret(
    access_token: str, user_login: str, secret_name: str, encrypted_value: str, key_id: str
) -> bool:
    """Αποθηκεύει (create/update) secret στο repo."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
    }
    async with httpx.AsyncClient() as client:
        r = await client.put(
            f"{GH_API_BASE}/repos/{user_login}/AndroidAPS/actions/secrets/{secret_name}",
            json={"encrypted_value": encrypted_value, "key_id": key_id},
            headers=headers,
            timeout=15,
        )
        return r.status_code in (200, 201, 204)


async def setup_all_secrets(
    access_token: str,
    user_login: str,
    keystore_set: str,  # base64 encoded KEYSTORE_BASE64|PASS|ALIAS|KEY_PASS
    gdrive_oauth2: str,  # base64 encoded CLIENT_ID|REFRESH_TOKEN
) -> dict[str, bool]:
    """Στήνει όλα τα απαραίτητα secrets για το AAPS-CI."""
    pub_key = await get_repo_public_key(access_token, user_login)
    if not pub_key:
        return {"KEYSTORE_SET": False, "GDRIVE_OAUTH2": False}

    key_id = pub_key["key_id"]
    pk = pub_key["key"]

    results = {}
    for name, value in [("KEYSTORE_SET", keystore_set), ("GDRIVE_OAUTH2", gdrive_oauth2)]:
        encrypted = encrypt_secret(pk, value)
        ok = await set_repo_secret(access_token, user_login, name, encrypted, key_id)
        results[name] = ok

    return results


async def find_workflow_id(access_token: str, user_login: str) -> Optional[str]:
    """Βρίσκει το workflow ID για το aaps-ci.yml."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
    }
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{GH_API_BASE}/repos/{user_login}/AndroidAPS/actions/workflows",
            headers=headers,
            timeout=15,
        )
        if r.status_code != 200:
            return None
        workflows = r.json()["workflows"]
        for w in workflows:
            if w["path"] == f".github/workflows/{WORKFLOW_NAME}":
                return w["id"]
    return None


async def dispatch_build(
    access_token: str,
    user_login: str,
    workflow_id: str,
    version: str,
    build_variant: str = "fullRelease",
) -> Optional[str]:
    """Πυροδοτεί το AAPS CI workflow. Επιστρέφει run_id ή None."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
    }
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{GH_API_BASE}/repos/{user_login}/AndroidAPS/actions/workflows/{workflow_id}/dispatches",
            json={
                "ref": "master",
                "inputs": {
                    "tagName": version,
                    "buildVariant": build_variant,
                },
            },
            headers=headers,
            timeout=15,
        )
        if r.status_code != 204:
            logger.error(f"Dispatch failed: {r.status_code} {r.text}")
            return None

        # Το dispatch είναι async — περίμενε λίγο και βρες το run_id
        import asyncio
        await asyncio.sleep(3)

        r2 = await client.get(
            f"{GH_API_BASE}/repos/{user_login}/AndroidAPS/actions/runs"
            f"?event=workflow_dispatch&per_page=1",
            headers=headers,
            timeout=15,
        )
        if r2.status_code != 200:
            return None
        runs = r2.json()["workflow_runs"]
        if runs:
            return str(runs[0]["id"])
        return None


async def get_run_status(access_token: str, user_login: str, run_id: str) -> Optional[dict]:
    """Παίρνει κατάσταση ενός workflow run."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
    }
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{GH_API_BASE}/repos/{user_login}/AndroidAPS/actions/runs/{run_id}",
            headers=headers,
            timeout=15,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        return {
            "status": data["status"],
            "conclusion": data.get("conclusion"),
            "html_url": data["html_url"],
        }


async def get_latest_aaps_version() -> str:
    """Παίρνει την τελευταία έκδοση του AAPS."""
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{GH_API_BASE}/repos/{UPSTREAM_REPO}/releases/latest",
            headers={"Accept": "application/vnd.github+json"},
            timeout=15,
        )
        if r.status_code != 200:
            return "3.4.2.4"  # fallback
        tag = r.json()["tag_name"]
        return tag.lstrip("v")


async def sync_fork(access_token: str, user_login: str) -> bool:
    """Συγχρονίζει το fork με upstream (fast-forward)."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
    }
    async with httpx.AsyncClient() as client:
        # Πάρε το SHA του upstream master
        r = await client.get(
            f"{GH_API_BASE}/repos/{UPSTREAM_REPO}/git/ref/heads/master",
            headers=headers,
            timeout=15,
        )
        if r.status_code != 200:
            return False
        upstream_sha = r.json()["object"]["sha"]

        # Ενημέρωσε το ref του fork (fast-forward only)
        r2 = await client.patch(
            f"{GH_API_BASE}/repos/{user_login}/AndroidAPS/git/refs/heads/master",
            json={"sha": upstream_sha, "force": False},
            headers=headers,
            timeout=15,
        )
        return r2.status_code == 200
