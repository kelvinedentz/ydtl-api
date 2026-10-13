import logging
from datetime import UTC, datetime, timedelta

import redis

from app.config import get_settings
from app.services import retention, storage

log = logging.getLogger(__name__)


def delete_job_task(job_id: str) -> None:
    """Layer 2: runs in the RQ worker FILE_TTL_SECONDS after upload.

    Storage errors are not caught, so RQ records the failure visibly.
    The sweeper retries the same file on its next pass.
    """
    r = redis.Redis.from_url(get_settings().redis_url)
    try:
        retention.delete_job_file(r, job_id)
    finally:
        r.close()


def sweep_expired(r: redis.Redis) -> int:
    """Layer 3: delete every object under media/ older than FILE_TTL_SECONDS.

    Works from the storage listing, so it still cleans up after a worker
    crash or a Redis restart. Returns the number of objects deleted.
    """
    cutoff = datetime.now(UTC) - timedelta(seconds=get_settings().file_ttl_seconds)
    deleted = 0

    for key, last_modified in storage.list_objects("media/"):
        if last_modified > cutoff:
            continue
        try:
            retention.delete_key(r, key)
            deleted += 1
        except Exception:
            # One bad object must not stop the rest; the next pass retries it.
            log.exception("Sweeper could not delete %s", key)

    if deleted:
        log.info("Sweeper deleted %d expired object(s)", deleted)
    return deleted


def sweep_once() -> int:
    """Entry point for the sweeper loop: own connection, one pass."""
    r = redis.Redis.from_url(get_settings().redis_url)
    try:
        return sweep_expired(r)
    finally:
        r.close()
