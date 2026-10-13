from typing import Annotated

import redis
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from app.api.deps import get_redis
from app.models.schemas import ErrorCode, ErrorResponse, MediaInfo
from app.services.info import build_info
from app.services.ratelimit import check_rate_limit
from app.services.validators import validate_url
from app.worker.downloader import DownloadFailed, fetch_info

router = APIRouter(tags=["info"])

RedisDep = Annotated[redis.Redis, Depends(get_redis)]


# Plain `def`: yt-dlp and the DNS lookup block, so FastAPI runs this in a thread pool.
@router.get(
    "/info",
    response_model=MediaInfo,
    responses={400: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
def get_info(
    url: Annotated[str, Query(min_length=1, max_length=2048)],
    request: Request,
    r: RedisDep,
):
    client_id = request.client.host if request.client else "unknown"
    check_rate_limit(r, client_id, scope="info")  # separate counter from /jobs
    validate_url(url)

    try:
        raw = fetch_info(url)
    except DownloadFailed as exc:
        status = 400 if exc.code in (ErrorCode.INVALID_URL, ErrorCode.UNSUPPORTED_SITE) else 422
        return JSONResponse(
            status_code=status,
            content={"error": {"code": exc.code.value, "message": exc.message}},
        )
    return build_info(raw)
