"""Giọng đọc (ElevenLabs TTS) và nhạc nền (ElevenLabs Music) cho NBA shorts.

Khác bản cloud: audio ghi xuống <MEDIA_DIR>/nba/<reel_id>/ thay vì S3; key trả về cho n8n
giữ nguyên dạng cũ.
"""

import asyncio
import base64
import logging
import os
import re

import httpx

from .config import MEDIA_PREFIX, clamp, secret
from .storage import write_bytes

logger = logging.getLogger(__name__)

_BASE = os.environ.get("ELEVENLABS_BASE_URL", "https://api.elevenlabs.io/v1")
_VOICE_ID = re.compile(r"^[A-Za-z0-9]{8,64}$")
_SEGMENT_ID = re.compile(r"^[a-z][a-z0-9_]{0,23}$")
_MODEL = re.compile(r"^[A-Za-z0-9_.-]{3,64}$")
DEFAULT_MODEL = "eleven_v4"


def _float(value, default: float, low: float, high: float) -> float:
    return clamp(value, default, low, high, cast=float)


def _words_from_alignment(al: dict) -> tuple[list[dict], float]:
    chars = al.get("characters") or []
    starts = al.get("character_start_times_seconds") or []
    ends = al.get("character_end_times_seconds") or []
    if not chars or len(chars) != len(starts) or len(chars) != len(ends):
        raise RuntimeError("ElevenLabs returned no usable alignment")
    words: list[dict] = []
    cur, w_start, w_end = "", None, None
    for ch, st, en in zip(chars, starts, ends):
        if ch.isspace():
            if cur:
                words.append({"w": cur, "start": round(w_start, 3), "end": round(w_end, 3)})
            cur, w_start = "", None
            continue
        if w_start is None:
            w_start = st
        cur += ch
        w_end = en
    if cur:
        words.append({"w": cur, "start": round(w_start, 3), "end": round(w_end, 3)})
    return words, round(ends[-1], 3)


async def _speak(client: httpx.AsyncClient, text: str, voice_id: str, model_id: str,
                 settings: dict) -> tuple[bytes, dict]:
    resp = await client.post(
        f"{_BASE}/text-to-speech/{voice_id}/with-timestamps",
        params={"output_format": "mp3_44100_128"},
        headers={"xi-api-key": secret("ELEVENLABS_API_KEY"), "Content-Type": "application/json"},
        json={"text": text, "model_id": model_id, "voice_settings": settings},
    )
    if resp.status_code != 200:
        raise RuntimeError(f"ElevenLabs error {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    words, duration = _words_from_alignment(data.get("alignment") or {})
    return base64.b64decode(data["audio_base64"]), {"words": words, "duration": duration}


async def synthesize_segments(reel_id: str, p: dict) -> dict:
    """p: {segments:[{id,text}], voice_id, model_id?, stability?, style?, speed?, max_commentary_seconds?}.

    Một lần gọi TTS cho mỗi segment để đặt chính xác lên timeline. Segment 'commentary' dài hơn
    max_commentary_seconds bị bỏ (nó sẽ che quá nhiều tiếng sân của clip).
    """
    # n8n để trống voice_id -> lấy từ .env, để id của giọng không nằm trong file workflow.
    voice_id = str(p.get("voice_id") or os.environ.get("ELEVENLABS_VOICE_ID") or "")
    if not _VOICE_ID.match(voice_id):
        raise ValueError("'voice_id' phải là ElevenLabs voice id (hoặc đặt ELEVENLABS_VOICE_ID trong .env)")
    model_id = str(p.get("model_id") or DEFAULT_MODEL)
    if not _MODEL.match(model_id):
        raise ValueError("invalid 'model_id'")
    segments = p.get("segments")
    if not isinstance(segments, list) or not segments:
        raise ValueError("'segments' must be a non-empty list of {id, text}")
    seen = set()
    for seg in segments:
        sid = str((seg or {}).get("id") or "")
        if not _SEGMENT_ID.match(sid) or sid in seen or not str((seg or {}).get("text") or "").strip():
            raise ValueError("each segment needs a unique lowercase 'id' and non-empty 'text'")
        seen.add(sid)

    settings = {
        "stability": _float(p.get("stability"), 0.35, 0.0, 1.0),
        "similarity_boost": 0.8,
        "style": _float(p.get("style"), 0.5, 0.0, 1.0),
        "use_speaker_boost": True,
        "speed": _float(p.get("speed"), 1.08, 0.7, 1.2),
    }
    max_comment = _float(p.get("max_commentary_seconds"), 8.0, 1.0, 20.0)

    async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=10)) as client:
        results = await asyncio.gather(*[
            _speak(client, str(s["text"]).strip(), voice_id, model_id, settings) for s in segments
        ])

    out = []
    for seg, (audio, info) in zip(segments, results):
        if seg["id"] == "commentary" and info["duration"] > max_comment:
            logger.info("Reel %s: commentary %.1fs > %.1fs, dropped", reel_id, info["duration"], max_comment)
            continue
        key = f"{MEDIA_PREFIX}/{reel_id}/voice_{seg['id']}.mp3"
        await asyncio.to_thread(write_bytes, key, audio)
        out.append({"id": seg["id"], "key": key, "duration": info["duration"], "words": info["words"]})
    return {"status": "voiced", "segments": out}


async def generate_music(reel_id: str, p: dict) -> dict:
    """p: {prompt, duration_seconds}. ElevenLabs Music API, chỉ nhạc cụ, lưu thành music.mp3."""
    prompt = str(p.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("'prompt' is required")
    seconds = _float(p.get("duration_seconds"), 20.0, 3.0, 120.0)
    body = {
        "prompt": f"{prompt[:400]}. Instrumental only, no vocals.",
        "music_length_ms": int(round(seconds * 1000)),
        "model_id": str(p.get("model_id") or "music_v1"),
        "force_instrumental": True,
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(280, connect=10)) as client:
        resp = await client.post(
            f"{_BASE}/music",
            params={"output_format": "mp3_44100_128"},
            headers={"xi-api-key": secret("ELEVENLABS_API_KEY"), "Content-Type": "application/json"},
            json=body,
        )
    if resp.status_code != 200 or not resp.content:
        raise RuntimeError(f"ElevenLabs music error {resp.status_code}: {resp.text[:300]}")
    key = f"{MEDIA_PREFIX}/{reel_id}/music.mp3"
    await asyncio.to_thread(write_bytes, key, resp.content)
    return {"status": "music_ready", "music_key": key, "duration": seconds}
