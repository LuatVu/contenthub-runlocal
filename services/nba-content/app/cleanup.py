"""Dọn media cũ trên đĩa local (thay cho lifecycle rule 7 ngày của bucket S3).

Chạy ngầm trong service nba-content: lúc khởi động và sau đó mỗi CLEANUP_INTERVAL_HOURS giờ,
xoá mọi thư mục <MEDIA_DIR>/nba/<reel_id> không được sửa trong MEDIA_RETENTION_DAYS ngày.
DB chỉ lưu key nên dòng reel cũ vẫn còn, chỉ file là mất — đúng như hành vi trên S3.
"""

import asyncio
import logging
import shutil
import time

from .config import MEDIA_PREFIX
from .storage import MEDIA_DIR

logger = logging.getLogger(__name__)


def sweep(retention_days: float) -> dict:
    root = MEDIA_DIR / MEDIA_PREFIX
    if retention_days <= 0 or not root.is_dir():
        return {"removed": 0, "kept": 0}
    cutoff = time.time() - retention_days * 86400
    removed, kept = 0, 0
    for path in sorted(root.iterdir()):
        if not path.is_dir():
            continue
        newest = max((f.stat().st_mtime for f in path.rglob("*") if f.is_file()), default=0.0)
        if newest and newest < cutoff:
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
        else:
            kept += 1
    if removed:
        logger.info("Cleanup: đã xoá %s thư mục reel cũ hơn %s ngày (còn %s)", removed, retention_days, kept)
    return {"removed": removed, "kept": kept}


async def loop(retention_days: float, interval_hours: float) -> None:
    while True:
        try:
            await asyncio.to_thread(sweep, retention_days)
        except Exception:
            logger.exception("Cleanup thất bại")
        await asyncio.sleep(max(600.0, interval_hours * 3600))
