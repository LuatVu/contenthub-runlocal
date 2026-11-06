"""Đọc media từ thư mục local (thay cho bucket S3 của bản cloud)."""

import os
import re
from pathlib import Path

MEDIA_DIR = Path(os.environ.get("MEDIA_DIR", "/data/media"))
_KEY = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9/._-]{0,200}$")


def media_path(key: str) -> Path:
    if not isinstance(key, str) or ".." in key or not _KEY.match(key):
        raise ValueError(f"media key không hợp lệ: {str(key)[:120]!r}")
    root = MEDIA_DIR.resolve()
    path = (root / key).resolve()
    if root not in path.parents:
        raise ValueError(f"media key thoát khỏi MEDIA_DIR: {key!r}")
    if not path.is_file():
        raise ValueError(f"media {key} không tồn tại trong MEDIA_DIR")
    return path


def read_media(key: str) -> bytes:
    """Đọc toàn bộ một media file (ảnh, phụ đề, video ngắn)."""
    return media_path(key).read_bytes()


class FileMedia:
    """Đọc theo range cho một file lớn: video dài được upload từng chunk, không nạp hết vào RAM."""

    def __init__(self, key: str):
        self.key = key
        self.path = media_path(key)
        self.size = self.path.stat().st_size
        if not self.size:
            raise ValueError(f"media {key} rỗng")

    def read(self, start: int, length: int) -> bytes:
        if start >= self.size or length <= 0:
            return b""
        with self.path.open("rb") as fh:
            fh.seek(start)
            return fh.read(min(length, self.size - start))


class ByteMedia:
    """Cùng giao diện nhưng trên bytes đã có trong RAM."""

    def __init__(self, data: bytes):
        self.data = data
        self.size = len(data)

    def read(self, start: int, length: int) -> bytes:
        return self.data[start:start + length]
