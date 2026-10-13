from typing import Annotated

import redis
from fastapi import APIRouter, Depends, Response

from app.api.deps import get_redis
from app.services import storage

router = APIRouter(tags=["health"])


@router.get("/health")
def health(response: Response, r: Annotated[redis.Redis, Depends(get_redis)]):
    checks: dict[str, str] = {}

    try:
        r.ping()
        checks["redis"] = "ok"
    except redis.RedisError:
        checks["redis"] = "error"

    checks["storage"] = "ok" if storage.check_connection() else "error"

    healthy = all(v == "ok" for v in checks.values())
    if not healthy:
        response.status_code = 503

    return {"status": "ok" if healthy else "degraded", "checks": checks}
