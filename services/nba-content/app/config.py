"""Cấu hình, secret (.env), gọi OpenAI và vài helper dùng chung cho service nba-content.

Thay cho common.py của lambda cũ: không còn AWS Secrets Manager, mọi khoá đọc từ biến
môi trường (docker compose nạp từ file .env).
"""

import json
import os
from typing import Optional

import httpx

CHAT_MODEL = os.environ.get("OPENAI_CHAT_MODEL", "gpt-4o")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
# Mọi media của luồng NBA nằm dưới <MEDIA_DIR>/nba/<reel_id>/...
MEDIA_PREFIX = "nba"


def secret(name: str, required: bool = True) -> str:
    """Giá trị của một khoá trong .env (trước đây là app secret trên AWS)."""
    value = os.environ.get(name) or ""
    if required and not value:
        raise RuntimeError(f"{name} chưa được đặt trong .env")
    return value


def api_key() -> str:
    """Khoá n8n phải gửi ở header X-Api-Key."""
    return os.environ.get("APP_API_KEY") or ""


def clamp(value, default, low, high, cast=int):
    try:
        return max(low, min(high, cast(value)))
    except (TypeError, ValueError):
        return default


def as_dict(value) -> dict:
    """Nhận dict hoặc chuỗi JSON (n8n có thể gửi cột jsonb dạng text)."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {}


def require_uuid(params: dict, field: str) -> str:
    value = str(params.get(field) or "")
    if len(value) != 36:
        raise ValueError(f"'{field}' must be a UUID")
    return value


# ─── OpenAI ────────────────────────────────────────────────────────────────

async def chat_json(messages: list[dict], temperature: float, max_tokens: Optional[int] = None) -> dict:
    body = {
        "model": CHAT_MODEL,
        "messages": messages,
        "temperature": temperature,
        "response_format": {"type": "json_object"},
    }
    if max_tokens:
        body["max_tokens"] = max_tokens
    async with httpx.AsyncClient(
        base_url=OPENAI_BASE_URL,
        headers={"Authorization": f"Bearer {secret('OPENAI_API_KEY')}"},
        timeout=httpx.Timeout(180, connect=10),
    ) as client:
        resp = await client.post("/chat/completions", json=body)
    if resp.is_error:
        raise RuntimeError(f"OpenAI chat error {resp.status_code}: {resp.text[:300]}")
    return json.loads(resp.json()["choices"][0]["message"]["content"] or "{}")
