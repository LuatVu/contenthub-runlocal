"""Đọc/ghi media cho job render: file trong MEDIA_DIR hoặc URL https công khai.

Thay cho bản S3: "key" là đường dẫn tương đối trong <MEDIA_DIR> (cùng bind mount với
service nba-content), URL https vẫn tải trực tiếp (clip gốc, thumbnail của Storyteller).
"""

import asyncio
import ipaddress
import json
import logging
import os
import re
import socket
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse
from urllib.request import getproxies, proxy_bypass

import httpx

logger = logging.getLogger(__name__)

MEDIA_DIR = Path(os.environ.get("MEDIA_DIR", "/data/media"))
IMAGE_TYPES = ("image/",)
VIDEO_TYPES = ("video/", "application/octet-stream", "binary/octet-stream")
AUDIO_TYPES = ("audio/", "application/octet-stream", "binary/octet-stream")

_KEY = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9/._-]{0,200}$")


def media_path(key: str, *, create_parent: bool = False) -> Path:
    """Đường dẫn tuyệt đối của một media key, sau khi chắc chắn nó nằm trong MEDIA_DIR."""
    if not isinstance(key, str) or ".." in key or not _KEY.match(key):
        raise ValueError(f"media key không hợp lệ: {str(key)[:120]!r}")
    root = MEDIA_DIR.resolve()
    path = (root / key).resolve()
    if path != root and root not in path.parents:
        raise ValueError(f"media key thoát khỏi MEDIA_DIR: {key!r}")
    if create_parent:
        path.parent.mkdir(parents=True, exist_ok=True)
    return path


def read_json(key: str) -> Optional[dict]:
    try:
        raw = media_path(key).read_bytes()
    except FileNotFoundError:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def write_json(key: str, data) -> None:
    path = media_path(key, create_parent=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"))
    tmp.replace(path)


def put_file(key: str, src: Path) -> str:
    """Chuyển file đã render vào thư mục media (copy rồi rename, chịu được khác filesystem)."""
    path = media_path(key, create_parent=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(Path(src).read_bytes())
    tmp.replace(path)
    return key


def _ip_literal(hostname: str):
    """Trả về IP nếu hostname là địa chỉ IP viết thẳng, ngược lại None."""
    try:
        return ipaddress.ip_address(hostname.strip("[]"))
    except ValueError:
        return None


def _goes_through_proxy(hostname: str) -> bool:
    """True khi kết nối tới hostname này sẽ đi qua HTTP(S)_PROXY.

    Trong mạng có proxy bắt buộc (Docker tự tiêm HTTP_PROXY vào container), container
    KHÔNG phân giải được tên miền ngoài — chính proxy mới resolve rồi mở kết nối.
    Lúc đó getaddrinfo ở đây luôn gaierror, nên phải bỏ qua bước kiểm IP; URL trỏ
    thẳng vào IP nội bộ vẫn bị chặn ở _assert_public_host.
    """
    proxies = getproxies()
    if not any(proxies.get(scheme) for scheme in ("https", "http", "all")):
        return False
    try:
        return not proxy_bypass(hostname)
    except Exception:  # proxy_bypass không bao giờ nên làm vỡ luồng render
        return True


async def _assert_public_host(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise RuntimeError(f"Only https URLs are allowed: {url[:200]}")
    if os.environ.get("ALLOW_PRIVATE_FETCH") == "1":  # chỉ dùng khi test local
        return
    host = parsed.hostname

    literal = _ip_literal(host)
    if literal is not None:
        if not literal.is_global:
            raise RuntimeError(f"Refusing to fetch non-public address for host {host}")
        return

    if _goes_through_proxy(host):
        logger.debug("Bỏ kiểm IP cho %s: kết nối đi qua HTTP proxy, proxy tự phân giải DNS", host)
        return

    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, parsed.port or 443, type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise RuntimeError(
            f"Không phân giải được tên miền '{host}' ({exc}). "
            "Container không có DNS ra ngoài: kiểm tra HTTP_PROXY/HTTPS_PROXY hoặc DNS của Docker."
        ) from exc
    for info in infos:
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise RuntimeError(f"Refusing to fetch non-public address for host {host}")


async def _fetch_url(url: str, max_bytes: int, allowed_types: tuple[str, ...]) -> bytes:
    current = url
    async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=10), follow_redirects=False) as client:
        for _ in range(4):
            await _assert_public_host(current)
            async with client.stream("GET", current) as resp:
                if resp.is_redirect:
                    current = urljoin(current, resp.headers.get("location", ""))
                    continue
                if resp.status_code != 200:
                    raise RuntimeError(f"HTTP {resp.status_code} for {current[:200]}")
                ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                if not any(ctype.startswith(t) for t in allowed_types):
                    raise RuntimeError(f"Unexpected content-type '{ctype}' for {current[:200]}")
                buf = bytearray()
                async for chunk in resp.aiter_bytes(65536):
                    buf.extend(chunk)
                    if len(buf) > max_bytes:
                        raise RuntimeError(f"File exceeds {max_bytes} bytes")
                return bytes(buf)
    raise RuntimeError("Too many redirects")


async def load(ref: str, *, max_bytes: int, allowed_types: tuple[str, ...]) -> bytes:
    """`ref` là URL https hoặc media key trong MEDIA_DIR."""
    if not ref:
        raise RuntimeError("Empty media reference")
    if ref.startswith("https://") or ref.startswith("http://"):
        return await _fetch_url(ref, max_bytes, allowed_types)
    path = media_path(ref)
    size = await asyncio.to_thread(lambda: path.stat().st_size)
    if size > max_bytes:
        raise RuntimeError(f"Media {ref} exceeds {max_bytes} bytes")
    return await asyncio.to_thread(path.read_bytes)
