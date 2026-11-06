"""Đăng media từ thư mục media local lên một kênh (thay cho Lambda publish-media, X-Api-Key).

POST / {mode: "publish", format: "reel", channel, video_key, title, description, tags,
        publish_at?, language?}
  channel  {code, platform, external_id, credential_key, platform_settings}
           (một dòng content.channels do n8n gửi; credential_key là TÊN biến môi trường
           chứa token, token không bao giờ đi qua n8n)
  publish_at (ISO-8601 có timezone, 11 phút - 30 ngày) đặt lịch thay vì đăng ngay;
  language (vd. "en") ghi đè mặc định của kênh.

Lỗi nền tảng trả HTTP 200 {status: "failed", channel, platform, error} để n8n ghi kết quả
từng kênh bằng content_nba.finish_reel(); thành công trả {status: "published", channel,
platform, post_id, url} hoặc {status: "scheduled", ..., scheduled_for} khi có publish_at.
"""

import asyncio
import hmac
import json
import logging
import os
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import facebook, youtube
from .channels import Channel
from .storage import FileMedia, read_media

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("publish-media")

# Cặp (platform, format) đã có publisher. Kênh của nền tảng khác (tiktok, instagram, threads)
# được schema chấp nhận nhưng dừng ở đây đến khi có module tương ứng.
SUPPORTED = {("facebook", "reel"), ("youtube", "reel")}

_MIN_LEAD = timedelta(minutes=11)  # Facebook từ chối lịch gần hơn 10 phút
_MAX_LEAD = timedelta(days=30)

app = FastAPI(title="publish-media")


def _parse_publish_at(value) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"'publish_at' must be an ISO-8601 timestamp: {value!r}") from exc
    if when.tzinfo is None:
        raise ValueError("'publish_at' must include a timezone offset (e.g. 2026-11-14T23:30:00Z)")
    when = when.astimezone(timezone.utc)
    lead = when - datetime.now(timezone.utc)
    if lead < _MIN_LEAD or lead > _MAX_LEAD:
        raise ValueError(f"'publish_at' must be between 11 minutes and 30 days from now, got {when.isoformat()}")
    return when


async def _publish(channel: Channel, fmt: str, p: dict) -> dict:
    video_key = p.get("video_key")
    title = str(p.get("title") or "").strip()
    if not video_key or not title:
        raise ValueError("'video_key' and 'title' are required")
    publish_at = _parse_publish_at(p.get("publish_at"))
    description = str(p.get("description") or "")
    language = str(p.get("language") or "").strip() or None
    tags = [str(t) for t in p.get("tags") or [] if t][:20]

    if channel.platform == "facebook":
        # Graph API upload theo chunk từ bytes trong RAM (reel chỉ vài MB).
        video = await asyncio.to_thread(read_media, video_key)
        return await facebook.publish_video(channel, video, title, description, publish_at)
    # YouTube đọc theo range trực tiếp từ đĩa, không nạp cả file vào RAM.
    source = await asyncio.to_thread(FileMedia, video_key)
    return await youtube.publish_video(channel, source, title, description, tags, publish_at, language)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "publish-media"}


@app.post("/")
async def run(request: Request) -> JSONResponse:
    expected = os.environ.get("APP_API_KEY") or ""
    if not expected:
        logger.error("APP_API_KEY chưa được đặt trong .env")
        return JSONResponse(status_code=503, content={"error": "publish-media not configured"})
    if not hmac.compare_digest((request.headers.get("x-api-key") or "").encode(), expected.encode()):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})

    try:
        raw = await request.body()
        params = json.loads(raw) if raw else {}
        if not isinstance(params, dict):
            raise ValueError("request body must be a JSON object")
        if params.get("mode") != "publish":
            raise ValueError(f"Unknown mode {params.get('mode')!r}. Valid: publish")
        fmt = str(params.get("format") or "")
        if fmt != "reel":
            raise ValueError("'format' must be 'reel' (chỉ luồng nba_reel_pipeline được chuyển sang local)")
        channel = Channel.from_payload(params.get("channel"))
    except (ValueError, json.JSONDecodeError) as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})

    meta = {"channel": channel.code, "platform": channel.platform}
    if (channel.platform, fmt) not in SUPPORTED:
        return JSONResponse(status_code=200, content={
            "status": "failed", **meta,
            "error": f"publishing {fmt} to {channel.platform} is not implemented yet",
        })
    try:
        result = await _publish(channel, fmt, params)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc), **meta})
    except Exception as exc:
        logger.exception("đăng %s lên %s thất bại", fmt, channel.code)
        return JSONResponse(status_code=200, content={"status": "failed", **meta, "error": str(exc)[:1000]})
    logger.info("%s %s lên %s: %s", result.get("status", "published"), fmt, channel.code, result["url"])
    return JSONResponse(status_code=200, content={"status": "published", **meta, **result})
