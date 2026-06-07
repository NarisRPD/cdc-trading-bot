# syntax=docker/dockerfile:1.6
# ─────────────────────────────────────────────────────────────────────
# Stage 1 — builder: compile wheels, ทิ้ง build deps ก่อนเข้า runtime
# ─────────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build
COPY requirements.txt .

# ติดตั้งลง /install เพื่อก็อปไป runtime stage เป็นชั้นเดียว
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ─────────────────────────────────────────────────────────────────────
# Stage 2 — runtime: slim image ไม่มี build deps
# ─────────────────────────────────────────────────────────────────────
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Bangkok

# CA bundle จำเป็นสำหรับ HTTPS ไปยัง Telegram/Binance/Yahoo
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

# copy installed packages
COPY --from=builder /install /usr/local

WORKDIR /app
COPY . /app

# Cloud Run Job entrypoint
CMD ["python", "-u", "main.py"]
