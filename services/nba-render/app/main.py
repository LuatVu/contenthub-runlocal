"""Service render NBA short (thay cho ECS Fargate task services/nba/render).

Chạy thường trú trong docker compose, nhận job từ service nba-content:
  POST /jobs        {job_id}  -> xếp job, trả {job_id, state}
  GET  /jobs/{id}             -> {job_id, state, ...}; 404 nếu service không biết job này
  GET  /jobs                  -> toàn bộ hàng đợi (xem bằng mắt khi vận hành)
  GET  /health
Mọi endpoint (trừ /health) cần header X-Api-Key = APP_API_KEY.
"""

import hmac
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .jobs import queue
from .render import WORK_DIR

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("nba-render")


def _unauthorized(request: Request) -> JSONResponse | None:
    expected = os.environ.get("APP_API_KEY") or ""
    if not expected:
        return JSONResponse(status_code=503, content={"error": "nba-render not configured"})
    if not hmac.compare_digest((request.headers.get("x-api-key") or "").encode(), expected.encode()):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    queue.start()
    try:
        yield
    finally:
        await queue.stop()


app = FastAPI(title="nba-render", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "nba-render"}


@app.post("/jobs")
async def create_job(request: Request) -> JSONResponse:
    denied = _unauthorized(request)
    if denied:
        return denied
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("request body must be a JSON object")
        job = queue.submit(str(body.get("job_id") or ""))
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    return JSONResponse(content=job.view())


@app.get("/jobs")
async def list_jobs(request: Request) -> JSONResponse:
    denied = _unauthorized(request)
    if denied:
        return denied
    return JSONResponse(content=queue.snapshot())


@app.get("/jobs/{job_id}")
async def get_job(job_id: str, request: Request) -> JSONResponse:
    denied = _unauthorized(request)
    if denied:
        return denied
    job = queue.get(job_id)
    if job is None:
        # nba-content sẽ coi đây là "job đã mất" (service restart) và báo reel failed.
        return JSONResponse(status_code=404, content={"error": "unknown job", "job_id": job_id})
    return JSONResponse(content=job.view())
