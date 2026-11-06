"""Pick the clips most likely to go viral from a Storyteller clips response.

Pipeline: normalize -> hard filters -> heuristic score (engagement, Storyteller score, star power,
recency) -> LLM editorial score -> blend -> per-player cap -> top N, best first.
"""

import logging
import re
from datetime import datetime, timezone

from .config import chat_json

logger = logging.getLogger(__name__)

REQUIRED_CATEGORIES = ("highlights", "must-see-moments")
# Third-party / fan / partner content: other owners, higher claim risk. "*provided" covers team feeds.
# "nonhighlight" is NOT excluded: it also tags official human-interest clips (family moments, mutual respect).
EXCLUDED_CATEGORIES = ("ugc-clips", "fangenerated-content", "influencer", "partnerprovided")
EXCLUDED_CATEGORY_SUFFIX = "provided"
_THIRD_PARTY_DESCRIPTION = re.compile(r"\(\s*via\s+@|\bvia\s+@|(?<!\w)@\w{3,}", re.IGNORECASE)

DEFAULT_STARS = (
    "LeBron James", "Stephen Curry", "Kevin Durant", "Giannis Antetokounmpo", "Luka Dončić", "Luka Doncic",
    "Nikola Jokić", "Nikola Jokic", "Victor Wembanyama", "Anthony Edwards", "Jayson Tatum",
    "Shai Gilgeous-Alexander", "Ja Morant", "Jalen Brunson", "Joel Embiid", "Devin Booker", "Kyrie Irving",
    "Damian Lillard", "Kawhi Leonard", "Paul George", "Zion Williamson", "Cooper Flagg", "AJ Dybantsa",
    "Caleb Wilson", "Jimmy Butler", "Donovan Mitchell", "Trae Young", "Tyrese Haliburton", "Jaylen Brown",
    "Anthony Davis", "James Harden", "Kevin Love", "Bam Adebayo", "Paolo Banchero", "Chet Holmgren",
    "Scottie Barnes", "Karl-Anthony Towns", "Michael Porter Jr.", "Jaren Jackson Jr.", "Kobe Bryant",
)

_LLM_SYSTEM = (
    "You are the editor of a global NBA highlights Shorts/Reels channel. Rate every candidate clip from 1 to 10 "
    "on how likely a casual worldwide audience is to watch it to the end and share it. Judge ONLY from the "
    "metadata given (description, players, teams, engagement); never assume facts that are not there. "
    "Sub-scores (1-10): star_power (famous player or hyped rookie), decisive_moment (buzzer-beater, game-winner, "
    "poster dunk, chase-down block, ankle-breaker, deep three), emotional_story (underdog, milestone, first-ever, "
    "family, tribute, rivalry), hook (curiosity / instantly understandable without game context). "
    "Penalize clips that need game context, pure PR, bench/mascot filler and anything not basketball action or "
    "a strong human moment. "
    'Return JSON: {"scores":[{"i":<index>,"star_power":n,"decisive_moment":n,"emotional_story":n,"hook":n,'
    '"total":n,"reason":"<max 15 words>"}]}'
)


def normalize(raw: dict) -> dict | None:
    """Reduce an API clip to the fields we use; None when it has no mp4."""
    if not isinstance(raw, dict) or not raw.get("id"):
        return None
    url = raw.get("url") or ""
    if not isinstance(url, str) or ".mp4" not in url.lower() or not url.startswith("https://"):
        return None
    cats = [c for c in raw.get("clipCategories") or [] if isinstance(c, dict)]
    return {
        "clip_id": str(raw["id"]),
        "description": str(raw.get("description") or "").strip(),
        "mp4_url": url,
        "thumbnail_url": raw.get("thumbnailUrl") or raw.get("playcardUrl") or None,
        "duration": float(raw.get("duration") or 0),
        "like_count": int(raw.get("likeCount") or 0),
        "share_count": int(raw.get("shareCount") or 0),
        "source_score": float(raw.get("score") or 0),
        "players": [str(c.get("displayTitle") or c.get("name")) for c in cats if c.get("type") == "player"],
        "teams": [str(c.get("displayTitle") or c.get("name")) for c in cats if c.get("type") == "team"],
        "categories": [str(c) for c in raw.get("categories") or []],
        "created_at": raw.get("creationTime") or raw.get("publishedAt") or "",
    }


def _age_hours(clip: dict, now: datetime) -> float:
    ts = clip.get("created_at")
    if isinstance(ts, str) and ts:
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(0.0, (now - dt).total_seconds() / 3600)
        except ValueError:
            pass
    return 24.0


def reject_reason(clip: dict, p: dict, now: datetime, exclude_ids: set[str]) -> str | None:
    cats = set(clip["categories"])
    if clip["clip_id"] in exclude_ids:
        return "already_used"
    if not clip["description"]:
        return "no_description"
    if not cats & set(p["required_categories"]):
        return "category"
    excluded = set(p["excluded_categories"])
    if cats & excluded or any(c.endswith(EXCLUDED_CATEGORY_SUFFIX) for c in cats):
        return "third_party_category"
    if _THIRD_PARTY_DESCRIPTION.search(clip["description"]):
        return "third_party_description"
    if not p["min_duration"] <= clip["duration"] <= p["max_duration"]:
        return "duration"
    if _age_hours(clip, now) > p["lookback_hours"]:
        return "too_old"
    return None


def _percentiles(values: list[float]) -> list[float]:
    """Rank-based 0..1 percentile (ties share the average rank)."""
    n = len(values)
    if n <= 1:
        return [1.0] * n
    order = sorted(range(n), key=lambda i: values[i])
    out = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            out[order[k]] = ((i + j) / 2) / (n - 1)
        i = j + 1
    return out


def heuristic_scores(clips: list[dict], stars: set[str], now: datetime) -> None:
    """Adds clip['heuristic'] (0-10) and clip['signals'] in place."""
    likes = _percentiles([c["like_count"] for c in clips])
    shares = _percentiles([c["share_count"] for c in clips])
    source = _percentiles([c["source_score"] for c in clips])
    for i, c in enumerate(clips):
        engagement = 0.4 * likes[i] + 0.6 * shares[i]
        if any(p.lower() in stars for p in c["players"]):
            star = 1.0
        else:
            star = 0.5 if c["players"] else 0.0
        age = _age_hours(c, now)
        recency = 1.0 if age <= 12 else 0.7 if age <= 24 else 0.4 if age <= 48 else 0.1
        c["signals"] = {
            "engagement": round(engagement, 3), "source": round(source[i], 3),
            "star": star, "recency": recency,
        }
        c["heuristic"] = round(10 * (0.45 * engagement + 0.2 * source[i] + 0.2 * star + 0.15 * recency), 2)


async def llm_scores(clips: list[dict]) -> dict[int, dict]:
    lines = []
    for i, c in enumerate(clips):
        lines.append(
            f"[{i}] {c['description']} | players: {', '.join(c['players']) or '-'} | teams: "
            f"{', '.join(c['teams']) or '-'} | {c['duration']:.0f}s | likes {c['like_count']} shares {c['share_count']}"
        )
    try:
        data = await chat_json(
            [{"role": "system", "content": _LLM_SYSTEM}, {"role": "user", "content": "\n".join(lines)}],
            temperature=0.2,
        )
        return {int(s["i"]): s for s in data.get("scores", []) if isinstance(s, dict) and "i" in s}
    except Exception as exc:
        logger.warning("LLM scoring failed, using heuristic only: %s", exc)
        return {}


def _llm_total(s: dict) -> float:
    try:
        parts = [float(s[k]) for k in ("star_power", "decisive_moment", "emotional_story", "hook")]
        # The overall verdict counts double: sub-scores alone reward one-dimensional clips.
        total = float(s.get("total", sum(parts) / 4))
        return max(1.0, min(10.0, (sum(parts) / 4 + 2 * total) / 3))
    except (KeyError, TypeError, ValueError):
        return 5.0


def with_defaults(params: dict | None) -> dict:
    p = dict(params or {})
    lst = lambda v, d: [str(x).lower() for x in v] if isinstance(v, list) and v else list(d)  # noqa: E731
    num = lambda v, d: float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else d  # noqa: E731
    return {
        "max_clips": int(max(1, min(20, num(p.get("max_clips"), 10)))),
        "min_duration": num(p.get("min_duration"), 6),
        "max_duration": num(p.get("max_duration"), 24),
        "lookback_hours": num(p.get("lookback_hours"), 72),
        "max_per_player": int(max(1, num(p.get("max_per_player"), 3))),
        "llm_candidates": int(max(5, min(60, num(p.get("llm_candidates"), 30)))),
        "required_categories": lst(p.get("required_categories"), REQUIRED_CATEGORIES),
        "excluded_categories": lst(p.get("excluded_categories"), EXCLUDED_CATEGORIES),
        "stars": {s.lower() for s in (p.get("stars") or DEFAULT_STARS) if isinstance(s, str)},
    }


async def select(raw_clips: list[dict], exclude_ids: list[str], params: dict | None = None,
                 now: datetime | None = None) -> dict:
    p = with_defaults(params)
    now = now or datetime.now(timezone.utc)
    used = {str(i) for i in exclude_ids or []}

    clips, rejected, seen = [], {}, set()
    for raw in raw_clips:
        clip = normalize(raw)
        if clip is None:
            rejected["no_mp4"] = rejected.get("no_mp4", 0) + 1
            continue
        if clip["clip_id"] in seen:
            continue
        seen.add(clip["clip_id"])
        reason = reject_reason(clip, p, now, used)
        if reason:
            rejected[reason] = rejected.get(reason, 0) + 1
        else:
            clips.append(clip)

    if not clips:
        return {"count": 0, "selected": [], "candidates": 0, "rejected": rejected}

    heuristic_scores(clips, p["stars"], now)
    clips.sort(key=lambda c: c["heuristic"], reverse=True)
    shortlist = clips[: p["llm_candidates"]]
    llm = await llm_scores(shortlist)

    for i, c in enumerate(shortlist):
        verdict = llm.get(i)
        llm_total = _llm_total(verdict) if verdict else None
        c["llm"] = llm_total
        c["llm_detail"] = verdict or {}
        c["total"] = round(0.5 * c["heuristic"] + 0.5 * llm_total, 2) if llm_total is not None else c["heuristic"]
    shortlist.sort(key=lambda c: c["total"], reverse=True)

    selected, per_player = [], {}
    for c in shortlist:
        primary = c["players"][0] if c["players"] else None
        if primary and per_player.get(primary, 0) >= p["max_per_player"]:
            continue
        if primary:
            per_player[primary] = per_player.get(primary, 0) + 1
        selected.append(c)
        if len(selected) >= p["max_clips"]:
            break

    out = []
    for rank, c in enumerate(selected, start=1):
        detail = c["llm_detail"]
        out.append({
            "rank": rank,
            "clip_id": c["clip_id"], "description": c["description"], "mp4_url": c["mp4_url"],
            "thumbnail_url": c["thumbnail_url"], "duration": c["duration"],
            "like_count": c["like_count"], "share_count": c["share_count"], "source_score": c["source_score"],
            "players": c["players"], "teams": c["teams"], "categories": c["categories"],
            "score": {
                "total": c["total"], "heuristic": c["heuristic"], "llm": c["llm"],
                "llm_reason": detail.get("reason", ""), "signals": c["signals"],
                "llm_detail": {k: detail.get(k) for k in ("star_power", "decisive_moment", "emotional_story", "hook")},
            },
        })
    return {"count": len(out), "selected": out, "candidates": len(clips), "rejected": rejected}
