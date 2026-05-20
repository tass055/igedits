"""
Direct Instagram publishing via instagrapi (unofficial Instagram client).
Credentials and reusable session data are stored encrypted in the DB.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Encryption helpers
# ---------------------------------------------------------------------------

def _make_fernet(secret: str) -> Fernet:
    """Derive a Fernet key from BACKEND_AUTH_SECRET."""
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


def _encrypt(fernet: Fernet, value: str) -> str:
    return fernet.encrypt(value.encode()).decode()


def _decrypt(fernet: Fernet, token: str) -> str:
    return fernet.decrypt(token.encode()).decode()


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

async def save_credentials(
    db: AsyncSession,
    user_id: str,
    username: str,
    password: str,
    backend_auth_secret: str,
) -> None:
    fernet = _make_fernet(backend_auth_secret)
    enc_password = _encrypt(fernet, password)

    await db.execute(
        text(
            """
            INSERT INTO instagram_credentials (user_id, username, enc_password, enc_session_id, enc_session, auth_method, updated_at)
            VALUES (:user_id, :username, :enc_password, NULL, NULL, 'password', CURRENT_TIMESTAMP)
            ON CONFLICT (user_id) DO UPDATE
                SET username      = EXCLUDED.username,
                    enc_password  = EXCLUDED.enc_password,
                    enc_session_id = NULL,
                    enc_session   = NULL,
                    auth_method   = 'password',
                    updated_at    = CURRENT_TIMESTAMP
            """
        ),
        {"user_id": user_id, "username": username, "enc_password": enc_password},
    )
    await db.commit()


async def save_session_id(
    db: AsyncSession,
    user_id: str,
    username: str,
    session_id: str,
    backend_auth_secret: str,
) -> None:
    fernet = _make_fernet(backend_auth_secret)
    enc_session_id = _encrypt(fernet, unquote(session_id))

    await db.execute(
        text(
            """
            INSERT INTO instagram_credentials (user_id, username, enc_password, enc_session_id, enc_session, auth_method, updated_at)
            VALUES (:user_id, :username, NULL, :enc_session_id, NULL, 'session_id', CURRENT_TIMESTAMP)
            ON CONFLICT (user_id) DO UPDATE
                SET username       = EXCLUDED.username,
                    enc_password   = NULL,
                    enc_session_id = EXCLUDED.enc_session_id,
                    enc_session    = NULL,
                    auth_method    = 'session_id',
                    updated_at     = CURRENT_TIMESTAMP
            """
        ),
        {"user_id": user_id, "username": username, "enc_session_id": enc_session_id},
    )
    await db.commit()


async def get_credentials(
    db: AsyncSession,
    user_id: str,
    backend_auth_secret: str,
) -> dict[str, Any] | None:
    result = await db.execute(
        text(
            "SELECT username, enc_password, enc_session, enc_session_id, auth_method FROM instagram_credentials WHERE user_id = :user_id"
        ),
        {"user_id": user_id},
    )
    row = result.fetchone()
    if not row:
        return None

    fernet = _make_fernet(backend_auth_secret)
    return {
        "username": row.username,
        "password": _decrypt(fernet, row.enc_password) if row.enc_password else None,
        "session": _decrypt(fernet, row.enc_session) if row.enc_session else None,
        "session_id": _decrypt(fernet, row.enc_session_id) if row.enc_session_id else None,
        "auth_method": row.auth_method or "password",
    }


async def delete_credentials(db: AsyncSession, user_id: str) -> None:
    await db.execute(
        text("DELETE FROM instagram_credentials WHERE user_id = :user_id"),
        {"user_id": user_id},
    )
    await db.commit()


async def _save_session(
    db: AsyncSession,
    user_id: str,
    session_json: str,
    backend_auth_secret: str,
) -> None:
    fernet = _make_fernet(backend_auth_secret)
    enc_session = _encrypt(fernet, session_json)
    await db.execute(
        text(
            "UPDATE instagram_credentials SET enc_session = :enc_session, updated_at = CURRENT_TIMESTAMP WHERE user_id = :user_id"
        ),
        {"user_id": user_id, "enc_session": enc_session},
    )
    await db.commit()


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------

async def post_reel(
    db: AsyncSession,
    user_id: str,
    clip_path: Path,
    caption: str,
    backend_auth_secret: str,
) -> str:
    """Upload a clip as an Instagram Reel. Returns the media permalink."""
    from instagrapi import Client
    from instagrapi.exceptions import (
        LoginRequired,
        TwoFactorRequired,
        ChallengeRequired,
        SelectContactPointRecoveryForm,
        RecaptchaChallengeForm,
        SubmitPhoneNumberForm,
        BadPassword,
        ClientError,
    )

    creds = await get_credentials(db, user_id, backend_auth_secret)
    if not creds:
        raise ValueError("No Instagram credentials saved for this user")

    if not clip_path.exists():
        raise FileNotFoundError(f"Clip file not found: {clip_path}")

    cl = Client()
    auth_method = creds.get("auth_method", "password")

    try:
        if auth_method == "session_id" and creds.get("session_id"):
            logger.info("Instagram: session ID login for user %s", user_id)
            cl.login_by_sessionid(unquote(creds["session_id"]))
        else:
            def _login_fresh() -> None:
                logger.info("Instagram: fresh login for user %s", user_id)
                cl.login(creds["username"], creds["password"])

            if creds["session"]:
                cl.load_settings(json.loads(creds["session"]))
                try:
                    cl.login(creds["username"], creds["password"])
                except LoginRequired:
                    cl = Client()
                    _login_fresh()
            else:
                _login_fresh()
    except TwoFactorRequired:
        raise ValueError(
            "Two-factor authentication is enabled on this Instagram account. "
            "Disable 2FA or use a secondary account without 2FA."
        )
    except BadPassword:
        raise ValueError(
            "Incorrect Instagram password. Update your credentials in Settings → Integrations."
        )
    except (ChallengeRequired, SelectContactPointRecoveryForm, RecaptchaChallengeForm, SubmitPhoneNumberForm):
        raise ValueError(
            "Instagram blocked the login from this server's IP address. "
            "Check your email for a security alert from Instagram and click 'This was me' to approve it, "
            "then try posting again. If the issue persists, log in to instagram.com from this machine's browser first."
        )
    except ClientError as e:
        logger.error("Instagram API blocked (%s) for user %s: %s", auth_method, user_id, e)
        raise ValueError(
            "Instagram is blocking API requests from this server's IP address. "
            "Your credentials are valid, but Instagram rejects connections from this IP. "
            "Set INSTAGRAM_PROXY_URL in your .env to route through a residential proxy, "
            "or use Make.com instead (Settings → Integrations)."
        )
    except (LoginRequired, Exception) as e:
        logger.error("Instagram login failed (%s) for user %s: %s: %s", auth_method, user_id, type(e).__name__, e)
        if auth_method == "session_id":
            raise ValueError(
                "Instagram session ID has expired or is invalid. "
                "Get a new one from your browser DevTools (instagram.com → F12 → Application → Cookies → sessionid) "
                "and reconnect in Settings → Integrations."
            )
        raise

    # Persist the session so we avoid a full re-login next time
    session_json = json.dumps(cl.get_settings())
    await _save_session(db, user_id, session_json, backend_auth_secret)

    logger.info("Instagram: uploading reel %s for user %s", clip_path.name, user_id)
    media = cl.clip_upload(clip_path, caption=caption)

    permalink = f"https://www.instagram.com/reel/{media.code}/"
    logger.info("Instagram: reel posted at %s", permalink)
    return permalink
