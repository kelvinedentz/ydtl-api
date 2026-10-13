from datetime import UTC, datetime, timedelta

import redis

from app.config import get_settings
from app.models.schemas import JobState
from app.services import jobs, storage


def compute_expiry(now: datetime | None = None) -> datetime:
    """When a file uploaded at `now` must be gone."""
    now = now or datetime.now(UTC)
    return now + timedelta(seconds=get_settings().file_ttl_seconds)


def job_id_from_key(key: str) -> str | None:
    """Extract the job id from 'media/{job_id}/{filename}'."""
    parts = key.split("/", 2)
    if len(parts) == 3 and parts[0] == "media" and parts[1]:
        return parts[1]
    return None


def delete_job_file(r: redis.Redis, job_id: str) -> bool:
    """Delete a job's file and mark the job expired.

    Safe to call repeatedly, so the client ack, the delayed delete, and the
    sweeper can overlap. Returns True if the job record was changed.
    Jobs still queued or processing are left alone.
    """
    record = jobs.get_job(r, job_id)
    if record is None:
        return False  # record already expired out of Redis

    state = record.get("state")
    if state in (JobState.QUEUED, JobState.PROCESSING):
        return False

    key = record.get("object_key")
    if key:
        storage.delete_object(key)  # no-op if the object is already gone

    if state == JobState.READY:
        jobs.mark_expired(r, job_id)
        return True
    return False


def delete_key(r: redis.Redis, key: str) -> None:
    """Used by the sweeper: delete by object key, then sync the job record."""
    storage.delete_object(key)
    job_id = job_id_from_key(key)
    if job_id:
        record = jobs.get_job(r, job_id)
        if record and record.get("state") == JobState.READY:
            jobs.mark_expired(r, job_id)
