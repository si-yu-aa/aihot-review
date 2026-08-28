FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AIHOT_DATA_DIR=/data

WORKDIR /app

COPY server.py index.html app.js styles.css ./

RUN useradd --create-home --uid 10001 aihot \
    && mkdir -p /data \
    && chown -R aihot:aihot /app /data

USER aihot

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=3).read()"

CMD ["python", "server.py", "--host", "0.0.0.0"]
