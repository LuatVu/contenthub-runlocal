"""Lưu media trên đĩa local thay cho S3.

"key" giữ đúng dạng cũ (nba/<reel_id>/voice_hook.mp3) nên hợp đồng dữ liệu với n8n, DB và
service render không đổi; chỉ chỗ lưu đổi từ bucket S3 sang <MEDIA_DIR> (bind mount
./data/media của host). Mọi key đều được kiểm tra để không thoát ra ngoài MEDIA_DIR.
"""

import json
import os
import re
from pathlib import Path
from typing import Optional

MEDIA_DIR = Path(os.environ.get("MEDIA_DIR", "/data/media"))
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


def write_bytes(key: str, data: bytes) -> str:
    """Ghi bytes vào media key (ghi tạm rồi rename để không ai đọc được file nửa vời)."""
    path = media_path(key, create_parent=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    return key


def write_json(key: str, data) -> str:
    return write_bytes(key, json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"))


def read_json(key: str) -> Optional[dict]:
    """None khi file chưa tồn tại (vd. render chưa xong) hoặc đang được ghi dở."""
    try:
        raw = media_path(key).read_bytes()
    except FileNotFoundError:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def delete(key: str) -> None:
    media_path(key).unlink(missing_ok=True)
