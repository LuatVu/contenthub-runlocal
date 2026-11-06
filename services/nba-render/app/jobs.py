"""Hàng đợi job render trong tiến trình (thay cho ECS RunTask + Fargate Spot).

Một PC chỉ nên render một video một lúc (ffmpeg đã dùng hết CPU), nên mặc định
RENDER_CONCURRENCY = 1 và các job khác xếp hàng chờ.

Hợp đồng với service nba-content giữ nguyên kiểu "qua file" của bản cloud:
  đọc  <MEDIA_DIR>/nba/<job_id>/render.json
  ghi  <MEDIA_DIR>/nba/<job_id>/reel.mp4 + render_result.json ({video_key, duration} | {error})
Nhờ vậy kết quả vẫn còn sau khi service restart, còn trạng thái queued/running thì không.
"""

import asyncio
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field

from . import render
from .storage import put_file, read_json, write_json

logger = logging.getLogger(__name__)

_ID = re.compile(r"^[0-9a-fA-F-]{36}$")
MEDIA_PREFIX = "nba"
CONCURRENCY = max(1, int(os.environ.get("RENDER_CONCURRENCY", "1")))


@dataclass
class Job:
    job_id: str
    state: str = "queued"          # queued | running | done | failed
    queued_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    video_key: str | None = None
    duration: float | None = None

    def view(self) -> dict:
        return {
            "job_id": self.job_id, "state": self.state,
            "queued_seconds": round((self.started_at or time.time()) - self.queued_at, 1),
            "render_seconds": round((self.finished_at or time.time()) - self.started_at, 1)
            if self.started_at else None,
            "video_key": self.video_key, "duration": self.duration, "error": self.error,
        }


class Queue:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._jobs: dict[str, Job] = {}
        self._workers: list[asyncio.Task] = []

    # ─── API dùng bởi main.py ──────────────────────────────────────────────
    def submit(self, job_id: str) -> Job:
        if not _ID.match(job_id or ""):
            raise ValueError("job_id must be a UUID")
        job = self._jobs.get(job_id)
        if job and job.state in ("queued", "running"):
            return job                      # gọi lại cùng job là vô hại
        job = Job(job_id)
        self._jobs[job_id] = job
        self._queue.put_nowait(job_id)
        logger.info("Job %s: xếp hàng (%s job đang chờ)", job_id, self._queue.qsize())
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def snapshot(self) -> dict:
        return {
            "concurrency": CONCURRENCY,
            "waiting": self._queue.qsize(),
            "jobs": [j.view() for j in sorted(self._jobs.values(), key=lambda j: j.queued_at, reverse=True)][:50],
        }

    def start(self) -> None:
        self._workers = [asyncio.create_task(self._worker(i)) for i in range(CONCURRENCY)]

    async def stop(self) -> None:
        for task in self._workers:
            task.cancel()
        self._workers.clear()

    # ─── Worker ───────────────────────────────────────────────────────────
    async def _worker(self, index: int) -> None:
        logger.info("Render worker %s sẵn sàng", index)
        while True:
            job_id = await self._queue.get()
            job = self._jobs.get(job_id)
            if job is None:
                continue
            job.state, job.started_at = "running", time.time()
            try:
                await self._render(job)
                job.state = "done"
            except asyncio.CancelledError:
                job.state, job.error = "failed", "service đang tắt"
                await asyncio.to_thread(self._write_error, job)
                raise
            except Exception as exc:
                job.state = "failed"
                job.error = f"{type(exc).__name__}: {str(exc)[-1500:]}"
                logger.exception("Job %s: render thất bại", job_id)
                await asyncio.to_thread(self._write_error, job)
            finally:
                job.finished_at = time.time()
                shutil.rmtree(render.WORK_DIR / f"nba_{job_id}", ignore_errors=True)
                self._queue.task_done()

    async def _render(self, job: Job) -> None:
        prefix = f"{MEDIA_PREFIX}/{job.job_id}"
        spec = await asyncio.to_thread(read_json, f"{prefix}/render.json")
        if not spec:
            raise RuntimeError(f"không đọc được {prefix}/render.json")
        out_path, duration = await render.render_reel(job.job_id, spec)
        video_key = await asyncio.to_thread(put_file, f"{prefix}/reel.mp4", out_path)
        await asyncio.to_thread(write_json, f"{prefix}/render_result.json",
                                {"video_key": video_key, "duration": duration})
        job.video_key, job.duration = video_key, duration
        logger.info("Job %s: xong %s (%.1fs video)", job.job_id, video_key, duration)

    def _write_error(self, job: Job) -> None:
        try:
            write_json(f"{MEDIA_PREFIX}/{job.job_id}/render_result.json", {"error": job.error})
        except Exception:
            logger.exception("Job %s: không ghi được render_result.json", job.job_id)


queue = Queue()
