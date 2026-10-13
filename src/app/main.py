from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.routes.routes_health import router as health_router
from app.api.routes.routes_info import router as info_router
from app.api.routes.routes_jobs import router as jobs_router
from app.services.ratelimit import RateLimited
from app.services.validators import ValidationError

app = FastAPI(title="ydtl-api")
app.include_router(health_router)
app.include_router(jobs_router)
app.include_router(info_router)


@app.exception_handler(ValidationError)
async def validation_error_handler(request: Request, exc: ValidationError):
    return JSONResponse(
        status_code=400,
        content={"error": {"code": exc.code.value, "message": exc.message}},
    )


@app.exception_handler(RateLimited)
async def rate_limited_handler(request: Request, exc: RateLimited):
    return JSONResponse(
        status_code=429,
        headers={"Retry-After": str(exc.retry_after)},
        content={"error": {"code": "rate_limited", "message": "Too many requests."}},
    )
