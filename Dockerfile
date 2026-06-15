FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000 \
    AGENT_HOST=0.0.0.0

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent.py webui.html ./

# Run as an unprivileged user.
RUN useradd -u 10001 -m appuser
USER appuser

EXPOSE 8000

# Liveness probe (no curl in slim images, so use Python). Reads $PORT so it
# always matches the port uvicorn binds below — including a PORT that the
# platform (e.g. Coolify) injects at runtime.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/health')" || exit 1

# Single worker on purpose: the live feed, history, and in-memory bearer token
# are per-process state. Do not scale workers or replicas above 1.
CMD ["sh", "-c", "exec uvicorn agent:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
