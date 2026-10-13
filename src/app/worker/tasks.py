import logging
import shutil
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import redis
from rq import Queue

from app.config import get_settings
from app.models.schemas import ErrorCode, JobState, MediaType
from app.services import jobs, storage
from app.worker.downloader import DownloadFailed, download

log = logging.getLogger(__name__)

# Referenced by dotted path so this file doesn't import cleanup.py.
DELETE_TASK = "app.worker.cleanup.delete_job_task"


def _safe_delete(key: str) -> None:
    try:
        storage.delete_object(key)
    except Exception:
        log.exception("Could not delete %s (the sweeper will retry)", key)


def _schedule_delete(r: redis.Redis, job_id: str) -> None:
    """Layer 2: delete the file FILE_TTL_SECONDS after upload.

    Never fatal: if scheduling fails, the sweeper still removes the file.
    """
    try:
        Queue(connection=r).enqueue_in(
            timedelta(seconds=get_settings().file_ttl_seconds),
            DELETE_TASK,
            job_id,
            result_ttl=0,
        )
    except Exception:
        log.exception("Could not schedule delete for job %s", job_id)


def process_job(job_id: str) -> None:
    """Run one job from queued to ready (or failed). Runs inside the RQ worker."""
    s = get_settings()
    r = redis.Redis.from_url(s.redis_url)  # the worker's own connection
    tmp_dir = Path(tempfile.mkdtemp(prefix=f"ydtl-{job_id}-"))
    uploaded_key: str | None = None

    try:
        record = jobs.get_job(r, job_id)
        if record is None or record.get("state") != JobState.QUEUED:
            return  # record expired, or this job already ran

        jobs.update_job(r, job_id, state=JobState.PROCESSING, progress=0)

        result = download(
            url=record["url"],
            dest_dir=tmp_dir,
            media_type=MediaType(record["type"]),
            quality=record["quality"],
            audio_format=record["audio_format"],
            on_progress=lambda pct: jobs.update_job(r, job_id, progress=pct),
        )

        key = storage.make_key(job_id, result.path.name)
        storage.upload_file(result.path, key)
        uploaded_key = key

        signed_url = storage.generate_signed_url(key, filename=result.download_name)
        link_expires = datetime.now(UTC) + timedelta(seconds=s.signed_url_ttl_seconds)

        _schedule_delete(r, job_id)

        jobs.update_job(
            r,
            job_id,
            state=JobState.READY,
            progress=100,
            title=result.title,
            file_size=result.size,
            object_key=key,
            filename=result.download_name,
            download_url=signed_url,
            expires_at=link_expires.isoformat(),
        )

    except DownloadFailed as exc:
        jobs.mark_failed(r, job_id, exc.code, exc.message)
    except Exception:
        log.exception("Job %s failed", job_id)
        if uploaded_key:
            _safe_delete(uploaded_key)
        jobs.mark_failed(
            r,
            job_id,
            ErrorCode.DOWNLOAD_FAILED,
            "Something went wrong while processing the video.",
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        r.close()
