FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 XDG_CACHE_HOME=/tmp/.cache \
    MEDIA_DIR=/data/media WORK_DIR=/tmp/render FONTS_DIR=/usr/share/fonts

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg ca-certificates fontconfig fonts-noto-core && \
    rm -rf /var/lib/apt/lists/* && fc-cache -f

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
# compose chạy container bằng uid/gid của host (xem docker-compose.yml), nên thư mục làm việc
# phải ghi được với mọi uid; /data/media là bind mount, quyền do host quyết định.
RUN groupadd -r appuser && useradd -r -g appuser appuser && mkdir -p /data/media /tmp/render \
    && chown -R appuser:appuser /app /data && chmod 1777 /tmp/render
USER appuser

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-keep-alive", "120"]
