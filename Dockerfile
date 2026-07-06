FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FTA_HOST=0.0.0.0 \
    FTA_PORT=8787 \
    FTA_DATA_DIR=/data

WORKDIR /app
COPY transfer_assistant ./transfer_assistant
COPY client ./client
RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /data /app

USER appuser
EXPOSE 8787
CMD ["python", "-m", "transfer_assistant.server"]
