FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Fixed numeric UID/GID (nonroot range) so volume/file ownership is
# reproducible on any host (same convention as NVRoute).
RUN groupadd --gid 65532 appuser \
    && useradd --uid 65532 --gid 65532 --no-create-home --shell /usr/sbin/nologin appuser
USER 65532

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
