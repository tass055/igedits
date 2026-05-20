"""Instagram publishing — Make.com webhook or direct via instagrapi."""

import logging
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from ...auth_headers import USER_ID_HEADER, get_signed_user_id
from ...config import get_config
from ...database import get_db
from ...ai import generate_instagram_caption
from ...repositories.clip_repository import ClipRepository
from ...repositories.task_repository import TaskRepository
from ...services import instagram_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/instagram", tags=["instagram"])

MAX_CAPTION_LEN = 2200
MAX_HASHTAGS = 30


def _get_user_id(request: Request) -> str:
    config = get_config()
    if config.monetization_enabled:
        return get_signed_user_id(request, config)
    user_id = request.headers.get("user_id") or request.headers.get(USER_ID_HEADER)
    if not user_id:
        raise HTTPException(status_code=401, detail="User authentication required")
    return user_id


class PublishRequest(BaseModel):
    clip_id: str = Field(min_length=1, max_length=64)
    caption: Optional[str] = Field(default=None, max_length=MAX_CAPTION_LEN)

    @field_validator("caption")
    @classmethod
    def _validate_caption(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if v.count("#") > MAX_HASHTAGS:
            raise ValueError(f"caption exceeds {MAX_HASHTAGS} hashtags")
        return v


class CredentialsRequest(BaseModel):
    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=255)


class SessionIdRequest(BaseModel):
    username: str = Field(min_length=1, max_length=255)
    session_id: str = Field(min_length=1, max_length=512)


# ---------------------------------------------------------------------------
# Status / config endpoints
# ---------------------------------------------------------------------------

@router.get("/make-status")
async def make_status():
    config = get_config()
    return {"enabled": bool(config.make_instagram_webhook_url)}


@router.get("/status")
async def instagram_status(request: Request, db: AsyncSession = Depends(get_db)):
    """Return how Instagram publishing is configured for this user."""
    config = get_config()
    user_id = _get_user_id(request)

    if config.make_instagram_webhook_url:
        return {"method": "make", "connected": True}

    creds = await instagram_service.get_credentials(
        db, user_id, config.backend_auth_secret or ""
    )
    if creds:
        return {"method": "direct", "connected": True, "username": creds["username"]}

    return {"method": None, "connected": False}


# ---------------------------------------------------------------------------
# Credentials CRUD (direct / instagrapi flow)
# ---------------------------------------------------------------------------

@router.post("/credentials")
async def save_credentials(
    body: CredentialsRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user_id = _get_user_id(request)
    config = get_config()
    if not config.backend_auth_secret:
        raise HTTPException(status_code=500, detail="BACKEND_AUTH_SECRET is not configured")

    await instagram_service.save_credentials(
        db, user_id, body.username, body.password, config.backend_auth_secret
    )
    return {"status": "saved", "username": body.username}


@router.post("/credentials/session-id")
async def save_session_id_credentials(
    body: SessionIdRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user_id = _get_user_id(request)
    config = get_config()
    if not config.backend_auth_secret:
        raise HTTPException(status_code=500, detail="BACKEND_AUTH_SECRET is not configured")

    await instagram_service.save_session_id(
        db, user_id, body.username, body.session_id, config.backend_auth_secret
    )
    return {"status": "saved", "username": body.username, "auth_method": "session_id"}


@router.get("/credentials")
async def get_credentials(request: Request, db: AsyncSession = Depends(get_db)):
    user_id = _get_user_id(request)
    config = get_config()
    if not config.backend_auth_secret:
        raise HTTPException(status_code=500, detail="BACKEND_AUTH_SECRET is not configured")

    creds = await instagram_service.get_credentials(
        db, user_id, config.backend_auth_secret
    )
    if not creds:
        return {"connected": False}
    return {
        "connected": True,
        "username": creds["username"],
        "auth_method": creds.get("auth_method", "password"),
    }


@router.delete("/credentials")
async def delete_credentials(request: Request, db: AsyncSession = Depends(get_db)):
    user_id = _get_user_id(request)
    await instagram_service.delete_credentials(db, user_id)
    return {"status": "disconnected"}


# ---------------------------------------------------------------------------
# Publish
# ---------------------------------------------------------------------------

@router.post("/publish")
async def publish(
    body: PublishRequest, request: Request, db: AsyncSession = Depends(get_db)
):
    user_id = _get_user_id(request)
    config = get_config()

    clip = await ClipRepository.get_clip_by_id(db, body.clip_id)
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")

    task = await TaskRepository.get_task_by_id(db, clip["task_id"])
    if not task or task.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="Not authorized for this clip")

    caption = body.caption or ""

    # --- Make.com webhook (priority if configured) ---
    if config.make_instagram_webhook_url:
        public_url = f"{config.public_base_url}/clips/{clip['filename']}"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    config.make_instagram_webhook_url,
                    json={"video_url": public_url, "caption": caption, "clip_id": body.clip_id},
                )
                resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            logger.error("Make.com webhook returned error: %s", e)
            raise HTTPException(status_code=502, detail="Make.com webhook returned an error") from e
        except httpx.RequestError as e:
            logger.error("Failed to reach Make.com webhook: %s", e)
            raise HTTPException(status_code=502, detail="Could not reach Make.com webhook") from e
        return {"status": "sent", "video_url": public_url}

    # --- Direct instagrapi ---
    if not config.backend_auth_secret:
        raise HTTPException(status_code=503, detail="Instagram publishing not configured")

    clip_path = Path(config.temp_dir) / "clips" / clip["filename"]
    try:
        permalink = await instagram_service.post_reel(
            db, user_id, clip_path, caption, config.backend_auth_secret
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error("Instagram direct publish failed: %s", e)
        raise HTTPException(status_code=502, detail=f"Instagram error: {e}") from e

    return {"status": "posted", "permalink": permalink}


# ---------------------------------------------------------------------------
# Caption suggestion
# ---------------------------------------------------------------------------

@router.get("/suggest-caption")
async def suggest_caption(
    clip_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user_id = _get_user_id(request)

    clip = await ClipRepository.get_clip_by_id(db, clip_id)
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")

    task = await TaskRepository.get_task_by_id(db, clip["task_id"])
    if not task or task.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="Not authorized for this clip")

    transcript_text = clip.get("text") or ""
    if not transcript_text.strip():
        raise HTTPException(status_code=422, detail="Clip has no transcript text")

    try:
        suggestion = await generate_instagram_caption(
            transcript_text=transcript_text,
            hook_type=clip.get("hook_type"),
            reasoning=clip.get("reasoning"),
            virality_score=clip.get("virality_score"),
        )
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e)) from e

    hashtag_str = " ".join(f"#{h.lstrip('#')}" for h in suggestion.hashtags)
    full_caption = f"{suggestion.caption}\n\n{hashtag_str}".strip()
    return {"caption": full_caption, "hashtags": suggestion.hashtags}
