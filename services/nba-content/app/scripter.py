"""English script for one NBA short: hook intro, optional commentary over the clip, CTA outro, metadata, music decision."""

import re

from .config import chat_json

DEFAULT_CTA_VOICE = "Follow for daily NBA highlights!"
DEFAULT_CTA_ON_SCREEN = "FOLLOW FOR DAILY NBA HIGHLIGHTS"
BASE_HASHTAGS = ["#NBA", "#Basketball", "#Shorts"]

_SYSTEM = """You write scripts for "{channel}", a global English-language NBA highlights channel (YouTube Shorts and Facebook Reels).
You get the metadata of ONE official NBA clip. The clip itself plays in full, with its original arena sound, between a
short intro and a short outro that you script.

Hard rules:
- English only. Energetic but natural, like a sharp sports-show host. No profanity, no gambling, no hate.
- Use ONLY facts present in the metadata. Never invent scores, stats, records, opponents, dates or quotes.
  If the description is vague, stay vague ("watch this").
- Do not describe the whole play (viewers will see it). Tease it.
- hook.voice: 6-12 words, a hook that makes people stay (question, bold claim or tease). Spoken in about 3 seconds.
- hook.on_screen: 2-5 words, the same idea as the hook (e.g. "ANT'S DEEP BUZZER-BEATER").
- commentary: optional. use=true ONLY when a short reaction adds something (emotional/underdog story, context a casual
  fan would miss). voice max 14 words, spoken at the beginning of the clip over the arena sound. use=false for pure
  action clips that speak for themselves.
- title: max 85 characters, no hashtags, no ALL-CAPS sentences, at most one emoji, must name the player when known.
- description: 2 short sentences for viewers + a question to drive comments. No links, no hashtags.
- hashtags: 4-6 including the player and team when known (e.g. #AnthonyEdwards #Timberwolves). Do not repeat #NBA/#Shorts.
- tags: 8-12 plain keywords for YouTube (player, team, "NBA highlights", moment type).
- music: use=true only when a music bed improves the clip (emotional story, hype dunk montage feeling,
  slow-motion drama); use=false when crowd/commentary audio is the star (buzzer-beaters, game-winners). When
  true, prompt = a ~20-word instrumental brief (genre, mood, tempo, no vocals).
Return JSON:
{{"hook":{{"voice":"","on_screen":""}},
 "commentary":{{"use":false,"voice":""}},
 "title":"","description":"","hashtags":[],"tags":[],
 "music":{{"use":false,"prompt":"","reason":""}}}}"""


def _words(text: str) -> int:
    return len(text.split())


def _clean(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _hashtag(tag: str) -> str:
    tag = re.sub(r"[^0-9A-Za-z_]", "", str(tag).lstrip("#"))
    return f"#{tag}" if tag else ""


async def generate_script(clip: dict, channel_name: str, cta_voice: str | None = None,
                          cta_on_screen: str | None = None, music_mode: str = "auto") -> dict:
    meta = (
        f"Description: {clip.get('description')}\n"
        f"Players: {', '.join(clip.get('players') or []) or 'unknown'}\n"
        f"Teams: {', '.join(clip.get('teams') or []) or 'unknown'}\n"
        f"Clip length: {clip.get('duration')} s\n"
        f"Engagement: {clip.get('like_count', 0)} likes, {clip.get('share_count', 0)} shares\n"
        f"Why it was picked: {(clip.get('score') or {}).get('llm_reason', '')}"
    )
    data = await chat_json(
        [{"role": "system", "content": _SYSTEM.format(channel=channel_name)},
         {"role": "user", "content": meta}],
        temperature=0.8,
    )

    hook = data.get("hook") or {}
    hook_voice, hook_screen = _clean(hook.get("voice")), _clean(hook.get("on_screen"))
    if not hook_voice or not hook_screen:
        raise ValueError("script has no hook")
    if _words(hook_voice) > 16:
        hook_voice = " ".join(hook_voice.split()[:16])

    comm = data.get("commentary") or {}
    comm_voice = _clean(comm.get("voice"))
    use_comm = bool(comm.get("use")) and 0 < _words(comm_voice) <= 18

    title = _clean(data.get("title"))[:90]
    if not title:
        raise ValueError("script has no title")

    tags = [_hashtag(h) for h in data.get("hashtags") or []]
    base = {b.lower() for b in BASE_HASHTAGS}
    tags = [t for t in tags if t and t.lower() not in base]
    hashtags = list(dict.fromkeys(tags[:6] + BASE_HASHTAGS))

    music = data.get("music") or {}
    music_prompt = _clean(music.get("prompt"))[:300]
    use_music = music_mode != "off" and bool(music.get("use")) and bool(music_prompt)

    description = _clean(data.get("description"))

    return {
        "hook": {"voice": hook_voice, "on_screen": hook_screen.upper()},
        "commentary": {"use": use_comm, "voice": comm_voice if use_comm else ""},
        "cta": {"voice": cta_voice or DEFAULT_CTA_VOICE, "on_screen": (cta_on_screen or DEFAULT_CTA_ON_SCREEN).upper()},
        "title": title,
        "description": f"{description}\n\n{' '.join(hashtags)}".strip(),
        "hashtags": hashtags,
        "tags": [_clean(t) for t in data.get("tags") or [] if _clean(t)][:12],
        "music": {"use": use_music, "prompt": music_prompt if use_music else "", "reason": _clean(music.get("reason"))},
    }
