# Image dùng chung cho hai service: TFTP nội bộ và web gateway.
# Build từ thư mục gốc repository:
#   docker build -f deploy/Dockerfile -t tftp-chat:local .
FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.title="tftp-nhom11" \
      org.opencontainers.image.description="TFTP server/gateway (RFC 1350/2347/7440)" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Cài package thường (không -e): image chạy được dù không giữ source tree.
COPY pyproject.toml README.md LICENSE ./
COPY tftp ./tftp
COPY deploy ./deploy

RUN pip install --no-cache-dir ".[web]" \
    && useradd --uid 10001 --create-home --home-dir /home/tftp --shell /usr/sbin/nologin tftp \
    && mkdir -p /srv/tftp/files /srv/tftp/web-data \
    && chown -R tftp:tftp /srv/tftp /app

USER tftp

# Chỉ có ý nghĩa khi chạy bridge network; compose mẫu dùng host network.
EXPOSE 8080 6969

CMD ["python", "deploy/run_web.py"]
