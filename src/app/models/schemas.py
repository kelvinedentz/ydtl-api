from datetime import datetime, date
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl


class JobState(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    READY = "ready"
    EXPIRED = "expired"
    FAILED = "failed"


class MediaType(StrEnum):
    VIDEO = "video"
    AUDIO = "audio"


class ErrorCode(StrEnum):
    INVALID_URL = "invalid_url"
    UNSUPPORTED_SITE = "unsupported_site"
    TOO_LARGE = "too_large"
    RATE_LIMITED = "rate_limited"
    DOWNLOAD_FAILED = "download_failed"
    NOT_FOUND = "not_found"
    EXPIRED = "expired"


# ---------- Requests ----------


class JobCreate(BaseModel):
    url: HttpUrl
    type: MediaType = MediaType.VIDEO
    quality: Literal["best", "1080p", "720p", "480p", "360p"] = "best"  # video only
    audio_format: Literal["mp3", "m4a", "opus"] = "mp3"  # audio only


# ---------- Responses ----------


class ErrorDetail(BaseModel):
    code: ErrorCode
    message: str


class ErrorResponse(BaseModel):
    error: ErrorDetail


class JobCreated(BaseModel):
    job_id: str
    state: JobState


class JobStatus(BaseModel):
    job_id: str
    state: JobState
    progress: int = Field(default=0, ge=0, le=100)
    title: str | None = None
    file_size: int | None = None  # bytes
    download_url: str | None = None  # only when state == ready
    expires_at: datetime | None = None  # only when state == ready
    error: ErrorDetail | None = None  # only when state == failed


class VideoQuality(BaseModel):
    quality: str
    width: int | None = None
    height: int
    ext: str
    filesize: int | None = None
    within_limit: bool | None = None


class AudioOption(BaseModel):
    format: Literal["mp3", "m4a", "opus"]  # usable as JobCreate.audio_format
    bitrate_kbps: int
    filesize: int | None = None  # estimate from duration x bitrate


class MediaInfo(BaseModel):
    title: str
    uploader: str | None = None
    duration: int | None = None
    view_count: int | None = None
    pub_date: date | None = None
    thumbnail: str | None = None
    webpage_url: str
    site: str | None = None
    max_file_size: int
    video: list[VideoQuality]
    audio: list[AudioOption]
