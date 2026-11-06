"""Render một NBA short (1080x1920): intro hook -> clip gốc -> outro CTA, giọng đọc,
nhạc nền tuỳ chọn, phụ đề và overlay kênh tuỳ chọn.

Giống bản Fargate cũ, chỉ khác: media đọc/ghi trên đĩa local (xem storage.py) và preset
x264 lấy từ biến môi trường FFMPEG_PRESET/FFMPEG_CRF để chạy được trên PC cá nhân."""

import asyncio
import io
import json
import logging
import math
import os
import re
import shutil
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from .storage import AUDIO_TYPES, IMAGE_TYPES, VIDEO_TYPES, load

logger = logging.getLogger(__name__)

W, H = 1080, 1920
SRC_W, SRC_H = W * 2, H * 2  # zoompan on a 2x source avoids visible sub-pixel jitter
FPS = 30
VOICE_LEAD = 0.15
INTRO_MIN = 2.6
OUTRO_MIN = 2.6
FONTS_DIR = Path(os.environ.get("FONTS_DIR", "/usr/share/fonts"))
# Thư mục làm việc tạm (không phải thư mục media): file trung gian của ffmpeg.
WORK_DIR = Path(os.environ.get("WORK_DIR", "/tmp/render"))
# PC cá nhân chậm hơn Fargate: hạ preset xuống "fast"/"veryfast" nếu cần nhanh hơn.
FFMPEG_PRESET = os.environ.get("FFMPEG_PRESET", "medium")
FFMPEG_CRF = os.environ.get("FFMPEG_CRF", "20")
FONT_NAME = "Noto Sans"
MAX_CLIP_BYTES = 120 * 1024 * 1024
MAX_IMAGE_BYTES = 15 * 1024 * 1024
MAX_AUDIO_BYTES = 40 * 1024 * 1024
MIN_OVERLAY_TRANSPARENT_RATIO = 0.05


# ─── Stills (intro / outro backgrounds) ────────────────────────────────────

def _cover(img: Image.Image, w: int, h: int) -> Image.Image:
    scale = max(w / img.width, h / img.height)
    resized = img.resize((math.ceil(img.width * scale), math.ceil(img.height * scale)), Image.LANCZOS)
    left, top = (resized.width - w) // 2, (resized.height - h) // 2
    return resized.crop((left, top, left + w, top + h))


def _compose_still(data: bytes, out: Path, blur: int, dim: float) -> None:
    img = _cover(Image.open(io.BytesIO(data)).convert("RGB"), SRC_W, SRC_H)
    if blur:
        img = img.filter(ImageFilter.GaussianBlur(blur))
    if dim:
        img = Image.blend(img, Image.new("RGB", img.size, (0, 0, 0)), dim)
    # Darker bands behind the top and bottom text for readability.
    mask = Image.new("L", img.size, 0)
    draw = ImageDraw.Draw(mask)
    top_end, bottom_start = int(SRC_H * 0.25), int(SRC_H * 0.6)
    for y in range(top_end):
        draw.line([(0, y), (SRC_W, y)], fill=int(110 * (1 - y / top_end)))
    for y in range(bottom_start, SRC_H):
        draw.line([(0, y), (SRC_W, y)], fill=int(160 * (y - bottom_start) / (SRC_H - bottom_start)))
    img.paste(Image.new("RGB", img.size, (0, 0, 0)), (0, 0), mask)
    img.save(out, "JPEG", quality=92)


def _prepare_overlay(data: bytes, out: Path) -> bool:
    """Save the overlay as a 1080x1920 PNG when it has real transparency; False otherwise."""
    img = Image.open(io.BytesIO(data))
    has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
    if not has_alpha:
        logger.warning("Overlay has no alpha channel (fake transparency?) - rendering without overlay")
        return False
    img = img.convert("RGBA")
    transparent = img.getchannel("A").point(lambda v: 255 if v < 250 else 0).histogram()[255]
    if transparent / (img.width * img.height) < MIN_OVERLAY_TRANSPARENT_RATIO:
        logger.warning("Overlay is almost opaque - rendering without overlay")
        return False
    _cover(img, W, H).save(out, "PNG")
    return True


# ─── Subtitles (ASS) ───────────────────────────────────────────────────────

def _ts(sec: float) -> str:
    cs = int(round(max(0.0, sec) * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _esc(text: str) -> str:
    # Noto Sans has no emoji; keep letters, digits and basic punctuation.
    text = re.sub(r"[^\w\s'’!?.,:&%$#+@\-]", "", text or "")
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ").strip()


def _wrap(text: str, max_chars: int) -> str:
    lines, cur = [], ""
    for word in text.split():
        if cur and len(cur) + 1 + len(word) > max_chars:
            lines.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
    if cur:
        lines.append(cur)
    return r"\N".join(lines)


def _build_ass(script: dict, params: dict, intro: float, clip_dur: float, outro: float,
               commentary: dict | None, comment_start: float) -> str:
    total = intro + clip_dur + outro
    margin_v = int(params.get("caption_margin_v", 420))
    # ASS colours are &HAABBGGRR; yellow = &H0000D7FF.
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Hook,{FONT_NAME},100,&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,8,4,5,60,60,0,1
Style: Cta,{FONT_NAME},92,&H0000D7FF,&H0000D7FF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,8,4,5,60,60,0,1
Style: Caption,{FONT_NAME},68,&H0000D7FF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,6,2,2,80,80,{margin_v},1
Style: Handle,{FONT_NAME},46,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,4,2,2,60,60,{margin_v},1
Style: Credit,{FONT_NAME},28,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2,0,3,30,30,30,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    # The clip's own baked-in caption sits around 55% of the height: our texts stay above it and below it.
    pop = r"\fscx40\fscy40\t(0,170,\fscx112\fscy112)\t(170,280,\fscx100\fscy100)"
    events: list[str] = []

    hook = _esc(script["hook"]["on_screen"]).upper()
    if hook:
        events.append(f"Dialogue: 2,{_ts(0)},{_ts(intro - 0.05)},Hook,,0,0,0,,"
                      f"{{\\pos({W // 2},{int(H * 0.25)}){pop}\\fad(0,120)}}{_wrap(hook, 14)}")

    cta = _esc(script["cta"]["on_screen"]).upper()
    cta_start = intro + clip_dur
    if cta:
        events.append(f"Dialogue: 2,{_ts(cta_start + 0.1)},{_ts(total)},Cta,,0,0,0,,"
                      f"{{\\pos({W // 2},{int(H * 0.32)}){pop}}}{_wrap(cta, 16)}")
    handle = _esc(params.get("brand_handle", ""))
    if handle:
        events.append(f"Dialogue: 2,{_ts(cta_start + 0.4)},{_ts(total)},Handle,,0,0,0,,{{\\fad(150,0)}}{handle}")

    credit = _esc(params.get("credit_text", ""))
    if credit:
        events.append(f"Dialogue: 0,{_ts(intro)},{_ts(intro + clip_dur)},Credit,,0,0,0,,{credit}")

    # Word-synced captions for the commentary spoken over the clip.
    if commentary:
        words = commentary["words"]
        chunks: list[list[dict]] = []
        for w in words:
            if (not chunks or len(chunks[-1]) >= 4 or chunks[-1][-1]["w"][-1:] in ".,!?;:"
                    or w["start"] - chunks[-1][-1]["end"] > 0.35):
                chunks.append([w])
            else:
                chunks[-1].append(w)
        for ci, chunk in enumerate(chunks):
            c_start = comment_start + chunk[0]["start"]
            nxt = comment_start + chunks[ci + 1][0]["start"] if ci + 1 < len(chunks) else total
            c_end = min(nxt, comment_start + chunk[-1]["end"] + 0.4)
            parts = []
            for wi, w in enumerate(chunk):
                nxt_w = chunk[wi + 1]["start"] if wi + 1 < len(chunk) else w["end"]
                parts.append(f"{{\\k{max(1, int(round((nxt_w - w['start']) * 100)))}}}{_esc(w['w'])}")
            events.append(f"Dialogue: 1,{_ts(c_start)},{_ts(c_end)},Caption,,0,0,0,,{{\\fad(60,0)}}" + " ".join(parts))

    return header + "\n".join(events) + "\n"


# ─── ffmpeg ────────────────────────────────────────────────────────────────

async def _run(cmd: list[str], timeout: int = 1200) -> str:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"{cmd[0]} timed out after {timeout}s")
    if proc.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {stderr.decode(errors='ignore')[-1500:]}")
    return stdout.decode()


async def _probe(path: Path) -> dict:
    out = await _run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height",
                      "-of", "json", str(path)], timeout=60)
    info = json.loads(out)
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    return {
        "duration": float(info["format"]["duration"]),
        "width": int(video["width"]) if video else 0,
        "height": int(video["height"]) if video else 0,
        "has_video": video is not None,
        "has_audio": any(s.get("codec_type") == "audio" for s in streams),
    }


async def _extract_frame(clip: Path, out: Path, *, from_end: bool) -> None:
    seek = ["-sseof", "-0.15"] if from_end else ["-ss", "0.5"]
    await _run(["ffmpeg", "-y", "-hide_banner", *seek, "-i", str(clip), "-frames:v", "1", "-update", "1", str(out)],
               timeout=120)


def _zoom(frames: int, punch: bool) -> str:
    center = "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
    if punch:
        # Snap-in for the first ~0.4 s, then a slow push.
        return f"z='if(lt(on,12),1.3-0.25*on/12,1.05+0.07*(on-12)/{frames})':{center}"
    return f"z='1+0.08*on/{frames}':{center}"


async def render_reel(reel_id: str, spec: dict, workdir_root: Path = WORK_DIR) -> tuple[Path, float]:
    """Build the MP4 locally and return (path, duration_seconds). Caller uploads and cleans up."""
    params = spec.get("params") or {}
    script = spec["script"]
    segments = {s["id"]: s for s in spec["segments"]}
    if "hook" not in segments or "cta" not in segments:
        raise RuntimeError("voice segments must include 'hook' and 'cta'")

    work = workdir_root / f"nba_{reel_id}"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)

    clip_path = work / "clip.mp4"
    clip_path.write_bytes(await load(spec["clip_url"], max_bytes=MAX_CLIP_BYTES, allowed_types=VIDEO_TYPES))
    clip = await _probe(clip_path)
    if not clip["has_video"] or clip["duration"] < 1:
        raise RuntimeError("downloaded clip has no usable video")
    clip_dur = clip["duration"]

    voice_files: dict[str, Path] = {}
    for sid, seg in segments.items():
        p = work / f"voice_{sid}.mp3"
        p.write_bytes(await load(seg["key"], max_bytes=MAX_AUDIO_BYTES, allowed_types=AUDIO_TYPES))
        voice_files[sid] = p

    music_path = None
    if spec.get("music_key"):
        music_path = work / "music.mp3"
        music_path.write_bytes(await load(spec["music_key"], max_bytes=MAX_AUDIO_BYTES, allowed_types=AUDIO_TYPES))

    overlay_path = None
    if params.get("overlay_image_url"):
        try:
            data = await load(params["overlay_image_url"], max_bytes=MAX_IMAGE_BYTES, allowed_types=IMAGE_TYPES)
            candidate = work / "overlay.png"
            if await asyncio.to_thread(_prepare_overlay, data, candidate):
                overlay_path = candidate
        except Exception as exc:  # a missing/unusable overlay must never block the video
            logger.warning(f"Overlay skipped: {type(exc).__name__}: {exc}")

    # Intro still: the clip's thumbnail (the editors' chosen frame), first frame as fallback.
    intro_data = None
    if spec.get("thumbnail_url"):
        try:
            intro_data = await load(spec["thumbnail_url"], max_bytes=MAX_IMAGE_BYTES, allowed_types=IMAGE_TYPES)
        except Exception as exc:
            logger.warning(f"Thumbnail unavailable ({exc}); using first frame")
    if intro_data is None:
        first = work / "first.png"
        await _extract_frame(clip_path, first, from_end=False)
        intro_data = first.read_bytes()
    last = work / "last.png"
    await _extract_frame(clip_path, last, from_end=True)

    intro_img, outro_img = work / "intro.jpg", work / "outro.jpg"
    await asyncio.to_thread(_compose_still, intro_data, intro_img, 0, 0.12)
    await asyncio.to_thread(_compose_still, last.read_bytes(), outro_img, 28, 0.45)

    hook_dur, cta_dur = float(segments["hook"]["duration"]), float(segments["cta"]["duration"])
    intro = max(INTRO_MIN, VOICE_LEAD + hook_dur + 0.35)
    outro = max(OUTRO_MIN, VOICE_LEAD + cta_dur + 0.7)
    total = round(intro + clip_dur + outro, 3)
    comment = segments.get("commentary")
    comment_start = intro + 0.25

    (work / "subs.ass").write_text(
        _build_ass(script, params, intro, clip_dur, outro, comment, comment_start), encoding="utf-8",
    )

    # ── inputs ──
    inputs = ["-i", str(intro_img), "-i", str(clip_path), "-i", str(outro_img)]
    idx = {"clip": 1}
    n = 3
    for sid in ("hook", "commentary", "cta"):
        if sid in voice_files:
            inputs += ["-i", str(voice_files[sid])]
            idx[sid] = n
            n += 1
    if music_path:
        inputs += ["-i", str(music_path)]
        idx["music"] = n
        n += 1
    if overlay_path:
        inputs += ["-i", str(overlay_path)]
        idx["overlay"] = n
        n += 1

    # ── video ──
    f: list[str] = []
    intro_frames, outro_frames = math.ceil(intro * FPS), math.ceil(outro * FPS)
    for label, src, frames, punch in (("v0", 0, intro_frames, True), ("v2", 2, outro_frames, False)):
        f.append(f"[{src}:v]zoompan={_zoom(frames, punch)}:d={frames}:s={W}x{H}:fps={FPS},"
                 f"setsar=1,scale=in_range=pc:out_range=tv,format=yuv420p,trim=end_frame={frames},setpts=PTS-STARTPTS[{label}]")
    ratio = clip["width"] / max(1, clip["height"])
    if abs(ratio - W / H) < 0.02:
        f.append(f"[1:v]fps={FPS},scale={W}:{H}:flags=lanczos,setsar=1,format=yuv420p,setpts=PTS-STARTPTS[v1]")
    else:
        # Not 9:16: fit the clip over a blurred, zoomed copy of itself.
        f.append(
            f"[1:v]fps={FPS},split=2[ca][cb];"
            f"[ca]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=30:3[cbg];"
            f"[cb]scale={W}:{H}:force_original_aspect_ratio=decrease[cfg];"
            f"[cbg][cfg]overlay=(W-w)/2:(H-h)/2,setsar=1,format=yuv420p,setpts=PTS-STARTPTS[v1]"
        )
    f.append("[v0][v1][v2]concat=n=3:v=1:a=0[vc]")

    subs = f"subtitles=filename={work / 'subs.ass'}"
    if FONTS_DIR.is_dir():
        subs += f":fontsdir={FONTS_DIR}"
    if overlay_path:
        f.append(f"[vc][{idx['overlay']}:v]overlay=0:0:format=auto[vo]")
        f.append(f"[vo]{subs},format=yuv420p[vout]")
    else:
        f.append(f"[vc]{subs},format=yuv420p[vout]")

    # ── audio ──
    fmt = "aformat=sample_rates=48000:channel_layouts=stereo"
    voice_volume = float(params.get("voice_volume", 1.15))
    mix: list[str] = []
    if clip["has_audio"]:
        duck = float(params.get("duck_volume", 0.3))
        chain = f"[1:a]{fmt},volume={float(params.get('clip_volume', 1.0))}"
        if comment:
            chain += f",volume={duck}:enable='between(t,0,{0.25 + float(comment['duration']) + 0.3:.3f})'"
        f.append(f"{chain},adelay={int(intro * 1000)}:all=1,apad=whole_dur={total}[ca_out]")
        mix.append("[ca_out]")

    starts = {"hook": VOICE_LEAD, "commentary": comment_start, "cta": intro + clip_dur + VOICE_LEAD}
    vox_parts = []
    for sid in ("hook", "commentary", "cta"):
        if sid in idx:
            f.append(f"[{idx[sid]}:a]{fmt},volume={voice_volume},adelay={int(starts[sid] * 1000)}:all=1[va_{sid}]")
            vox_parts.append(f"[va_{sid}]")
    f.append(f"{''.join(vox_parts)}amix=inputs={len(vox_parts)}:duration=longest:normalize=0,apad=whole_dur={total}"
             + ("[vox_raw];[vox_raw]asplit=2[vox][sc]" if music_path else "[vox]"))
    mix.append("[vox]")

    if music_path:
        bgm = float(params.get("bgm_volume", 0.18))
        f.append(f"[{idx['music']}:a]{fmt},atrim=0:{total},asetpts=PTS-STARTPTS,apad=whole_dur={total},volume={bgm},"
                 f"afade=t=in:d=0.5,afade=t=out:st={max(0.0, total - 1.2):.3f}:d=1.2[bg]")
        f.append("[bg][sc]sidechaincompress=threshold=0.03:ratio=5:attack=15:release=300[bgd]")
        mix.append("[bgd]")

    f.append(f"{''.join(mix)}amix=inputs={len(mix)}:duration=longest:normalize=0,"
             f"loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000,atrim=0:{total}[aout]")

    out_path = work / "reel.mp4"
    cmd = [
        "ffmpeg", "-y", "-hide_banner", *inputs,
        "-filter_complex", ";".join(f),
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", FFMPEG_PRESET, "-crf", FFMPEG_CRF, "-maxrate", "10M", "-bufsize", "20M",
        "-profile:v", "high", "-level", "4.1", "-pix_fmt", "yuv420p", "-r", str(FPS), "-g", str(FPS * 2),
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart",
        "-t", f"{total:.3f}",
        str(out_path),
    ]
    logger.info(f"Reel {reel_id}: rendering intro {intro:.1f}s + clip {clip_dur:.1f}s + outro {outro:.1f}s "
                f"(commentary={bool(comment)}, music={bool(music_path)}, overlay={bool(overlay_path)})")
    await _run(cmd)
    return out_path, total
