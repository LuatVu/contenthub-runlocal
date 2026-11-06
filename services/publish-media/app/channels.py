"""Kênh đăng và credential của nó.

Mỗi kênh là một dòng content.channels do n8n gửi kèm request. Token KHÔNG đi qua n8n:
dòng kênh chỉ chứa `credential_key` = tên biến môi trường, service tự đọc từ .env.
"""

import os
import re
from dataclasses import dataclass, field

_PLATFORMS = {"facebook", "youtube", "tiktok", "instagram", "threads"}
_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def secret(name: str, required: bool = True) -> str:
    value = os.environ.get(name) or ""
    if required and not value:
        raise RuntimeError(f"{name} chưa được đặt trong .env")
    return value


@dataclass(frozen=True)
class Channel:
    """Một đích đăng (một dòng content.channels)."""

    code: str
    platform: str
    external_id: str
    credential_key: str
    settings: dict = field(default_factory=dict)

    @classmethod
    def from_payload(cls, data) -> "Channel":
        if not isinstance(data, dict):
            raise ValueError("'channel' object is required")
        code = str(data.get("code") or "").strip()
        platform = str(data.get("platform") or "").strip()
        external_id = str(data.get("external_id") or "").strip()
        credential_key = str(data.get("credential_key") or "").strip()
        settings = data.get("platform_settings") or {}
        if not code or not external_id:
            raise ValueError("channel.code and channel.external_id are required")
        if platform not in _PLATFORMS:
            raise ValueError(f"unknown channel.platform {platform!r}")
        if not _KEY_RE.match(credential_key):
            raise ValueError(
                "channel.credential_key must be an env var name like FACEBOOK_TOKEN_SOURCE_ACCESS_TOKEN"
            )
        if not isinstance(settings, dict):
            raise ValueError("channel.platform_settings must be an object")
        return cls(code, platform, external_id, credential_key, settings)

    def credential(self) -> str:
        """Token nằm trong .env dưới tên credential_key (n8n không bao giờ gửi nó)."""
        value = os.environ.get(self.credential_key) or ""
        if not value:
            raise RuntimeError(
                f"{self.credential_key} (credential của kênh {self.code}) chưa được đặt trong .env"
            )
        return value
