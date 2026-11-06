"""Lấy danh sách clip từ API Storyteller.

Ở bản cloud, node *Fetch Clips* của n8n gọi trực tiếp API này và mang theo api key trong
*Set Parameters*. Bản local gọi qua service để:
  * api key chỉ nằm trong .env (STORYTELLER_API_KEY), không nằm trong file workflow;
  * n8n không cần ra internet -> không phải đi qua proxy của máy host.
"""

import logging
import os
import re

import httpx

from .config import secret

logger = logging.getLogger(__name__)

BASE_URL = os.environ.get("STORYTELLER_BASE_URL", "https://api.usestoryteller.com")
DEFAULT_CLIENT_VERSION = os.environ.get("STORYTELLER_CLIENT_VERSION", "10.13.15")
_COLLECTION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")
_VERSION = re.compile(r"^[0-9][0-9A-Za-z._-]{0,30}$")


async def fetch_clips(collection: str, client_version: str | None = None) -> dict:
    """Trả về {collection, count, clips: [clip thô]} — clip giữ nguyên như API trả về."""
    collection = str(collection or "").strip()
    if not _COLLECTION.match(collection):
        raise ValueError("'collection' phải là tên collection của Storyteller")
    version = str(client_version or DEFAULT_CLIENT_VERSION).strip()
    if not _VERSION.match(version):
        raise ValueError("'client_version' không hợp lệ")

    async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10)) as client:
        resp = await client.get(
            f"{BASE_URL}/api/app/clips/{collection}/clips",
            params={
                "ClientPlatform": "Web",
                "ClientVersion": version,
                "x-storyteller-api-key": secret("STORYTELLER_API_KEY"),
            },
        )
    if resp.is_error:
        raise RuntimeError(f"Storyteller error {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    clips = data.get("clips") if isinstance(data, dict) else None
    if not isinstance(clips, list):
        raise RuntimeError(f"Storyteller không trả về danh sách 'clips': {str(data)[:300]}")
    logger.info("Storyteller collection %s: %s clip", collection, len(clips))
    return {"collection": collection, "count": len(clips), "clips": clips}
