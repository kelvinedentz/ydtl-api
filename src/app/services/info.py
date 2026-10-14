from datetime import date, datetime, timezone
from typing import Any, Literal

from app.config import get_settings
from app.models.schemas import AudioOption, MediaInfo, VideoQuality

# Formats we re-encode to.
AudioFormat = Literal["mp3", "m4a"]

STANDARD_HEIGHTS = (1080, 720, 480, 360)
AUDIO_BITRATES: dict[AudioFormat, int] = {
    "mp3": 192,
    "m4a": 256,
}


def _res(f: Any) -> int:
    """YouTube-style resolution: the shorter side, so vertical video is labelled correctly."""
    w, h = f.get("width"), f["height"]
    return min(w, h) if w else h


def _size(f: Any, duration: int | None = None) -> int | None:
    v = f.get("filesize") or f.get("filesize_approx")
    if not v and duration and f.get("tbr"):
        v = f["tbr"] * 1000 / 8 * duration  # estimate from bitrate
    return int(v) if v else None


def _has(f: Any, key: str) -> bool:
    return (f.get(key) or "none") != "none"


def _best_video(videos: list[Any], max_res: int | None = None) -> Any:
    pool = [f for f in videos if max_res is None or _res(f) <= max_res]
    return max(
        pool,
        key=lambda f: (_res(f), f.get("ext") == "mp4", f.get("tbr") or 0),
        default=None,
    )


def _pub_date(raw: Any) -> date | None:
    """Publication date: upload_date ('YYYYMMDD'), else release_date, else a Unix timestamp."""
    s = raw.get("upload_date") or raw.get("release_date")
    if s:
        try:
            return datetime.strptime(str(s), "%Y%m%d").date()
        except ValueError:
            pass
    ts = raw.get("timestamp") or raw.get("release_timestamp")
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, tz=timezone.utc).date()
    return None


def build_info(raw: Any) -> MediaInfo:
    formats = raw.get("formats") or []
    videos = [f for f in formats if _has(f, "vcodec") and f.get("height")]
    audios = [f for f in formats if _has(f, "acodec") and not _has(f, "vcodec")]
    duration = int(raw["duration"]) if raw.get("duration") else None

    best_audio = max(
        audios, key=lambda f: f.get("abr") or f.get("tbr") or 0, default=None
    )
    audio_size = _size(best_audio, duration) if best_audio else None
    limit = get_settings().max_file_size_mb * 1024 * 1024

    video: list[VideoQuality] = []
    if videos:
        max_res = _res(_best_video(videos))
        targets: list[tuple[str, int | None]] = [("best", None)]
        targets += [(f"{h}p", h) for h in STANDARD_HEIGHTS if h <= max_res]
        for label, h in targets:
            f = _best_video(videos, h)
            if f is None:
                continue
            size = _size(f, duration)
            if size is not None and not _has(f, "acodec"):
                size += audio_size or 0  # video-only stream: add the audio track
            video.append(
                VideoQuality(
                    quality=label,
                    width=f.get("width"),
                    height=f["height"],
                    ext="mp4",
                    filesize=size,
                    within_limit=None if size is None else size <= limit,
                )
            )

    audio = [
        AudioOption(
            format=fmt,
            bitrate_kbps=kbps,
            filesize=int(duration * kbps * 1000 / 8) if duration else None,
        )
        for fmt, kbps in AUDIO_BITRATES.items()
    ]

    thumbnail = raw.get("thumbnail")
    if not thumbnail and raw.get("thumbnails"):
        thumbnail = raw["thumbnails"][-1].get("url")

    return MediaInfo(
        title=raw.get("title") or "Untitled",
        uploader=raw.get("uploader") or raw.get("channel"),
        view_count=raw.get("view_count"),
        pub_date=_pub_date(raw),
        duration=duration,
        thumbnail=thumbnail,
        webpage_url=raw.get("webpage_url") or raw.get("original_url") or "",
        site=raw.get("extractor_key"),
        max_file_size=limit,
        video=video,
        audio=audio,
    )
