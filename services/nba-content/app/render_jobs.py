"""Khởi động / theo dõi job render của service nba-render (thay cho ECS RunTask trên Fargate).

Spec render được ghi xuống <MEDIA_DIR>/nba/<reel_id>/render.json (cả hai service mount cùng
thư mục media), nên request chỉ cần job_id = reel_id. Worker ghi render_result.json cạnh đó:
  {video_key, duration}  hoặc  {error}
"""

import asyncio
import logging
import os
import re
import time

import httpx

from .config import MEDIA_PREFIX, api_key
from .storage import delete, read_json, write_json

logger = logging.getLogger(__name__)

_ID = re.compile(r"^[0-9a-fA-F-]{36}$")
RENDER_URL = os.environ.get("RENDER_SERVICE_URL", "http://nba-render:8000").rstrip("/")
# trust_env=False: lời gọi nội bộ giữa hai container không bao giờ đi qua HTTP_PROXY của host
# (Docker tiêm biến proxy vào container nhưng không tiêm no_proxy).


def _prefix(reel_id: str) -> str:
    if not _ID.match(reel_id or ""):
        raise ValueError("reel_id must be a UUID")
    return f"{MEDIA_PREFIX}/{reel_id}"


def _headers() -> dict:
    return {"X-Api-Key": api_key()}


async def start(reel_id: str, spec: dict) -> dict:
    """spec: {clip_url, thumbnail_url?, segments: [{id,key,duration,words}], script, music_key?, params}."""
    prefix = _prefix(reel_id)
    if not str(spec.get("clip_url") or "").startswith("https://"):
        raise ValueError("render spec needs an https 'clip_url'")
    if not isinstance(spec.get("segments"), list) or not spec["segments"]:
        raise ValueError("render spec needs voice 'segments'")
    if not isinstance(spec.get("script"), dict) or not isinstance(spec.get("params"), dict):
        raise ValueError("render spec needs 'script' and 'params'")

    # Kết quả cũ còn sót lại sẽ khiến render_status báo xong ngay lập tức.
    await asyncio.to_thread(delete, f"{prefix}/render_result.json")
    await asyncio.to_thread(write_json, f"{prefix}/render.json", spec)

    async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=5), trust_env=False) as client:
        resp = await client.post(f"{RENDER_URL}/jobs", json={"job_id": reel_id}, headers=_headers())
    if resp.is_error:
        raise RuntimeError(f"nba-render từ chối job {resp.status_code}: {resp.text[:300]}")
    logger.info("Reel %s: đã xếp job render (%s)", reel_id, resp.json().get("state", "queued"))
    return {"status": "rendering", "job_id": reel_id}


async def _remote_state(reel_id: str) -> str:
    """queued | running | done | failed | unknown (service đã restart và mất hàng đợi)."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20, connect=5), trust_env=False) as client:
            resp = await client.get(f"{RENDER_URL}/jobs/{reel_id}", headers=_headers())
        if resp.status_code == 404:
            return "unknown"
        if resp.is_error:
            logger.warning("nba-render /jobs/%s -> %s", reel_id, resp.status_code)
            return "running"
        return str(resp.json().get("state") or "running")
    except httpx.HTTPError as exc:
        # Service render đang khởi động lại: coi như vẫn chạy, lần poll sau kiểm tra lại.
        logger.warning("Không gọi được nba-render: %s", exc)
        return "running"


async def status(reel_id: str, job_id: str | None = None) -> dict:
    """{status: rendering|video_ready|failed, video_key?, duration?, error?}."""
    prefix = _prefix(reel_id)
    result = await asyncio.to_thread(read_json, f"{prefix}/render_result.json")
    if result:
        if result.get("error"):
            return {"status": "failed", "error": str(result["error"])[:1000]}
        return {"status": "video_ready", "video_key": result["video_key"], "duration": result.get("duration")}

    state = await _remote_state(job_id or reel_id)
    if state in ("queued", "running"):
        return {"status": "rendering", "job_state": state}
    if state == "unknown":
        return {"status": "failed", "error": "render job không còn trong hàng đợi (nba-render đã restart?)"}
    # done/failed mà chưa có file kết quả: worker chết giữa đường.
    result = await asyncio.to_thread(read_json, f"{prefix}/render_result.json")
    if result and not result.get("error"):
        return {"status": "video_ready", "video_key": result["video_key"], "duration": result.get("duration")}
    return {"status": "failed", "error": f"render job kết thúc ({state}) mà không ghi được kết quả"}


async def wait(reel_id: str, job_id: str | None = None, max_seconds: int = 1800,
               interval: int = 10) -> dict:
    """Poll status() đến khi render xong hoặc hết max_seconds.

    Trả về status cuối cùng; 'rendering' nghĩa là hết hạn chờ và n8n coi đó là timeout.
    """
    deadline = time.monotonic() + max_seconds
    while True:
        result = await status(reel_id, job_id)
        if result["status"] != "rendering" or time.monotonic() + interval >= deadline:
            return result
        await asyncio.sleep(interval)
