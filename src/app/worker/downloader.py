import logging
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError, match_filter_func, sanitize_filename

from app.config import get_settings
from app.models.schemas import ErrorCode, MediaType

log = logging.getLogger(__name__)

_ANSI = re.compile(r"\x1b\[[0-9;]*m")

# Mounted from the host by docker-compose. Optional: without it, yt-dlp runs without cookies.
COOKIE_FILE = Path(os.getenv("YTDLP_COOKIES", "/app/cookies.txt"))


class DownloadFailed(Exception):
    """Raised for any failed download. tasks.py stores code and message on the job."""

    def __init__(self, code: ErrorCode, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class DownloadResult:
    path: Path  # file on disk, named {video_id}.{ext}
    title: str
    download_name: str  # friendly name for the browser's save dialog
    size: int  # bytes


@contextmanager
def _cookie_opts() -> Iterator[dict[str, Any]]:
    """Yield yt-dlp options that point at a private copy of the cookie file.

    yt-dlp rewrites its cookie file when it closes. The API (several threads)
    and the worker would all write to the same mounted file, so each call gets
    its own temporary copy instead. That also means the mount can be read-only.
    Refreshed cookies are not saved back: re-export cookies.txt if they expire.
    """
    if not COOKIE_FILE.is_file():
        yield {}
        return
    with tempfile.TemporaryDirectory(prefix="ytdlp-cookies-") as tmp:
        copy = Path(tmp) / "cookies.txt"
        try:
            shutil.copyfile(COOKIE_FILE, copy)
        except OSError:
            log.warning("Cannot read cookie file %s, continuing without it", COOKIE_FILE)
            yield {}
            return
        yield {"cookiefile": str(copy)}


def _video_format(quality: str) -> str:
    if quality == "best":
        return "bv*[ext=mp4]+ba[ext=m4a]/bv*+ba/b"
    h = int(quality.removesuffix("p"))
    return f"bv*[height<={h}][ext=mp4]+ba[ext=m4a]/bv*[height<={h}]+ba/b[height<={h}]/b"


def _clean_error(exc: Exception) -> str:
    text = _ANSI.sub("", str(exc)).strip().splitlines()[0] if str(exc) else ""
    text = text.removeprefix("ERROR: ")
    return text[:200] or "Download failed."


def _find_output(info: Any, dest_dir: Path) -> Path | None:
    for d in info.get("requested_downloads") or []:
        p = d.get("filepath")
        if p and Path(p).exists():
            return Path(p)
    # Fallback: the largest finished file in the temp dir.
    files = [p for p in dest_dir.iterdir() if p.is_file() and p.suffix not in (".part", ".ytdl")]
    return max(files, key=lambda p: p.stat().st_size) if files else None


def download(
    url: str,
    dest_dir: Path,
    media_type: MediaType,
    quality: str = "best",
    audio_format: str = "mp3",
    on_progress: Callable[[int], None] | None = None,
) -> DownloadResult:
    """Download one video or audio file into dest_dir. Blocking."""
    limit = get_settings().max_file_size_mb * 1024 * 1024
    state = {"too_large": False, "last_pct": -1}

    def hook(d: dict[str, Any]) -> None:
        if d.get("status") != "downloading":
            return
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        done = d.get("downloaded_bytes") or 0
        # Abort as soon as the size is known, or when it grows past the cap.
        if done > limit or (total and total > limit):
            state["too_large"] = True
            raise DownloadFailed(ErrorCode.TOO_LARGE, "File exceeds the size limit.")
        if total and on_progress:
            pct = min(99, int(done * 100 / total))
            # Only report increases, and not every single percent.
            if pct >= state["last_pct"] + 2:
                state["last_pct"] = pct
                on_progress(pct)

    opts: dict[str, Any] = {
        "outtmpl": str(dest_dir / "%(id)s.%(ext)s"),
        "noplaylist": True,
        "playlist_items": "1",
        "match_filter": match_filter_func("!is_live"),
        "progress_hooks": [hook],
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "socket_timeout": 30,
        "retries": 3,
        "restrictfilenames": True,
    }

    if media_type == MediaType.AUDIO:
        # Take the best available source (YouTube's best audio is Opus ~160k,
        # better than the AAC ~128k stream), then encode to the target codec.
        opts["format"] = "bestaudio/best"
        if audio_format == "m4a":
            opts["postprocessors"] = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "m4a",
                    "preferredquality": "256",
                }
            ]
        else:
            opts["postprocessors"] = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": audio_format,
                    "preferredquality": "192",
                }
            ]
    else:
        opts["format"] = _video_format(quality)
        opts["merge_output_format"] = "mp4"

    try:
        # The cookie copy lives until YoutubeDL has closed (it saves cookies on close).
        with _cookie_opts() as cookie_opts, YoutubeDL(cast(Any, {**opts, **cookie_opts})) as ydl:
            info = ydl.extract_info(url, download=True)
    except DownloadFailed:
        raise
    except DownloadError as exc:
        if state["too_large"]:
            raise DownloadFailed(ErrorCode.TOO_LARGE, "File exceeds the size limit.") from exc
        msg = _clean_error(exc)
        code = ErrorCode.UNSUPPORTED_SITE if "Unsupported URL" in msg else ErrorCode.DOWNLOAD_FAILED
        raise DownloadFailed(code, msg) from exc

    if not info:
        raise DownloadFailed(ErrorCode.DOWNLOAD_FAILED, "Nothing was downloaded.")

    entry: Any = info
    entries = info.get("entries")
    if entries:
        entry = next(iter(cast(Any, entries)), None)
        if entry is None:
            raise DownloadFailed(ErrorCode.DOWNLOAD_FAILED, "Nothing was downloaded.")

    path = _find_output(entry, dest_dir)
    if path is None:
        raise DownloadFailed(ErrorCode.DOWNLOAD_FAILED, "No file was produced.")

    size = path.stat().st_size
    if size > limit:  # final check, covers merged and converted files
        path.unlink(missing_ok=True)
        raise DownloadFailed(ErrorCode.TOO_LARGE, "File exceeds the size limit.")

    title = entry.get("title") or "download"
    download_name = f"{sanitize_filename(title)[:100]}{path.suffix}"
    return DownloadResult(path=path, title=title, download_name=download_name, size=size)


def fetch_info(url: str) -> Any:
    """Extract metadata without downloading. Blocking. Returns yt-dlp's info dict."""
    opts: dict[str, Any] = {
        "noplaylist": True,
        "playlist_items": "1",
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 15,
        "retries": 1,
    }
    try:
        with _cookie_opts() as cookie_opts, YoutubeDL(cast(Any, {**opts, **cookie_opts})) as ydl:
            info = ydl.extract_info(url, download=False)
    except DownloadError as exc:
        msg = _clean_error(exc)
        code = ErrorCode.UNSUPPORTED_SITE if "Unsupported URL" in msg else ErrorCode.DOWNLOAD_FAILED
        raise DownloadFailed(code, msg) from exc

    if not info:
        raise DownloadFailed(ErrorCode.DOWNLOAD_FAILED, "No information found.")

    entry: Any = info
    entries = info.get("entries")
    if entries:
        entry = next(iter(cast(Any, entries)), None)
        if entry is None:
            raise DownloadFailed(ErrorCode.DOWNLOAD_FAILED, "No information found.")

    if entry.get("is_live"):
        raise DownloadFailed(ErrorCode.INVALID_URL, "Live streams are not supported.")
    return entry