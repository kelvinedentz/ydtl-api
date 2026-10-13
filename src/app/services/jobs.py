import uuid
from datetime import UTC, datetime
from typing import Any

import redis
from redis.typing import EncodableT, FieldT

from app.config import get_settings
from app.models.schemas import (
    ErrorCode,
    ErrorDetail,
    JobCreate,
    JobState,
    JobStatus,
)


def _key(job_id: str) -> str:
    return f"job:{job_id}"


def _text(value: bytes | str) -> str:
    """Redis returns bytes on our raw client; accept str too."""
    return value.decode() if isinstance(value, bytes) else value


def create_job(r: redis.Redis, req: JobCreate) -> str:
    """Create a queued job record and return its id."""
    job_id = uuid.uuid4().hex
    record: dict[FieldT, EncodableT] = {
        "job_id": job_id,
        "state": JobState.QUEUED.value,
        "progress": 0,
        "url": str(req.url),
        "type": req.type.value,
        "quality": req.quality,
        "audio_format": req.audio_format,
        "created_at": datetime.now(UTC).isoformat(),
    }
    pipe = r.pipeline()
    pipe.hset(_key(job_id), mapping=record)
    pipe.expire(_key(job_id), get_settings().job_record_ttl_seconds)
    pipe.execute()
    return job_id


def get_job(r: redis.Redis, job_id: str) -> dict[str, str] | None:
    """Return the raw record (all values are strings) or None if unknown/expired."""
    raw = r.hgetall(_key(job_id))
    if not raw:
        return None
    return {_text(k): _text(v) for k, v in raw.items()}


def update_job(r: redis.Redis, job_id: str, **fields: Any) -> None:
    """Merge fields into the record. None values are skipped.

    Refreshes the record TTL on every update, so a long-running job
    can't lose its record mid-download.
    """
    mapping: dict[FieldT, EncodableT] = {
        k: (v.value if hasattr(v, "value") else v) for k, v in fields.items() if v is not None
    }
    if not mapping:
        return
    pipe = r.pipeline()
    pipe.hset(_key(job_id), mapping=mapping)
    pipe.expire(_key(job_id), get_settings().job_record_ttl_seconds)
    pipe.execute()


def mark_failed(r: redis.Redis, job_id: str, code: ErrorCode, message: str) -> None:
    update_job(
        r,
        job_id,
        state=JobState.FAILED,
        error_code=code,
        error_message=message,
    )


def mark_expired(r: redis.Redis, job_id: str) -> None:
    """Called after the file is deleted. Clears the download link."""
    if get_job(r, job_id) is None:
        return
    update_job(r, job_id, state=JobState.EXPIRED)
    r.hdel(_key(job_id), "download_url", "expires_at")


def to_status(record: dict[str, str]) -> JobStatus:
    """Convert a raw Redis record into the public API response."""
    error = None
    if record.get("error_code"):
        error = ErrorDetail(
            code=ErrorCode(record["error_code"]),
            message=record.get("error_message", ""),
        )
    expires_raw = record.get("expires_at")
    return JobStatus(
        job_id=record["job_id"],
        state=JobState(record["state"]),
        progress=int(record.get("progress", 0)),
        title=record.get("title"),
        file_size=int(record["file_size"]) if record.get("file_size") else None,
        download_url=record.get("download_url"),
        expires_at=datetime.fromisoformat(expires_raw) if expires_raw else None,
        error=error,
    )
