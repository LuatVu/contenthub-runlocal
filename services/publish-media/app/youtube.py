"""Đăng YouTube Shorts (Data API v3, resumable upload qua httpx, OAuth2 refresh token).

Bytes video đọc qua một "source" ({size, read(start, length)}) nên file lớn được stream từng
chunk từ đĩa thay vì nạp hết vào RAM (storage.FileMedia).

Phạm vi bản local: format `reel` (<= 60 s, dọc) -> URL /shorts/<id>. Long-form (format `video`
với thumbnail + phụ đề) chưa chuyển sang vì luồng NBA không dùng.
"""

import logging
import os
from datetime import datetime, timezone

import httpx

from .channels import Channel, secret

logger = logging.getLogger(__name__)

_TOKEN_URL = os.environ.get("YOUTUBE_TOKEN_URL", "https://oauth2.googleapis.com/token")
_API = os.environ.get("YOUTUBE_API_BASE", "https://www.googleapis.com")
_CHUNK = 8 * 1024 * 1024  # bội số của 256 KiB
_PRIVACY = {"public", "unlisted", "private"}


def _raise(resp: httpx.Response, phase: str) -> None:
    if resp.is_error:
        raise RuntimeError(f"YouTube API error during '{phase}' {resp.status_code}: {resp.text[:500]}")


async def _access_token(client: httpx.AsyncClient, channel: Channel) -> str:
    # Một OAuth client (app) dùng chung cho mọi kênh; mỗi kênh có refresh token riêng.
    resp = await client.post(_TOKEN_URL, data={
        "grant_type": "refresh_token",
        "refresh_token": channel.credential(),
        "client_id": secret("YOUTUBE_CLIENT_ID"),
        "client_secret": secret("YOUTUBE_CLIENT_SECRET"),
    })
    if resp.is_error:
        # Body có thể lặp lại request; chỉ giữ mã lỗi của Google.
        try:
            err = resp.json().get("error")
        except ValueError:
            err = resp.status_code
        raise RuntimeError(f"YouTube token refresh failed for channel {channel.code}: {err}")
    return resp.json()["access_token"]


async def _verify_channel(client: httpx.AsyncClient, headers: dict, channel: Channel) -> None:
    resp = await client.get(f"{_API}/youtube/v3/channels", params={"part": "id", "mine": "true"},
                            headers=headers)
    _raise(resp, "channels")
    items = resp.json().get("items") or []
    actual = items[0]["id"] if items else None
    if actual != channel.external_id:
        raise RuntimeError(
            f"{channel.credential_key} resolves to YouTube channel {actual!r}, but channel {channel.code} is "
            f"{channel.external_id!r}. Re-authorize the refresh token and pick the correct channel in the "
            "account chooser."
        )


def _video_body(channel: Channel, title: str, description: str, tags: list[str] | None,
                publish_at: datetime | None, language: str | None) -> dict:
    """snippet + status của videos.insert. platform_settings: privacy (public|unlisted|private,
    mặc định public), category_id (mặc định 25 News & Politics), default_tags, default_language."""
    settings = channel.settings
    privacy = str(settings.get("privacy") or "public")
    if privacy not in _PRIVACY:
        raise RuntimeError(f"channel {channel.code}: invalid platform_settings.privacy {privacy!r}")
    lang = language or str(settings.get("default_language") or "en")
    default_tags = [str(t) for t in settings.get("default_tags") or [] if t]
    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:5000],
            "tags": list(dict.fromkeys((tags or []) + default_tags))[:30],
            "categoryId": str(settings.get("category_id") or "25"),
            "defaultLanguage": lang,
        },
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    if language:
        body["snippet"]["defaultAudioLanguage"] = language
    if publish_at:
        # YouTube chỉ áp dụng publishAt cho video private.
        body["status"]["privacyStatus"] = "private"
        body["status"]["publishAt"] = publish_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return body


async def _upload(client: httpx.AsyncClient, headers: dict, body: dict, source) -> dict:
    """Resumable upload của `source` ({size, read(start, length)}); trả về video resource."""
    resp = await client.post(
        f"{_API}/upload/youtube/v3/videos",
        params={"uploadType": "resumable", "part": "snippet,status"},
        headers={**headers, "X-Upload-Content-Type": "video/mp4",
                 "X-Upload-Content-Length": str(source.size)},
        json=body,
    )
    _raise(resp, "start")
    upload_url = resp.headers.get("location")
    if not upload_url:
        raise RuntimeError("YouTube did not return a resumable upload URL")

    offset, total = 0, source.size
    # Có trần để một server cứ hỏi lại cùng range không làm request treo vô hạn.
    for _ in range(4 * (total // _CHUNK + 1) + 10):
        chunk = source.read(offset, _CHUNK)
        if not chunk:
            raise RuntimeError(f"video source ended at {offset} of {total} bytes")
        last = offset + len(chunk) - 1
        resp = await client.put(
            upload_url,
            headers={**headers, "Content-Type": "video/mp4",
                     "Content-Range": f"bytes {offset}-{last}/{total}"},
            content=chunk,
        )
        if resp.status_code == 308:
            rng = resp.headers.get("range")  # "bytes=0-NNN": byte cuối YouTube đã nhận
            offset = int(rng.rsplit("-", 1)[1]) + 1 if rng else 0
            continue
        _raise(resp, "upload")
        return resp.json()
    raise RuntimeError(f"YouTube upload did not finish after {total} bytes of chunks")


async def publish_video(channel: Channel, source, title: str, description: str,
                        tags: list[str] | None = None, publish_at: datetime | None = None,
                        language: str | None = None, client: httpx.AsyncClient | None = None) -> dict:
    """Upload một Short. `source` là bytes hoặc object {size, read(start, length)}.

    publish_at (datetime có timezone) upload ở chế độ private và để YouTube công khai đúng giờ;
    language ghi đè platform_settings.default_language và cũng đặt audio language.
    """
    from .storage import ByteMedia

    if isinstance(source, (bytes, bytearray)):
        source = ByteMedia(bytes(source))
    body = _video_body(channel, title, description, tags, publish_at, language)
    own = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(300, connect=10))
    try:
        headers = {"Authorization": f"Bearer {await _access_token(client, channel)}"}
        await _verify_channel(client, headers, channel)
        result = await _upload(client, headers, body, source)
        video_id = result["id"]
    except httpx.HTTPError as exc:
        raise RuntimeError(f"YouTube API request failed ({type(exc).__name__}): {exc}") from exc
    finally:
        if own:
            await client.aclose()

    out = {"post_id": video_id, "url": f"https://www.youtube.com/shorts/{video_id}"}
    if publish_at:
        out.update(status="scheduled", scheduled_for=publish_at.astimezone(timezone.utc).isoformat())
    return out
