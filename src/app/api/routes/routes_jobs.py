import logging
from typing import Annotated

import redis
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from rq import Queue

from app.api.deps import get_redis
from app.config import get_settings
from app.models.schemas import (
    ErrorCode,
    ErrorResponse,
    JobCreate,
    JobCreated,
    JobState,
    JobStatus,
)
from app.services import jobs, retention
from app.services.ratelimit import check_rate_limit
from app.services.validators import validate_url

log = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs", tags=["jobs"])

# Referenced by dotted path so the API never imports yt-dlp.
PROCESS_TASK = "app.worker.tasks.process_job"

RedisDep = Annotated[redis.Redis, Depends(get_redis)]


def _error(status: int, code: ErrorCode, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code.value, "message": message}},
    )


# These are plain `def` routes on purpose: validate_url does a blocking DNS
# lookup, and FastAPI runs sync routes in a thread pool.


@router.post("", status_code=202, response_model=JobCreated)
def create_job(body: JobCreate, request: Request, r: RedisDep):
    client_id = request.client.host if request.client else "unknown"
    check_rate_limit(r, client_id)  # raises RateLimited -> 429
    validate_url(str(body.url))  # raises ValidationError -> 400

    job_id = jobs.create_job(r, body)
    try:
        Queue(connection=r).enqueue(
            PROCESS_TASK,
            job_id,
            job_timeout=get_settings().job_timeout_seconds,
            result_ttl=0,  # job state lives in our own Redis hash
        )
    except redis.RedisError:
        log.exception("Could not enqueue job %s", job_id)
        r.delete(f"job:{job_id}")
        return _error(503, ErrorCode.DOWNLOAD_FAILED, "Service busy, try again shortly.")

    return JobCreated(job_id=job_id, state=JobState.QUEUED)


@router.get(
    "/{job_id}",
    response_model=JobStatus,
    responses={404: {"model": ErrorResponse}},
)
def get_job(job_id: str, r: RedisDep):
    record = jobs.get_job(r, job_id)
    if record is None:
        return _error(404, ErrorCode.NOT_FOUND, "Job not found or expired.")
    return jobs.to_status(record)


@router.post(
    "/{job_id}/complete",
    status_code=204,
    responses={404: {"model": ErrorResponse}},
)
def complete_job(job_id: str, r: RedisDep):
    """Client ack after downloading: delete the file now (layer 1).

    Safe to call twice. A storage error is logged but not shown to the
    client, because the delayed delete and the sweeper will retry.
    """
    if jobs.get_job(r, job_id) is None:
        return _error(404, ErrorCode.NOT_FOUND, "Job not found or expired.")
    try:
        retention.delete_job_file(r, job_id)
    except Exception:
        log.exception("Early delete failed for job %s", job_id)
    return Response(status_code=204)
