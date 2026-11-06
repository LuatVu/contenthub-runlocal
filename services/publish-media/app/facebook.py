"""Đăng video lên Facebook Page (Graph API, resumable upload).

Phạm vi bản local: format `reel` (video dọc ngắn) cho luồng NBA. Multi-photo post của luồng
tin tức chưa chuyển sang.
"""

import logging
import os
from datetime import datetime, timezone

import httpx

from .channels import Channel

logger = logging.getLogger(__name__)

_GRAPH_BASE = os.environ.get("FACEBOOK_GRAPH_BASE", "https://graph.facebook.com/v26.0")
_CHUNK = 8 * 1024 * 1024


def _raise_for_fb_error(resp: httpx.Response, phase: str) -> None:
    """Raise kèm chi tiết lỗi của Facebook (không bao giờ kèm access token như raise_for_status())."""
    if not resp.is_error:
        return
    try:
        err = resp.json().get("error", {})
    except ValueError:
        err = {}
    message = err.get("message") or resp.text[:500]
    if err.get("code") == 190:
        message = (
            f"{message}. The Facebook access token is invalid or expired; "
            "use a non-expiring System User token as the channel credential"
        )
    raise RuntimeError(
        f"Facebook API error during '{phase}' {resp.status_code}: {message} "
        f"(code={err.get('code')}, subcode={err.get('error_subcode')}, fbtrace_id={err.get('fbtrace_id')})"
    )


async def _page_token(client: httpx.AsyncClient, channel: Channel) -> str:
    """Page access token cho channel.external_id.

    platform_settings.token_type:
      "system_user" (mặc định): credential là token System User không hết hạn; service xin Page
          token mới trước mỗi lần đăng, nên cũng phát hiện được System User không có quyền Page.
      "page": credential là Page token dùng trực tiếp, sau khi kiểm tra đúng Page.
    """
    token = channel.credential()
    token_type = channel.settings.get("token_type", "system_user")
    if token_type == "page":
        resp = await client.get(f"{_GRAPH_BASE}/me", params={"fields": "id"},
                                headers={"Authorization": "Bearer " + token})
        _raise_for_fb_error(resp, phase="verify_page_token")
        actual = str(resp.json().get("id") or "")
        if actual != channel.external_id:
            raise RuntimeError(
                f"{channel.credential_key} is a token of Page {actual!r}, but channel {channel.code} "
                f"is Page {channel.external_id!r}"
            )
        return token
    if token_type != "system_user":
        raise RuntimeError(f"channel {channel.code}: unknown platform_settings.token_type {token_type!r}")

    resp = await client.get(
        f"{_GRAPH_BASE}/{channel.external_id}",
        params={"fields": "access_token"},
        headers={"Authorization": "Bearer " + token},
    )
    _raise_for_fb_error(resp, phase="get_page_access_token")
    page_token = resp.json().get("access_token")
    if not page_token:
        raise RuntimeError(
            f"Facebook did not return a Page access token for channel {channel.code} "
            f"(Page {channel.external_id}); verify that the System User of {channel.credential_key} "
            "has access to the Page and the required publishing permissions"
        )
    return page_token


async def publish_video(channel: Channel, video: bytes, title: str, description: str,
                        publish_at: datetime | None = None) -> dict:
    """Upload video lên Page theo từng chunk; chỉ trả {post_id, url} khi phase finish thành công.
    publish_at (datetime có timezone) tạo video đã đặt lịch, chưa public."""
    page_id = channel.external_id
    url = f"{_GRAPH_BASE}/{page_id}/videos"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=10)) as client:
            headers = {"Authorization": f"Bearer {await _page_token(client, channel)}"}

            resp = await client.post(url, params={"upload_phase": "start", "file_size": len(video)},
                                     headers=headers)
            _raise_for_fb_error(resp, phase="start")
            start = resp.json()
            session_id, video_id = start["upload_session_id"], start["video_id"]
            offset, end = int(start.get("start_offset", 0)), int(start.get("end_offset", len(video)))

            while offset < len(video):
                chunk = video[offset:max(end, offset + 1)] if end > offset else video[offset:offset + _CHUNK]
                resp = await client.post(
                    url,
                    params={"upload_phase": "transfer", "upload_session_id": session_id,
                            "start_offset": str(offset)},
                    headers=headers,
                    files={"video_file_chunk": ("reel.mp4", chunk, "video/mp4")},
                )
                _raise_for_fb_error(resp, phase="transfer")
                nxt = resp.json()
                new_offset = int(nxt.get("start_offset", offset + len(chunk)))
                if new_offset <= offset:
                    raise RuntimeError("Facebook upload did not advance")
                offset, end = new_offset, int(nxt.get("end_offset", len(video)))

            finish_data = {"title": title[:255], "description": description, "published": "true"}
            if publish_at:
                finish_data.update(
                    published="false", unpublished_content_type="SCHEDULED",
                    scheduled_publish_time=str(int(publish_at.timestamp())),
                )
            resp = await client.post(
                url,
                params={"upload_phase": "finish", "upload_session_id": session_id},
                headers=headers,
                data=finish_data,
            )
            _raise_for_fb_error(resp, phase="finish")
            if not resp.json().get("success"):
                raise RuntimeError("Facebook video upload finish did not succeed")
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Facebook API request failed ({type(exc).__name__}): {exc}") from exc
    out = {"post_id": str(video_id), "url": f"https://www.facebook.com/{page_id}/videos/{video_id}"}
    if publish_at:
        out.update(status="scheduled", scheduled_for=publish_at.astimezone(timezone.utc).isoformat())
    return out
