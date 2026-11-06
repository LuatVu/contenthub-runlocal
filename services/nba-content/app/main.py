"""Các bước của luồng NBA short-video cho workflow n8n nba_reel_pipeline (chạy local, X-Api-Key).

Thay cho Lambda `nba-content`: cùng hợp đồng (POST JSON có trường "mode", header X-Api-Key,
400 cho ValueError, 500 {"error": ...} cho lỗi khác) nên node n8n chỉ cần đổi URL.
n8n vẫn sở hữu toàn bộ đọc/ghi DB; service này chỉ nhận dữ liệu và trả kết quả / media key.

Modes (POST / với body JSON, trường "mode"):
  fetch_clips    {collection, client_version?}   gọi API Storyteller bằng khoá trong .env
  select         {clips: [clip thô của Storyteller], exclude_ids: [clip id], params: {...tuỳ chọn selector}}
  script         {clip, channel_name, cta_voice?, cta_on_screen?, music_mode?}
  voice          {reel_id, script, voice_id, model_id?, stability?, style?, speed?, max_commentary_seconds?}
  music          {reel_id, prompt, duration_seconds, model_id?}
  render_start   {reel_id, spec: {clip_url, thumbnail_url, segments, script, music_key?, params}}
  render_status  {reel_id, job_id?}
  render_wait    {reel_id, job_id?, max_seconds?}   poll ngay trong request (render mất 1-5 phút)
  cleanup        {retention_days?}                  xoá media cũ (cũng tự chạy ngầm)
"""

import asyncio
import hmac
import json
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import cleanup, render_jobs, scripter, selector, storyteller, voice
from .config import api_key, as_dict, clamp, require_uuid

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("nba-content")

RETENTION_DAYS = float(os.environ.get("MEDIA_RETENTION_DAYS", "7"))
CLEANUP_INTERVAL_HOURS = float(os.environ.get("CLEANUP_INTERVAL_HOURS", "6"))


async def _dispatch(mode: str, p: dict) -> dict:
    if mode == "fetch_clips":
        return await storyteller.fetch_clips(p.get("collection"), p.get("client_version"))

    if mode == "select":
        clips = p.get("clips")
        if not isinstance(clips, list):
            raise ValueError("'clips' must be a list")
        return await selector.select(clips, p.get("exclude_ids") or [], as_dict(p.get("params")))

    if mode == "script":
        clip = as_dict(p.get("clip"))
        if not clip.get("clip_id") or not clip.get("description"):
            raise ValueError("'clip' with clip_id and description is required")
        script = await scripter.generate_script(
            clip, str(p.get("channel_name") or "NBA Game Turn On"),
            p.get("cta_voice") or None, p.get("cta_on_screen") or None,
            "off" if p.get("music_mode") == "off" else "auto",
        )
        return {"script": script}

    if mode == "voice":
        reel_id = require_uuid(p, "reel_id")
        script = as_dict(p.get("script"))
        segments = [
            {"id": "hook", "text": as_dict(script.get("hook")).get("voice")},
            {"id": "cta", "text": as_dict(script.get("cta")).get("voice")},
        ]
        commentary = as_dict(script.get("commentary"))
        if commentary.get("use") and commentary.get("voice"):
            segments.insert(1, {"id": "commentary", "text": commentary["voice"]})
        return await voice.synthesize_segments(reel_id, {**p, "segments": segments})

    if mode == "music":
        return await voice.generate_music(require_uuid(p, "reel_id"), p)

    if mode == "render_start":
        return await render_jobs.start(require_uuid(p, "reel_id"), as_dict(p.get("spec")))

    if mode == "render_status":
        reel_id = require_uuid(p, "reel_id")
        return await render_jobs.status(reel_id, p.get("job_id") or reel_id)

    if mode == "render_wait":
        reel_id = require_uuid(p, "reel_id")
        # Không còn giới hạn 15 phút của Lambda: trần là 2 giờ, n8n đặt timeout HTTP dài hơn.
        budget = clamp(p.get("max_seconds"), 1800, 30, 7200)
        return await render_jobs.wait(reel_id, p.get("job_id") or reel_id, budget)

    if mode == "cleanup":
        days = clamp(p.get("retention_days"), RETENTION_DAYS, 0, 3650, cast=float)
        return await asyncio.to_thread(cleanup.sweep, days)

    raise ValueError(
        f"Unknown mode {mode!r}. Valid: fetch_clips, select, script, voice, music, render_start, "
        "render_status, render_wait, cleanup"
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(cleanup.loop(RETENTION_DAYS, CLEANUP_INTERVAL_HOURS))
    logger.info("nba-content sẵn sàng (giữ media %s ngày)", RETENTION_DAYS)
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="nba-content", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "nba-content"}


@app.post("/")
async def run(request: Request) -> JSONResponse:
    expected = api_key()
    if not expected:
        logger.error("APP_API_KEY chưa được đặt trong .env")
        return JSONResponse(status_code=503, content={"error": "nba-content not configured"})
    if not hmac.compare_digest((request.headers.get("x-api-key") or "").encode(), expected.encode()):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})

    mode = ""
    try:
        raw = await request.body()
        params = json.loads(raw) if raw else {}
        if not isinstance(params, dict):
            raise ValueError("request body must be a JSON object")
        mode = str(params.get("mode") or "")
        return JSONResponse(content=jsonable(await _dispatch(mode, params)))
    except (ValueError, json.JSONDecodeError) as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    except Exception as exc:
        logger.exception("%s thất bại", mode)
        # Phía gọi là n8n của chính mình; thông điệp này được ghi vào error_message của reel.
        return JSONResponse(
            status_code=500, content={"error": f"{mode} failed: {type(exc).__name__}: {str(exc)[:500]}"},
        )


def jsonable(data: dict) -> dict:
    """json.dumps(default=str) như bản Lambda: số/Decimal/datetime lạ không làm vỡ response."""
    return json.loads(json.dumps(data, ensure_ascii=False, default=str))
